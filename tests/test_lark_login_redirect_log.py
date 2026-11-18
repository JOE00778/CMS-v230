"""飞书登录：自动跳转在主文档里执行（components iframe 无 allow-top-navigation，从未生效）·
登录结果写日志（成功 / state 不符 / 换 code 失败 + 是否飞书客户端），不记 code・token・姓名（Boss 2026-11-18「执行」）。"""
import logging
from types import SimpleNamespace

import pytest

from shared import auth, lark_auth

LARK_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Lark/7.30.8 LarkLocale/zh_CN"


class _QP(dict):
    def clear(self):
        super().clear()


def _run(monkeypatch, qp, cookies, ua, user=True):
    for k, v in {"LARK_APP_ID": "cli_x", "LARK_APP_SECRET": "s", "LARK_REDIRECT_URI": "https://cms"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lark_auth, "st", SimpleNamespace(query_params=_QP(qp), secrets={},
                                                         context=SimpleNamespace(cookies=cookies, headers={"User-Agent": ua})))
    monkeypatch.setattr(lark_auth, "exchange_code_for_user", lambda code: {"name": "徐越", "union_id": "on_1"} if user else None)
    return lark_auth.try_handle_oauth_callback()


@pytest.mark.parametrize("qp,cookies,ua,user,want", [
    ({"code": "secretcode", "state": "cms-a"}, {"cms_lark_state": "cms-a"}, LARK_UA, True, "result=ok client=lark"),
    ({"code": "secretcode", "state": "cms-a"}, {}, "Mozilla/5.0 Chrome/131", True, "result=state_mismatch client=browser cookie=missing"),
    ({"code": "secretcode", "state": "cms-a"}, {"cms_lark_state": "cms-b"}, LARK_UA, True, "result=state_mismatch client=lark cookie=differs"),
    ({"code": "secretcode", "state": "cms-a"}, {"cms_lark_state": "cms-a"}, LARK_UA, False, "result=exchange_failed client=lark"),
])
def test_login_outcome_is_logged_without_secrets(monkeypatch, caplog, qp, cookies, ua, user, want):
    with caplog.at_level(logging.INFO, logger="cms.auth"):
        _run(monkeypatch, qp, cookies, ua, user)
    msgs = [r.getMessage() for r in caplog.records if r.name == "cms.auth"]
    assert msgs and want in msgs[-1], msgs
    assert not any(s in m for m in msgs for s in ("secretcode", "cms-a", "徐越", "on_1"))


def test_auto_redirect_runs_in_main_document(monkeypatch):
    """自动跳转用 st.html(unsafe_allow_javascript=True)（主文档执行）；失败页（URL 带 code）不跳。"""
    calls = []
    fake = SimpleNamespace(
        markdown=lambda *a, **k: None, error=lambda *a, **k: None,
        html=lambda body, **k: calls.append(("html", body, k)),
        query_params=_QP(), stop=lambda: (_ for _ in ()).throw(SystemExit),
        columns=lambda n: [SimpleNamespace(button=lambda *a, **k: False)] * n)
    monkeypatch.setattr(auth, "st", fake)
    monkeypatch.setattr(auth, "_lark_sso_enabled", lambda: True)
    monkeypatch.setattr(lark_auth, "build_login_url", lambda s: f"https://lark/authorize?state={s}")
    import streamlit.components.v1 as components
    monkeypatch.setattr(components, "html", lambda *a, **k: calls.append(("iframe", a, k)))
    with pytest.raises(SystemExit):
        auth._lark_login_gate()
    js = [c for c in calls if c[0] == "html" and c[2].get("unsafe_allow_javascript")]
    assert js and "window.location" in js[0][1] and "cms_lark_state=" in js[0][1]
    assert js[0][1].index("cms_lark_state=") < js[0][1].index("window.location")   # 先写 cookie 再跳
    calls.clear()
    fake.query_params = _QP({"code": "x"})
    with pytest.raises(SystemExit):
        auth._lark_login_gate()
    assert not any("window.location" in str(c) for c in calls)
