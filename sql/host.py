# -*- coding: UTF-8 -*-
import simplejson as json
from django.contrib.auth.decorators import permission_required
from django.http import HttpResponse
from django.shortcuts import render

from common.utils.extend_json_encoder import ExtendJSONEncoder
from .models import Host, MyCnf
from paramiko import SSHClient, AutoAddPolicy


@permission_required('sql.menu_host', raise_exception=True)
def lists(request):
    """获取实例列表"""
    limit = int(request.POST.get('limit'))
    offset = int(request.POST.get('offset'))
    limit = offset + limit
    search = request.POST.get('search', '')

    # 组合筛选项
    filter_dict = dict()
    # 过滤搜索
    if search:
        filter_dict['host_name__icontains'] = search

    hosts = Host.objects.filter(**filter_dict)

    count = hosts.count()
    hosts = hosts[offset:limit].values("host_id", "host_name", "host_addr", "ssh_port", "ssh_user")
    # QuerySet 序列化
    rows = [row for row in hosts]

    result = {"total": count, "rows": rows}
    return HttpResponse(json.dumps(result, cls=ExtendJSONEncoder, bigint_as_string=True),
                        content_type='application/json')


@permission_required('sql.menu_host', raise_exception=True)
def login(request):
    pass


# check host login
@permission_required('sql.menu_host', raise_exception=True)
def check(request):
    result = {'status': 0, 'msg': 'ok', 'data': []}
    host_id = request.POST.get('host_id')
    host = Host.objects.get(host_id=host_id)
    try:
        ssh = SSHClient()
        # 把要连接的机器添加到known_hosts文件中
        ssh.set_missing_host_key_policy(AutoAddPolicy())
        ssh.connect(hostname=host.host_addr, port=host.ssh_port, username=host.ssh_user, password=host.ssh_password, timeout=6)
        ssh.close()
    except Exception as e:
        result['status'] = 1
        result['msg'] = '无法连接主机,\n{}'.format(str(e))
    # 返回结果
    return HttpResponse(json.dumps(result), content_type='application/json')
