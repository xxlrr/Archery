# -*- coding: UTF-8 -*-

import datetime
import logging
import traceback
import simplejson as json

from django.db.models import Q
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.shortcuts import HttpResponse, render, redirect
from django.contrib.auth.decorators import permission_required
from django_q.tasks import schedule, async_task
from django.http import JsonResponse

from common.config import SysConfig
from common.utils.const import WorkflowDict
from common.utils.extend_json_encoder import ExtendJSONEncoder
from sql.models import SqlWorkflow, SqlWorkflowContent, ResourceGroup, Instance, Users
from sql.engines import get_engine
from sql.engines.models import ReviewSet, ReviewResult
from sql.notify import notify_for_audit
from sql.utils.resource_group import user_groups, user_instances
from sql.utils.workflow_audit import Audit
from django_q.models import Schedule
from sql.query_privileges import query_priv_check


logger = logging.getLogger('default')


@permission_required('sql.sqlcron_list', raise_exception=True)
def list(request):
    """
    获取审核列表
    :param request:
    :return:
    """
    nav_status = request.POST.get('navStatus')
    instance_id = request.POST.get('instance_id')
    resource_group_id = request.POST.get('group_id')
    start_date = request.POST.get('start_date')
    end_date = request.POST.get('end_date')
    limit = int(request.POST.get('limit'))
    offset = int(request.POST.get('offset'))
    limit = offset + limit
    search = request.POST.get('search')
    user = request.user

    # 组合筛选项
    filter_dict = {'order_type': 'sqlcron_order'}
    # 工单状态
    if nav_status:
        filter_dict['status'] = nav_status
    # 实例
    if instance_id:
        filter_dict['instance_id'] = instance_id
    # 资源组
    if resource_group_id:
        filter_dict['group_id'] = resource_group_id
    # 时间
    if start_date and end_date:
        end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=1)
        filter_dict['create_time__range'] = (start_date, end_date)
    # 管理员，可查看所有工单
    if user.is_superuser:
        pass
    # 非管理员，拥有审核权限、资源组粒度执行权限的，可以查看组内所有工单
    elif user.has_perm('sql.sql_review') or user.has_perm('sql.sql_execute_for_resource_group'):
        # 先获取用户所在资源组列表
        group_list = user_groups(user)
        group_ids = [group.group_id for group in group_list]
        filter_dict['group_id__in'] = group_ids
    # 其他人只能查看自己提交的工单
    else:
        filter_dict['engineer'] = user.username

    # 过滤组合筛选项
    workflow = SqlWorkflow.objects.filter(**filter_dict)

    # 过滤搜索项，模糊检索项包括提交人名称、工单名
    if search:
        workflow = workflow.filter(Q(engineer_display__icontains=search) | Q(workflow_name__icontains=search))

    count = workflow.count()
    workflow_list = workflow.order_by('-create_time')[offset:limit].values(
        "id", "workflow_name", "engineer_display",
        "status", "is_backup", "finish_time",
        "instance__instance_name", "db_name",
        "group_name", "syntax_type")

    # QuerySet 序列化
    rows = [row for row in workflow_list]
    result = {"total": count, "rows": rows}
    # 返回查询结果
    return HttpResponse(json.dumps(result, cls=ExtendJSONEncoder, bigint_as_string=True),
                        content_type='application/json')


@permission_required('sql.sqlcron_newexec', raise_exception=True)
def newexec(request):
    """正式提交SQL, 此处生成工单"""
    workflow_name = request.POST.get('workflow_name')
    demand_url = request.POST.get('demand_url')
    group_name = request.POST.get('group_name')
    instance_name = request.POST.get('instance_name')
    db_name = request.POST.get('db_name')
    first_run_time = request.POST.get('first_run_time')
    schedule_type = request.POST.get('period')
    minutes = request.POST.get('minutes')
    receivers = request.POST.getlist('receivers')
    cc_list = request.POST.getlist('cc_list')
    is_backup = True if request.POST.get('is_backup') == 'True' else False
    sql_content = request.POST.get('sql_content').strip()

    # 服务器端参数验证
    if not all([workflow_name, group_name, instance_name, db_name, first_run_time, schedule_type, sql_content]):
        context = {'errMsg': '请检查提交工单填写内容是否完整'}
        return render(request, 'error.html', context)

    group_id = ResourceGroup.objects.get(group_name=group_name).group_id
    instance = Instance.objects.get(instance_name=instance_name)

    # 验证组权限（用户是否在该组、该组是否有指定实例）
    try:
        user_instances(request.user, tag_codes=['can_write']).get(instance_name=instance_name)
    except instance.DoesNotExist:
        context = {'errMsg': '你所在组未关联该实例！'}
        return render(request, 'error.html', context)

    # 再次交给engine进行检测，防止绕过
    try:
        check_engine = get_engine(instance=instance)
        check_result = check_engine.execute_check(db_name=db_name, sql=sql_content.strip())
    except Exception as e:
        context = {'errMsg': str(e)}
        return render(request, 'error.html', context)

    # 未开启备份选项，并且engine支持备份，强制设置备份
    sys_config = SysConfig()
    if not sys_config.get('enable_backup_switch') and check_engine.auto_backup:
        is_backup = True

    # 按照系统配置确定是自动驳回还是放行
    auto_review_wrong = sys_config.get('auto_review_wrong', '')  # 1表示出现警告就驳回，2和空表示出现错误才驳回
    workflow_status = 'workflow_manreviewing'
    if check_result.warning_count > 0 and auto_review_wrong == '1':
        workflow_status = 'workflow_autoreviewwrong'
    elif check_result.error_count > 0 and auto_review_wrong in ('', '1', '2'):
        workflow_status = 'workflow_autoreviewwrong'

    # 调用工作流生成工单
    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 存进数据库里
            sql_workflow = SqlWorkflow.objects.create(
                order_type="sqlcron_order",
                workflow_name=workflow_name,
                demand_url=demand_url,
                group_id=group_id,
                group_name=group_name,
                engineer=request.user.username,
                engineer_display=request.user.display,
                audit_auth_groups=Audit.settings(group_id, WorkflowDict.workflow_type['sqlreview']),
                status=workflow_status,
                is_backup=is_backup,
                instance=instance,
                db_name=db_name,
                is_manual=0,
                syntax_type=check_result.syntax_type,
                create_time=timezone.now(),
                run_date_start=None,
                run_date_end=None,
                cc_list=cc_list,
            )
            sql_workflow.receivers.set(Users.objects.filter(username__in=receivers))

            sql_workflow_content = SqlWorkflowContent.objects.create(
                workflow=sql_workflow,
                sql_content=sql_content,
                review_content=check_result.json(),
                execute_result=''
            )

            sched = schedule('sql.utils.execute_sql.execute',
                sql_workflow.id,
                hook='sql.utils.execute_sql.execute_callback',
                name=f'sqlcron-{sql_workflow.id}',
                schedule_type=schedule_type,
                minutes=minutes,
                next_run=first_run_time,
                repeats=0,     # 在审批结束前创建但不启用
                timeout=-1,
             )
            sql_workflow.schedule = sched

            sql_workflow.save()
            sql_workflow_content.save()

            # 自动审核通过了，才调用工作流
            if workflow_status == 'workflow_manreviewing':
                # 调用工作流插入审核信息, 查询权限申请workflow_type=2
                Audit.add(WorkflowDict.workflow_type['sqlreview'], sql_workflow.id)
    except Exception as msg:
        logger.error(f"提交工单报错，错误信息：{traceback.format_exc()}")
        context = {'errMsg': msg}
        logger.error(traceback.format_exc())
        return render(request, 'error.html', context)
    else:
        # 自动审核通过才进行消息通知
        if workflow_status == 'workflow_manreviewing':
            # 获取审核信息
            audit_id = Audit.detail_by_workflow_id(workflow_id=sql_workflow.id,
                                                   workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
            async_task(notify_for_audit, audit_id=audit_id, cc_users=receivers, timeout=60,
                       task_name=f'sqlreview-submit-{sql_workflow.id}')

    return redirect(reverse('sql:sqlcrondetail', args=(sql_workflow.id,)))


@permission_required('sql.sqlcron_newquery', raise_exception=True)
def newquery(request):
    """正式提交SQL, 此处生成工单"""
    workflow_name = request.POST.get('workflow_name')
    demand_url = request.POST.get('demand_url', '')
    group_name = request.POST.get('group_name')
    instance_name = request.POST.get('instance_name')
    db_name = request.POST.get('db_name')
    first_run_time = request.POST.get('first_run_time')
    schedule_type = request.POST.get('period')
    minutes = request.POST.get('minutes')
    receivers = request.POST.getlist('receivers')
    cc_list = request.POST.getlist('cc_list')
    is_backup = False
    sql_content = request.POST.get('sql_content').strip()

    result = {'status': 0, 'msg': 'ok', 'data': {}}
    # 服务器端参数验证
    if not all([workflow_name, group_name, instance_name, db_name, first_run_time, schedule_type, sql_content]):
        result['status'] = 1
        result['msg'] = '请检查提交工单填写内容是否完整'
        return HttpResponse(json.dumps(result), content_type='application/json')

    user = request.user
    group_id = ResourceGroup.objects.get(group_name=group_name).group_id
    instance = Instance.objects.get(instance_name=instance_name)

    # 验证组权限（用户是否在该组、该组是否有指定实例）
    try:
        user_instances(user, tag_codes=['can_read']).get(instance_name=instance_name)
    except instance.DoesNotExist:
        result['status'] = 1
        result['msg'] = '你所在组未关联该实例'
        return HttpResponse(json.dumps(result), content_type='application/json')

    try:
        config = SysConfig()
        # 查询前的检查，禁用语句检查，语句切分
        query_engine = get_engine(instance=instance)
        query_check_info = query_engine.query_check(db_name=db_name, sql=sql_content)
        if query_check_info.get('bad_query'):
            # 引擎内部判断为 bad_query
            result['status'] = 1
            result['msg'] = query_check_info.get('msg')
            return HttpResponse(json.dumps(result), content_type='application/json')
        if query_check_info.get('has_star') and config.get('disable_star') is True:
            # 引擎内部判断为有 * 且禁止 * 选项打开
            result['status'] = 1
            result['msg'] = query_check_info.get('msg')
            return HttpResponse(json.dumps(result), content_type='application/json')
        sql_content = query_check_info['filtered_sql']

        # 查询权限校验，并且获取limit_num
        priv_check_info = query_priv_check(user, instance, db_name, sql_content, 0)
        if priv_check_info['status'] != 0:
            result['status'] = 1
            result['msg'] = priv_check_info['msg']
            return HttpResponse(json.dumps(result), content_type='application/json')
    except Exception as e:
        logger.error(f'查询异常报错，查询语句：{sql_content}\n，错误信息：{traceback.format_exc()}')
        result['status'] = 1
        result['msg'] = f'查询异常报错，错误信息：{e}'
        return HttpResponse(json.dumps(result), content_type='application/json')

    check_result = ReviewSet(full_sql=sql_content)
    check_result.rows = [ReviewResult(id=1,
                              errlevel=0,
                              stagestatus='Audit completed',
                              errormessage='None',
                              sql=sql_content,
                              affected_rows=0,
                              execute_time=0, )]
    # 调用工作流生成工单
    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 存进数据库里
            sql_workflow = SqlWorkflow.objects.create(
                order_type="sqlcron_order",
                workflow_name=workflow_name,
                demand_url=demand_url,
                group_id=group_id,
                group_name=group_name,
                engineer=request.user.username,
                engineer_display=request.user.display,
                audit_auth_groups=Audit.settings(group_id, WorkflowDict.workflow_type['sqlreview']),
                status='workflow_manreviewing',
                is_backup=is_backup,
                instance=instance,
                db_name=db_name,
                is_manual=0,
                syntax_type=3,   #0、未知，1、DDL，2、DML 3、QUERY'
                create_time=timezone.now(),
                run_date_start=None,
                run_date_end=None,
                cc_list=cc_list,
            )
            sql_workflow.receivers.set(Users.objects.filter(username__in=receivers))
            sql_workflow_content = SqlWorkflowContent.objects.create(
                workflow=sql_workflow,
                sql_content=sql_content,
                review_content=check_result.json(),
                execute_result=''
            )
            sched = schedule('sql.utils.query_sql.query',
                sql_workflow.id,
                hook='sql.utils.query_sql.query_callback',
                name=f'sqlcron-{sql_workflow.id}',
                schedule_type=schedule_type,
                minutes=minutes,
                next_run=first_run_time,
                repeats=0,     # 在审批结束前创建但不启用
                timeout=-1,
            )
            sql_workflow.schedule = sched
            sql_workflow.save()
            sql_workflow_content.save()

            # 调用工作流插入审核信息, 查询权限申请workflow_type=2
            Audit.add(WorkflowDict.workflow_type['sqlreview'], sql_workflow.id)
    except Exception as e:
        logger.error(f"提交工单报错，错误信息：{traceback.format_exc()}")
        result['status'] = 1
        result['msg'] = f'提交工单报错，错误信息：{e}'
        return HttpResponse(json.dumps(result), content_type='application/json')
    else:
        # 进行消息通知
        audit_id = Audit.detail_by_workflow_id(workflow_id=sql_workflow.id,
                                               workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
        async_task(notify_for_audit, audit_id=audit_id, cc_users=receivers, timeout=60,
                   task_name=f'sqlreview-submit-{sql_workflow.id}')

    result['status'] = 0
    result['data'] = {'redirect': reverse('sql:sqlcrondetail', args=(sql_workflow.id,))}
    return HttpResponse(json.dumps(result), content_type='application/json')

@permission_required('sql.sqlcron_switch', raise_exception=True)
def pause(request):
    user = request.user
    workflow_id = request.POST.get('workflow_id')

    if not workflow_id:
        return JsonResponse({'status': 1, 'msg': '参数不完整，请确认后提交', 'data': []})

    try:
        group_list = user_groups(user)
        group_ids = [group.group_id for group in group_list]
        workflow = SqlWorkflow.objects.get(id=workflow_id)

        if workflow.group_id not in group_ids:
            return JsonResponse({'status': 1, 'msg': '您无权操作', 'data': []})
        # 在‘已审核’、‘在执行’、‘已执行’的状态下工单才能被暂停
        if workflow.status not in ['workflow_review_pass', 'workflow_executing', 'workflow_finish']:
            return JsonResponse({'status': 1, 'msg': '该工单非暂停状态，操作非法', 'data': []})

        with transaction.atomic():
            Schedule.objects.filter(name=f'sqlcron-{workflow_id}').update(repeats=0)
            workflow.status = 'workflow_pause'
            workflow.save()

        # 添加工单日志
        audit_id = Audit.detail_by_workflow_id(workflow_id=workflow_id,
                                               workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
        Audit.add_log(audit_id=audit_id,
                      operation_type=5,
                      operation_type_desc='执行工单',
                      operation_info='执行结果：已暂停任务',
                      operator=user.username,
                      operator_display=user.display,
                      )

    except Exception as e:
        logger.error(f'发生异常，错误信息：{traceback.format_exc()}')
        return JsonResponse({'status': 1, 'msg': f'发生异常，错误信息：{e}'})

    return JsonResponse({'status': 0, 'msg': '', 'data': []})


@permission_required('sql.sqlcron_switch', raise_exception=True)
def resume(request):
    user = request.user
    workflow_id = request.POST.get('workflow_id')

    if not workflow_id:
        return JsonResponse({'status': 1, 'msg': '参数不完整，请确认后提交', 'data': []})

    try:
        group_list = user_groups(user)
        group_ids = [group.group_id for group in group_list]
        workflow = SqlWorkflow.objects.get(id=workflow_id)

        if workflow.group_id not in group_ids:
            return JsonResponse({'status': 1, 'msg': '您无权操作', 'data': []})
        # 只有在暂停状态下，工单才可以转为继续
        if workflow.status != 'workflow_pause':
            return JsonResponse({'status': 1, 'msg': '该工单非暂停状态，操作非法', 'data': []})

        with transaction.atomic():
            Schedule.objects.filter(name=f'sqlcron-{workflow_id}').update(repeats=-1)
            workflow.status = 'workflow_review_pass'
            workflow.save()

        # 添加工单日志
        audit_id = Audit.detail_by_workflow_id(workflow_id=workflow_id,
                                               workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
        Audit.add_log(audit_id=audit_id,
                      operation_type=5,
                      operation_type_desc='执行工单',
                      operation_info='执行结果：已恢复任务',
                      operator=user.username,
                      operator_display=user.display,
                      )
    except Exception as e:
        logger.error(f'发生异常，错误信息：{traceback.format_exc()}')
        return JsonResponse({'status': 1, 'msg': f'发生异常，错误信息：{e}'})

    return JsonResponse({'status': 0, 'msg': '', 'data': []})


@permission_required('sql.sqlcron_stop', raise_exception=True)
def stop(request):
    user = request.user
    workflow_id = request.POST.get('workflow_id')

    if not workflow_id:
        return JsonResponse({'status': 1, 'msg': '参数不完整，请确认后提交', 'data': []})

    try:
        group_list = user_groups(user)
        group_ids = [group.group_id for group in group_list]
        workflow = SqlWorkflow.objects.get(id=workflow_id)

        if workflow.group_id not in group_ids:
            return JsonResponse({'status': 1, 'msg': '您无权操作', 'data': []})

        # 只有工单流转起来后，才能停止
        if workflow.status not in ['workflow_finish', 'workflow_review_pass',
                                   'workflow_exception', 'workflow_pause']:
            return JsonResponse({'status': 1, 'msg': '该工单无法停止，操作非法', 'data': []})

        with transaction.atomic():
            Schedule.objects.filter(name=f'sqlcron-{workflow_id}').update(repeats=0)
            workflow.status = 'workflow_stop'
            workflow.save()

        # 添加工单日志
        audit_id = Audit.detail_by_workflow_id(workflow_id=workflow_id,
                                               workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
        Audit.add_log(audit_id=audit_id,
                      operation_type=5,
                      operation_type_desc='执行工单',
                      operation_info='执行结果：已停止任务',
                      operator=user.username,
                      operator_display=user.display,
                      )
    except Exception as e:
        logger.error(f'发生异常，错误信息：{traceback.format_exc()}')
        return JsonResponse({'status': 1, 'msg': f'发生异常，错误信息：{e}'})

    return JsonResponse({'status': 0, 'msg': '', 'data': []})