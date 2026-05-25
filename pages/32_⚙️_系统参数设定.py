"""模块 #32 系统参数设定 · 运营建议阈值等可调参数（系统设置·二级密码保护）。

阈值持久化于 data/files（modules.operation_advice.settings）。改后到「💡 运营调整建议」
点【🔄 重新计算】生效。
"""
from __future__ import annotations

import streamlit as st
from shared.i18n import t, lang_selector

st.set_page_config(page_title=t("系统参数设定"), page_icon="⚙️", layout="wide")
from shared.auth import require_password, require_extra_password
require_password()
require_extra_password("sys", "SYS_SETTINGS_PW", default="1001")  # 系统设置二级密码（默认 1001·可 env 覆盖）
from shared.theme import inject_theme
inject_theme()
lang_selector()

from modules.operation_advice.settings import load_thresholds, save_thresholds

st.title(t("⚙️ 系统参数设定"))
st.caption(t("仅授权人员可改 · 影响运营调整建议等模块的判定阈值"))

st.subheader(t("运营调整建议 · 双轴阈值"))
st.caption(t("毛利率(%) × 月周转率 → 5 档建议的分界线"))

_th = load_thresholds()
c1, c2, c3, c4 = st.columns(4)
_ml = c1.number_input(t("毛利 低界 (%)"), value=float(_th["margin_low"]), step=1.0, min_value=0.0)
_mh = c2.number_input(t("毛利 高界 (%)"), value=float(_th["margin_high"]), step=1.0, min_value=0.0)
_tl = c3.number_input(t("周转 低界"), value=float(_th["turn_low"]), step=0.1, min_value=0.0, format="%.2f")
_thh = c4.number_input(t("周转 高界"), value=float(_th["turn_high"]), step=0.1, min_value=0.0, format="%.2f")

if st.button(t("💾 保存阈值"), type="primary"):
    if _ml >= _mh:
        st.error(t("毛利低界必须 < 高界"))
    elif _tl >= _thh:
        st.error(t("周转低界必须 < 高界"))
    else:
        save_thresholds({"margin_low": _ml, "margin_high": _mh,
                         "turn_low": _tl, "turn_high": _thh})
        st.success(t("✅ 已保存。到「💡 运营调整建议」点【🔄 重新计算】生效。"))

st.divider()
st.caption(
    t("当前生效阈值") + f"：毛利 {_th['margin_low']:.0f}/{_th['margin_high']:.0f}% · "
    f"周转 {_th['turn_low']}/{_th['turn_high']}"
)
