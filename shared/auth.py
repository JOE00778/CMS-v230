"""双角色守门（管理员 / SmikieJapan）· 每个 page 顶部调用 require_password()。

密码配置（Streamlit Cloud → Settings → Secrets）：
    ADMIN_USERNAME = "JO043"            # 可选，默认 "JO043"
    ADMIN_PASSWORD = "..."
    GUEST_USERNAME = "smikiejapan"      # 可选，默认 "smikiejapan"
    GUEST_PASSWORD = "..."

向后兼容：仅配置 APP_PASSWORD 时视为管理员单密码（旧行为）。
两者都未配置时开放访问 → 默认管理员角色（避免误锁）。

Page 顶部用法：
    require_password()  # 任一角色登录即过
    require_admin()     # 仅管理员（page 03 / page 99）
    is_admin()          # 局部按钮控制
"""
from __future__ import annotations

import hmac
import os

import streamlit as st

# 每次重要修复 push 时 bump，Cloud 部署后一眼能看出是不是新版
APP_VERSION = "2.4.0 · nst-master-template"


def _secret(name: str, default: str = "") -> str:
    """优先读 streamlit secrets（Cloud 部署），fallback 到环境变量（Docker / NAS 部署）。"""
    # 1. Streamlit Cloud secrets.toml
    try:
        v = st.secrets.get(name, None)
        if v:
            return str(v)
    except (FileNotFoundError, KeyError):
        pass
    # 2. 环境变量（docker-compose 注入）
    return os.environ.get(name, "") or default


def _check(entered: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(entered, expected)


def _login_form() -> None:
    """单密码登录（CF Access 模式）。

    上层由 Cloudflare Access 邮箱域白名单守门（仅公司邮箱能到达此页），
    进来的都是公司员工，CMS 仅设统一密码、登录后默认 admin 角色。
    兼容旧 GUEST_PASSWORD：如果只配了 GUEST_PASSWORD 也能登录。
    """
    admin_pwd = _secret("ADMIN_PASSWORD") or _secret("APP_PASSWORD")
    guest_pwd = _secret("GUEST_PASSWORD")

    if not admin_pwd and not guest_pwd:
        # 完全未配密码 → 视为 CF Access 已守门，直接放行
        st.session_state["__auth_ok"] = True
        st.session_state["__role"] = "admin"
        return

    st.markdown(_LOGIN_NARROW_CSS, unsafe_allow_html=True)  # 登录页收窄居中
    st.title("🔒 一元管理系统V2.4")
    st.caption(f"build {APP_VERSION}")

    with st.form("login", clear_on_submit=False):
        p = st.text_input("密码", type="password", key="__login_pwd",
                          placeholder="请输入访问密码")
        if st.form_submit_button("登录", type="primary", use_container_width=True):
            if _check(p, admin_pwd) or _check(p, guest_pwd):
                st.session_state["__auth_ok"] = True
                st.session_state["__role"] = "admin"  # 统一 admin（CF Access 已过滤）
                st.session_state.pop("__login_pwd", None)
                st.rerun()
            else:
                st.error("密码错误")

    st.stop()


_GUEST_HIDE_CSS = """
<style>
/* 仅 SmikieJapan 角色：隐藏 Streamlit 顶部 toolbar、状态条、部署标记、Manage app 浮动按钮 */
[data-testid="stToolbar"],
[data-testid="stStatusWidget"],
[data-testid="stDecoration"],
[data-testid="stHeader"] button,
.viewerBadge_link__1S137,
.viewerBadge_container__r5tak,
.styles_viewerBadge__1yB5_,
#MainMenu,
header[data-testid="stHeader"] > div:last-child {
    display: none !important;
}
</style>
"""

# 紧凑 layout · 全局生效（不论角色）· 把内容顶到上面
_COMPACT_LAYOUT_CSS = """
<style>
/* 主内容区上 padding 从默认 6rem 压到 1rem */
[data-testid="stMainBlockContainer"],
.main .block-container,
[data-testid="block-container"] {
    padding-top: 1rem !important;
    padding-bottom: 2rem !important;
}
/* Streamlit 顶部 header 高度压扁（保留 toolbar 但压紧）*/
[data-testid="stHeader"] {
    height: 0 !important;
    background: transparent !important;
}
/* 标题与 caption 行间距收紧 */
h1, h2, h3 {
    margin-top: 0.5rem !important;
    padding-top: 0 !important;
}
</style>
"""


# 登录页专用：内容区收窄居中（覆盖全局 1600）+ 上方留白（Boss 2026-05-25）
_LOGIN_NARROW_CSS = """
<style>
[data-testid="stMainBlockContainer"],
.main .block-container,
[data-testid="block-container"] {
    max-width: 460px !important;
    margin-left: auto !important;
    margin-right: auto !important;
    padding-top: 8vh !important;
}
</style>
"""


def _apply_compact_layout() -> None:
    """全局紧凑布局 · 内容贴顶 · 每次 require_password / require_admin 都注入"""
    st.markdown(_COMPACT_LAYOUT_CSS, unsafe_allow_html=True)


def _hide_chrome_for_guest() -> None:
    if not is_admin():
        st.markdown(_GUEST_HIDE_CSS, unsafe_allow_html=True)


def _try_lark_sso() -> bool:
    """飞书 H5 应用 SSO 入口（NAS 部署时启用，Cloud 部署时 is_configured()=False 自动跳过）。

    URL 带 ?code=xxx 表示飞书 OAuth 回调，校验通过后直接以「team」角色登录。
    返回 True 说明已登录，外层不需要再渲染密码框。
    """
    try:
        from shared import lark_auth
    except ImportError:
        return False
    if not lark_auth.is_configured():
        return False
    user = lark_auth.try_handle_oauth_callback()
    if not user:
        return False
    st.session_state["__auth_ok"] = True
    # 飞书登录的同事默认走 SmikieJapan 角色（团队成员）；
    # 例外：飞书邮箱在 ADMIN_LARK_EMAILS（逗号分隔 secret）里的视为 admin
    admin_emails = {
        e.strip().lower()
        for e in (_secret("ADMIN_LARK_EMAILS") or "").split(",")
        if e.strip()
    }
    email = (user.get("email") or "").lower()
    st.session_state["__role"] = "admin" if email in admin_emails else "guest"
    st.session_state["__lark_user"] = user
    return True


def require_password() -> None:
    _apply_compact_layout()  # 全局贴顶 · 不论登录前后都生效
    if st.session_state.get("__auth_ok"):
        _hide_chrome_for_guest()
        return
    # 优先尝试飞书 SSO（仅 NAS 部署 + 配齐 LARK_* 时生效）
    if _try_lark_sso():
        st.rerun()
    _login_form()


def is_admin() -> bool:
    return st.session_state.get("__role") == "admin"


def require_admin() -> None:
    """整页禁SmikieJapan。仅 admin 可访问，否则显示提示并 stop。"""
    require_password()
    if not is_admin():
        st.title("⛔ 仅管理员可访问")
        st.warning("此功能涉及数据底盘操作（数据导入 / 定義原価覆盖），仅管理员账号可进入。")
        st.stop()


def show_role_badge() -> None:
    """已废弃：保留空实现兼容主入口已有调用。"""
    return


def require_extra_password(scope: str, env_var: str) -> None:
    """页面级独立二级密码守门（page 99 数据导入用）。

    用例：page 99 涉及数据库写入，需要比一级密码更高权限。
    一级密码登录后，访问该 page 时再单独验证二级密码。

    Args:
        scope: 密码作用域名（如 "page99"），用于隔离不同 page 的 session 状态
        env_var: 环境变量名，存储该 page 的密码（如 "PAGE99_PASSWORD"）
    """
    expected = _secret(env_var)
    if not expected:
        return  # 未配密码 = 不启用此层守门，直接放行

    state_key = f"__extra_auth_{scope}"
    if st.session_state.get(state_key):
        return  # 本 session 已通过该 scope 的二级密码

    st.title("🔐 此页面需要二级密码")
    st.caption("数据底盘操作（导入 / 写库）需要二级密码授权")

    with st.form(f"extra_auth_{scope}", clear_on_submit=False):
        p = st.text_input(
            "二级密码", type="password",
            key=f"__extra_pwd_{scope}",
            placeholder="请输入二级密码",
        )
        if st.form_submit_button("授权进入", type="primary", use_container_width=True):
            if _check(p, expected):
                st.session_state[state_key] = True
                st.session_state.pop(f"__extra_pwd_{scope}", None)
                st.rerun()
            else:
                st.error("二级密码错误")
    st.stop()
