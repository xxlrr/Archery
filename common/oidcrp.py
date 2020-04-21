# -*- coding: UTF-8 -*-
import base64, logging
from django.contrib import auth
from django.shortcuts import render, redirect

from oic.oic import Client
from oic.utils.authn.client import CLIENT_AUTHN_METHOD
from oic.oic.message import RegistrationResponse
from oic import rndstr
from oic.oic.message import AuthorizationResponse

from sql.models import Users
from common.config import SysConfig
from common.auth import init_user

_logger = logging.getLogger('default')
_client = None


def _register():
    global _client

    _config = SysConfig()
    if _config.get("oidc_enable"):
        oidcrp = {
            "provider": _config.get("oidc_provider_url"),
            "client_id": _config.get("oidc_client_id"),
            "client_secret": _config.get("oidc_client_secret"),
            "redirect_uris": [_config.get("oidc_redirect_url")],
            "logout_url": _config.get("oidc_logout_url"),
        }
        _client = Client(client_authn_method=CLIENT_AUTHN_METHOD)
        _client.provider_config(oidcrp.get("provider"))
        client_reg = RegistrationResponse(**oidcrp)
        _client.store_registration_info(client_reg)
    else:
        _client = None


SysConfig.subscribe(_register)
_register()


def authenticate(request):
    if request.user and request.user.is_authenticated:
        return redirect('/')

    request.session["state"] = rndstr()
    request.session["nonce"] = rndstr()
    args = {
        "client_id": _client.client_id,
        "response_type": "code",
        "scope": ["profile", "company_info"],
        "nonce": request.session["nonce"],
        "redirect_uri": _client.registration_response["redirect_uris"][0],
        "state": request.session["state"],
    }

    auth_req = _client.construct_AuthorizationRequest(request_args=args)
    login_url = auth_req.request(_client.authorization_endpoint)

    return redirect(login_url)


def sign_out(request):
    auth.logout(request)
    return redirect(_client.registration_response["logout_url"])


def callback(request):
    response = request.META["QUERY_STRING"]
    try:
        aresp = _client.parse_response(AuthorizationResponse, info=response, sformat="urlencoded")
        assert aresp["state"] == request.session["state"]
        resp = _client.do_access_token_request(state=aresp["state"],
                                              scope=["profile", "company_info"],
                                              request_args={"code": aresp["code"]},
                                              authn_method="client_secret_basic")
        profile = _client.do_user_info_request(state=aresp["state"])
        _logger.debug(base64.b64encode(profile.to_json().encode("utf-8")))
    except:
        return redirect("/login/")

    email = profile.get("email")
    if email is None:
        return render(request, "forbidden.html", {
            "title": "Sorry, 您暂时无法使用此平台!",
            "context": "这很奇怪，你好像没有企业邮箱账号☠",
            "logout": _client.logout_url,
        })

    try:
        user = Users.objects.get(email=email)
    except Users.DoesNotExist:
        return render(request, "forbidden.html", {
            "title": "您无权访问，如需开通请邮件申请!",
            "context": """
邮件格式：

    标题：
        边锋-数据库平台登陆权限

    正文：
        姓名：
        邮箱：
        所属部门：
        部门领导：
        申请原因：

请发送给雪豹项目部（g-xb@bianfeng.com），并抄送给部门领导
        """,
            "logout": _client.logout_url,
        })

    if not user.last_login:
        init_user(user)
    auth.login(request, user)

    return redirect("/")
