# -*- coding: UTF-8 -*-

import xlwt
import time
from archery import settings
from django.db import close_old_connections, connection
from common.utils.timer import FuncTimer
from common.utils.const import WorkflowDict
from sql.engines.models import ReviewResult, ReviewSet
from sql.models import SqlWorkflow
from sql.notify import notify_for_execute
from sql.utils.workflow_audit import Audit
from sql.engines import get_engine


def query(workflow_id):
    """为延时或异步任务准备的queryx, 传入工单ID即可"""
    workflow = SqlWorkflow.objects.get(id=workflow_id)
    # 给定时执行的工单增加执行日志
    if workflow.status == 'workflow_timingtask':
        # 将工单状态修改为执行中
        SqlWorkflow(id=workflow_id, status='workflow_executing').save(update_fields=['status'])
        audit_id = Audit.detail_by_workflow_id(workflow_id=workflow_id,
                                               workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
        Audit.add_log(audit_id=audit_id,
                      operation_type=5,
                      operation_type_desc='执行工单',
                      operation_info='系统定时执行',
                      operator='',
                      operator_display='系统'
                      )
    query_engine = get_engine(instance=workflow.instance)

    with FuncTimer() as t:
        query_result = query_engine.query(workflow.db_name, workflow.sqlworkflowcontent.sql_content)
        # if workflow.instance.db_type == 'pgsql':  # TODO 此处判断待优化，请在 修改传参方式后去除
        #     query_result = query_engine.query(workflow.db_name, workflow.sqlworkflowcontent.sql_content,
        #                                       schema_name=workflow.schema_name)
        # else:
        #     query_result = query_engine.query(workflow.db_name, workflow.sqlworkflowcontent.sql_content)
    query_result.query_time = t.cost

    return query_result


def query_callback(task):
    """异步任务的回调, 将结果填入数据库等等
    使用django-q的hook, 传入参数为整个task
    task.review_set 是真正的结果
    """
    # https://stackoverflow.com/questions/7835272/django-operationalerror-2006-mysql-server-has-gone-away
    if connection.connection and not connection.is_usable():
        close_old_connections()
    workflow_id = task.args[0]
    workflow = SqlWorkflow.objects.get(id=workflow_id)
    workflow.finish_time = task.stopped
    query_result = task.result
    filename = ''

    review_set = ReviewSet(full_sql=workflow.sqlworkflowcontent.sql_content)
    if not task.success:
        # 不成功会返回错误堆栈信息，构造一个错误信息
        workflow.status = 'workflow_exception'

        review_set.rows = [ReviewResult(
            stage='Execute failed',
            errlevel=2,
            stagestatus='异常终止',
            errormessage=query_result,
            sql=workflow.sqlworkflowcontent.sql_content)]
    elif query_result.error:
        # 不成功会返回错误堆栈信息，构造一个错误信息
        workflow.status = 'workflow_exception'

        review_set.rows = [ReviewResult(
            stage='Execute failed',
            errlevel=2,
            stagestatus='异常终止',
            errormessage=query_result.error,
            sql=workflow.sqlworkflowcontent.sql_content)]
    else:
        filename = f"QueryTask{workflow.id}-{int(time.time())}.xls"
        save2excel(f"{settings.DOWNLOAD_DIR}/{filename}", [query_result.column_list]+list(query_result.rows))
        review_set.rows = [ReviewResult(
            errlevel=0,
            stagestatus='Execute Successfully',
            errormessage='None',
            sql=query_result.full_sql,
            affected_rows=query_result.affected_rows,
            execute_time=query_result.query_time,
        )]
        workflow.status = 'workflow_finish'
    # 保存执行结果
    workflow.sqlworkflowcontent.execute_result = review_set.json()
    workflow.sqlworkflowcontent.save()
    workflow.save()

    # 增加工单日志
    audit_id = Audit.detail_by_workflow_id(workflow_id=workflow_id,
                                           workflow_type=WorkflowDict.workflow_type['sqlreview']).audit_id
    Audit.add_log(audit_id=audit_id,
                  operation_type=6,
                  operation_type_desc='执行工单',
                  operation_info=f'执行结果：{workflow.get_status_display()}' + (f'，文件: [{filename}]' if filename else ''),
                  operator='',
                  operator_display='系统')


    # 发送消息
    notify_for_execute(workflow, filename_list=([f"{settings.DOWNLOAD_DIR}/{filename}"] if filename else None))


def save2excel(filename, table):
    """将表（二维元组）写到excel文件中"""
    wb = xlwt.Workbook()
    ws = wb.add_sheet('Sheet1')

    row = 0
    for record in table:
        col = 0
        for field in record:
            ws.write(row, col, field)
            col += 1
        row += 1
    wb.save(filename)
