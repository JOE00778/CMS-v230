"""飞书 H5 应用 OAuth 登录支持。

适用：NAS Self-hosted 部署 + 飞书自建应用包装。
团队成员从飞书工作台点开 CMS 时，飞书把 user_access_token 注入 URL，
本模块校验后自动登录（无需输入账号密码）。

文档：https://open.feishu.cn/document/server-docs/authentication-management/access-token/web_app_user_access_token

环境变量（NAS docker-compose 注入）：
    LARK_APP_ID         自建应用 App ID（cli_xxx 格式）
    LARK_APP_SECRET     自建应用 Secret
    LARK_REDIRECT_URI   OAuth 回调，与飞书后台填的「重定向 URL」一致

URL flow：
    1. 飞书工作台点应用 → 跳到 https://open.feishu.cn/open-apis/authen/v1/index?
       app_id=...&redirect_uri=... → 同意授权
    2. 飞书 302 回到 ${LARK_REDIRECT_URI}?code=xxx&state=...
    3. 本模块拿 code 换 user_access_token，再换 user_info（含 union_id / email / name）
    4. 把 union_id 写入 st.session_state，标记登录通过
"""
from __future__ import annotations

import hmac
import os
import secrets
from typing import Optional

import requests
import streamlit as st


LARK_OPEN_HOST = "https://open.feishu.cn"


def _config() -> tuple[str, str, str]:
    """读 secrets / env，返回 (app_id, app_secret, redirect_uri)。"""
    app_id = os.environ.get("LARK_APP_ID") or _safe_secret("LARK_APP_ID")
    app_secret = os.environ.get("LARK_APP_SECRET") or _safe_secret("LARK_APP_SECRET")
    redirect = os.environ.get("LARK_REDIRECT_URI") or _safe_secret("LARK_REDIRECT_URI")
    return app_id, app_secret, redirect


def _safe_secret(key: str) -> str:
    try:
        return str(st.secrets.get(key, "") or "")
    except (FileNotFoundError, KeyError):
        return ""


def is_configured() -> bool:
    """飞书 SSO 是否已配置（NAS 部署后才为 True；现 Cloud 部署仍 False）。"""
    app_id, app_secret, redirect = _config()
    return bool(app_id and app_secret and redirect)


# 監査 L3（login CSRF）: state は毎回ランダム、発行したブラウザに cookie で紐付け、
# 回调で URL の state と cookie が一致したときだけ code を換える。
# cookie の書き方は 7 天免登 cookie（auth._write_login_cookie）と同じ JS document.cookie。
STATE_COOKIE = "cms_lark_state"
_STATE_TTL = 600  # 10 分：飞书授权页で放置しても通る長さ


def new_state() -> str:
    return "cms-" + secrets.token_urlsafe(16)


def state_cookie_js(state: str) -> str:
    """state を同ブラウザの cookie に書く JS（components.html の <script> 内で使う）。"""
    return (f'document.cookie = "{STATE_COOKIE}={state}; path=/; '
            f'max-age={_STATE_TTL}; SameSite=Lax";')


def _browser_cookie(name: str) -> str:
    try:
        return (st.context.cookies or {}).get(name) or ""
    except Exception:
        return ""


def build_login_url(state: str) -> str:
    """构造飞书 OAuth 同意页 URL。state 用 new_state() 生成并用 state_cookie_js() 写进浏览器。"""
    app_id, _, redirect = _config()
    return (
        f"{LARK_OPEN_HOST}/open-apis/authen/v1/index"
        f"?app_id={app_id}&redirect_uri={redirect}&state={state}"
    )


def _get_app_access_token() -> Optional[str]:
    """换应用级 token（用 app_id + app_secret）。"""
    app_id, app_secret, _ = _config()
    if not (app_id and app_secret):
        return None
    res = requests.post(
        f"{LARK_OPEN_HOST}/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    res.raise_for_status()
    data = res.json()
    return data.get("app_access_token")


def exchange_code_for_user(code: str) -> Optional[dict]:
    """用 OAuth code 换 user_access_token，再换 user_info。

    返回 dict 含：union_id / open_id / name / email / en_name / mobile（按需）。
    失败返回 None。
    """
    app_token = _get_app_access_token()
    if not app_token:
        return None

    # Step 1: code → user_access_token
    res = requests.post(
        f"{LARK_OPEN_HOST}/open-apis/authen/v1/oidc/access_token",
        headers={"Authorization": f"Bearer {app_token}"},
        json={"grant_type": "authorization_code", "code": code},
        timeout=10,
    )
    if res.status_code != 200:
        return None
    user_token = res.json().get("data", {}).get("access_token")
    if not user_token:
        return None

    # Step 2: user_access_token → user_info
    res2 = requests.get(
        f"{LARK_OPEN_HOST}/open-apis/authen/v1/user_info",
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=10,
    )
    if res2.status_code != 200:
        return None
    return res2.json().get("data") or None


def try_handle_oauth_callback() -> Optional[dict]:
    """检查 URL query 是否带 ?code=xxx，是的话执行登录。

    每个 page 顶部 require_password() 时会先调一次本函数。
    成功返回 user dict，失败/无 code 返回 None。
    """
    if not is_configured():
        return None
    qp = st.query_params
    code = qp.get("code")
    if not code:
        return None
    # 監査 L3: 发起登录的浏览器才有同值 cookie；不符（他人发来的 /?code= 链接）就不换 code
    got, expect = qp.get("state") or "", _browser_cookie(STATE_COOKIE)
    if not (got and expect and hmac.compare_digest(got.encode(), expect.encode())):
        return None
    try:
        user = exchange_code_for_user(code)
    except Exception:
        return None
    if user:
        # 清掉 URL 上的 code，避免刷新重提交
        try:
            st.query_params.clear()
        except Exception:
            pass
    return user
