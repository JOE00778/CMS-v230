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

import base64
import hashlib
import hmac
import json
import os
import time

import streamlit as st

# 每次重要修复 push 时 bump，Cloud 部署后一眼能看出是不是新版
APP_VERSION = "2.7.0 · inventory-risk-i18n"


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
    st.title("🔒 一元管理系统V2.7")
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
.viewerBadge_link__1S137,
.viewerBadge_container__r5tak,
.styles_viewerBadge__1yB5_,
#MainMenu {
    display: none !important;
}
/* 注：不要笼统隐藏 [data-testid="stHeader"] button —— 会连带杀掉侧栏控件；toolbar / MainMenu 已各自精准隐藏。
   侧栏折叠相关处理统一在全局 _COMPACT_LAYOUT_CSS（永久固定·禁折叠），此处不重复。*/
</style>
"""

# 紧凑 layout · 全局生效（不论角色）· 把内容顶到上面
_COMPACT_LAYOUT_CSS = """
<style>
/* 主内容区上 padding 从默认 6rem 压到 1rem */
[data-testid="stMainBlockContainer"],
.main .block-container,
[data-testid="block-container"] {
    padding-top: 0 !important;
    padding-bottom: 2rem !important;
}
/* Streamlit 顶部 header 高度压扁（保留 toolbar 但压紧）*/
[data-testid="stHeader"] {
    height: 0 !important;
    background: transparent !important;
    overflow: visible !important;   /* 别裁掉 header 里 fixed 定位的侧栏展开控件 */
}
/* === 侧边栏永久固定 · 禁用折叠（Boss 2026-06-30：折叠后展不开的彻底解法）=== */
/* 1) 折叠 / 展开按钮全部隐藏 —— 用户无法再折叠，从源头根除「折叠后回不来」 */
[data-testid="stSidebarCollapseButton"],
[data-testid="stExpandSidebarButton"],
[data-testid="stSidebarCollapsedControl"] {
    display: none !important;
}
/* 2) 强制 sidebar 始终在视口内展开 —— 1.57 折叠机制 = transform:translateX(-102%)，
      这里无条件复位；同时覆盖窄屏 / 子页 auto-collapse 与浏览器已存的 collapsed 状态 */
section[data-testid="stSidebar"] {
    transform: none !important;
    visibility: visible !important;
    margin-left: 0 !important;
}
section[data-testid="stSidebar"][aria-expanded="false"] {
    min-width: 240px !important;
    width: 240px !important;
    max-width: 240px !important;
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
    max-width: 1150px !important;
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


# ---- 方案A：登录态持久化到浏览器签名 cookie（刷新无感、7 天免登）----
_COOKIE_NAME = "cms_lark_auth"
_COOKIE_TTL = 7 * 24 * 3600  # 7 天


def _cookie_secret() -> str:
    """cookie 签名密钥（复用已有 secret，不新增配置）。"""
    return (_secret("LARK_APP_SECRET") or _secret("ADMIN_PASSWORD")
            or _secret("APP_PASSWORD") or "cms-cookie-fallback-secret")


def _sign_cookie(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(_cookie_secret().encode(), raw.encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _verify_cookie(token: str):
    """验签 + 查过期，返回 payload dict 或 None。"""
    try:
        raw, sig = token.rsplit(".", 1)
        expect = hmac.new(_cookie_secret().encode(), raw.encode(),
                          hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except Exception:
        return None


def _restore_from_cookie() -> bool:
    """从 st.context.cookies 读签名 cookie，验签通过则恢复登录态（同步、刷新首次即有）。"""
    try:
        token = (st.context.cookies or {}).get(_COOKIE_NAME)
    except Exception:
        return False
    if not token:
        return False
    payload = _verify_cookie(token)
    if not payload:
        return False
    st.session_state["__auth_ok"] = True
    st.session_state["__role"] = payload.get("role", "guest")
    st.session_state["__lark_user"] = payload.get("user", {})
    return True


def _write_login_cookie(user: dict, role: str) -> None:
    """登录成功后写签名 cookie 到浏览器（7 天）。

    用 components.html 注入 JS 写 document.cookie——Streamlit 的 srcdoc iframe 在
    `allow-same-origin` sandbox 下同源，document.cookie 写的是同域 cookie，
    st.context.cookies 下次请求即可读到。比 CookieManager.set（刚创建就 set 有
    时序 bug，丢失）可靠。
    """
    payload = {
        "uid": (user.get("union_id") or user.get("open_id") or ""),
        "role": role,
        "user": {"name": user.get("name", ""), "email": user.get("email", "")},
        "exp": int(time.time()) + _COOKIE_TTL,
    }
    token = _sign_cookie(payload)
    import streamlit.components.v1 as components
    components.html(
        f'<script>document.cookie = "{_COOKIE_NAME}={token}; path=/; '
        f'max-age={_COOKIE_TTL}; SameSite=Lax";</script>',
        height=0,
    )


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
    _write_login_cookie(user, st.session_state["__role"])  # 方案A：持久化到浏览器 cookie
    return True


def _lark_sso_enabled() -> bool:
    """飞书 SSO 是否已配齐（LARK_APP_ID/SECRET/REDIRECT_URI 都有）。"""
    try:
        from shared import lark_auth
    except ImportError:
        return False
    return lark_auth.is_configured()


# 飞书登录页专用 · 卡片式（参照同事 HR app 的 Lark ログイン 页）
_LARK_LOGIN_CSS = """
<style>
[data-testid="stMainBlockContainer"], .main .block-container, [data-testid="block-container"]{
    max-width: 540px !important; margin-left:auto !important; margin-right:auto !important;
    padding-top: 7vh !important;
}
.lark-card{
    background:#fff; border:1px solid #E5E7EB; border-radius:16px;
    padding:44px 40px 38px; box-shadow:0 4px 24px rgba(17,24,39,.06); text-align:center;
}
.lark-card h1{font-size:2.2rem; font-weight:800; margin:0 0 14px; color:#1F2937; letter-spacing:.01em;}
.lark-card p{color:#6B7280; font-size:.92rem; line-height:1.75; margin:0 0 26px;}
.lark-btn{display:block; width:100%; box-sizing:border-box; background:#2B6E8F; color:#fff !important;
    padding:13px 0; border-radius:10px; font-weight:700; font-size:1.05rem; text-decoration:none;
    transition:background .15s;}
.lark-btn:hover{background:#225a76;}
.lark-note{color:#9CA3AF; font-size:.9rem; padding:12px 0;}
.lark-dev-label{color:#9CA3AF; font-size:.8rem; margin:22px 0 6px; text-align:left;}
</style>
"""


def _mock_login(role: str) -> None:
    """开发用模拟登录：直接写登录态（不走真 OAuth）。仅 dev 入口可触发。"""
    st.session_state["__auth_ok"] = True
    st.session_state["__role"] = role
    st.session_state["__lark_user"] = {"name": f"mock-{role}", "email": "", "union_id": "mock"}
    st.rerun()


def _lark_login_gate(dev_mock: bool = False) -> None:
    """飞书 SSO 已启用但未登录 → 卡片式「CMS 登录」引导页并 st.stop()。

    这是「只能飞书账号进、一般网页进不来」的强制落点：**不 fallback 到密码框**。
    外人直接打开 URL 也只会停在这一页，点登录会被带到飞书授权；不在应用「可用
    范围」/ 无飞书账号者拿不到 code，进不来。
    应急回退：在元川机器清掉 LARK_* env → is_configured()=False → 自动回到密码/CF 模式。
    dev_mock=True（仅 CMS_DEV_MOCK_LOGIN=1，生产不设）时额外显示模拟登录按钮。
    """
    st.markdown(_LARK_LOGIN_CSS, unsafe_allow_html=True)

    # URL 带 code 走到这里 = 换 token 失败（过期/已用）。此时**不自动跳转**（防
    # 「失败→自动跳→又失败」死循环），显示手动重试。
    code_failed = bool(st.query_params.get("code"))
    if code_failed:
        st.error("飞书登录未成功（授权码可能已过期），请点下方手动重试。")
        try:
            st.query_params.clear()
        except Exception:
            pass

    lark_on = _lark_sso_enabled()

    # 正常未登录 + 飞书已配 + 非失败 + 非 dev → 自动重定向飞书授权（方案B）。
    # session_state 刷新即丢，靠这个让刷新自动重登：飞书已授权则静默回跳带 code →
    # 自动登录，用户只闪一下、无需手动点。
    if lark_on and not code_failed and not dev_mock:
        from shared import lark_auth
        import streamlit.components.v1 as components
        login_url = lark_auth.build_login_url()
        st.markdown(
            '<div class="lark-card"><h1>🔒 CMS 登录</h1>'
            '<p>正在跳转飞书登录…<br>没有自动跳转的话，点下方按钮。</p>'
            f'<a class="lark-btn" href="{login_url}" target="_top">用飞书登录</a></div>',
            unsafe_allow_html=True,
        )
        # Streamlit components iframe 的 sandbox 含 allow-top-navigation → 可跳顶层窗口
        components.html(
            f'<script>window.top.location.href = "{login_url}";</script>',
            height=0,
        )
        st.stop()

    # 失败 / 飞书没配 / dev_mock → 显示卡片 + 手动按钮（不自动跳）
    if lark_on:
        from shared import lark_auth
        action = (f'<a class="lark-btn" href="{lark_auth.build_login_url()}" '
                  f'target="_top">用飞书登录</a>')
    else:
        action = '<div class="lark-note">飞书未配置（开发模式）</div>'
    st.markdown(
        '<div class="lark-card"><h1>🔒 CMS 登录</h1>'
        '<p>本系统通过<b>飞书账号</b>登录，仅授权成员可访问。</p>'
        f'{action}</div>',
        unsafe_allow_html=True,
    )

    if dev_mock:
        st.markdown('<div class="lark-dev-label">开发用：模拟登录（仅本地，生产关闭）</div>',
                    unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        if c1.button("以管理员进入", use_container_width=True):
            _mock_login("admin")
        if c2.button("以普通成员进入", use_container_width=True):
            _mock_login("guest")
    st.stop()


def require_password() -> None:
    _apply_compact_layout()  # 全局贴顶 · 不论登录前后都生效
    if st.session_state.get("__auth_ok"):
        _hide_chrome_for_guest()
        return
    # 方案A：先从签名 cookie 恢复登录态（st.context.cookies 同步、刷新首次 run 即有
    # → 真无感，7 天内不用重登）。
    if _restore_from_cookie():
        _hide_chrome_for_guest()
        return
    dev_mock = _secret("CMS_DEV_MOCK_LOGIN") == "1"  # 仅本地开发；生产元川绝不设此 env
    # 飞书 SSO 已配置：强制走飞书账号，**不再 fallback 到密码框**（否则密码框=外人
    # 后门，"只能飞书账号进"就破了）。飞书未配时维持原行为（CF Access + 统一密码框）。
    if _lark_sso_enabled():
        if _try_lark_sso():           # 登录成功 → 已写 session + cookie；**不 rerun**，
            _hide_chrome_for_guest()  # 让本次 run 渲染完（CookieManager component 才写入 cookie）
            return
        _lark_login_gate(dev_mock=dev_mock)  # 未登录 → 飞书登录引导并 st.stop()，不落密码框
        return
    if dev_mock:                  # 本地：飞书没配但开了 mock → 直接给模拟登录页
        _lark_login_gate(dev_mock=True)
        return
    _login_form()


def is_admin() -> bool:
    return st.session_state.get("__role") == "admin"


def require_admin() -> None:
    """全开放（JO 2026-06-06）：操作页不再限 admin，仅需登录即可。

    安全模型转为「飞书门禁挡外人 + 团队内部全开放」——入口靠飞书应用「可用范围」，
    进来的团队成员都能用全部页面（含数据导入 / 定義原価编辑 / 发注 / 上传等）。
    角色体系（admin/guest by ADMIN_LARK_EMAILS）保留但不再 gate 页面；
    将来要收紧某页，恢复 `if not is_admin(): st.stop()` 即可。
    """
    require_password()


def show_role_badge() -> None:
    """已废弃：保留空实现兼容主入口已有调用。"""
    return


def require_extra_password(scope: str, env_var: str, default: str = "") -> None:
    """全开放（JO 2026-06-06）：二级密码已取消，直接放行。

    原为数据导入（page27）/ 物流上传（page29）的二级确认（防误操作覆盖 PG）。
    团队内部全开放后去掉。将来要恢复：删掉下面的 return、还原二级密码表单逻辑。
    """
    return
