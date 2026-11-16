"""模块 #49 · 🧾 采购处理（采购）（Boss 2026-11-16「采购方面，新品采购处理和日常补货处理，分这两个方面。然后可以共同看到货追踪」）.

  🆕 新品采购处理 / 🔁 日常补货处理  各 📋 待确认（既定の仕入先・起批量・金額 → 確認 / 取消）｜ 📦 发注书（NST CSV・出した時点で記録）
  🚚 到货跟踪                       全員の申請（下一步谁做つき）・退回待发注・取消
申請は page48「📝 订货申请」。部品は shared/order_request_ui、ロジックは shared/new_item_request。
"""
from __future__ import annotations

import streamlit as st

from shared import order_request_ui as UI
from shared.auth import require_password
from shared.i18n import lang_selector, t
from shared.theme import inject_theme

st.set_page_config(page_title=t("采购处理"), page_icon="🧾", layout="wide")
require_password()
inject_theme()
lang_selector()
st.title(t("🧾 采购处理"))
st.caption(t("运营在「📝 订货申请」提的申请，采购在这里确认、生成发注书（新品 / 日常补货分开处理）。到货跟踪两边共用"))

ctx = UI.load_ctx()
UI.flash()
UI.alerts(ctx)


def _open(kind: str) -> int:
    return len(ctx.state("待确认", kind)) + len(ctx.state("待发注", kind))


tab_new, tab_re, tab_trk = st.tabs([t("🆕 新品采购处理（{n}）").format(n=_open("new")),
                                    t("🔁 日常补货处理（{n}）").format(n=_open("restock")), t("🚚 到货跟踪")])
for tab, kind in ((tab_new, "new"), (tab_re, "restock")):
    with tab:
        UI.metrics(ctx, kind)
        conf, po = st.tabs([t("📋 待确认（{n}）").format(n=len(ctx.state("待确认", kind))),
                            t("📦 发注书（{n}）").format(n=len(ctx.state("待发注", kind)))])
        with conf:
            UI.render_confirm(ctx, kind)
        with po:
            UI.render_po(ctx, kind)
with tab_trk:
    UI.render_tracking(ctx, ops=False)
