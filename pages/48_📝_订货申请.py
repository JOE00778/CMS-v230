"""模块 #48 · 📝 订货申请（运营）（Boss 2026-11-13 新品订货申请 → 2026-11-16「A 加一个日常补货申请，这块并入到新品订货申请中，
整体改为订货申请，其中按照新品订和日常补这个 tab 来区分…然后可以共同看到货追踪」）.

  🆕 新品订货 / 🔁 日常补货  JAN を貼る or Excel 一括 → ② 画像・商品名・単価・起批量を見て数量 → 申請（日常补货は NST 登録済みだけ）
  🚚 到货跟踪               既定は自分の申請だけ（下一步谁做つき）・自分の待确认は撤回できる
確認・発注書は page49「🧾 采购处理」。部品は shared/order_request_ui、ロジックは shared/new_item_request。
"""
from __future__ import annotations

import streamlit as st

from shared import new_item_request as NR
from shared import order_request_ui as UI
from shared.auth import require_password
from shared.i18n import lang_selector, t
from shared.theme import inject_theme

st.set_page_config(page_title=t("订货申请"), page_icon="📝", layout="wide")
require_password()
inject_theme()
lang_selector()
st.title(t("📝 订货申请"))
st.caption(t("运营在这里提申请（新品订货 / 日常补货）→ 采购在「🧾 采购处理」确认和生成发注书 → 两边都在「🚚 到货跟踪」看进度。"
             "没有入荷预定日的 PO 按发注日 + {d} 天算期限").format(d=NR.OVERDUE_DAYS))

ctx = UI.load_ctx()
UI.flash()
tab_new, tab_re, tab_trk = st.tabs([t("🆕 新品订货"), t("🔁 日常补货"), t("🚚 到货跟踪")])
with tab_new:
    UI.render_submit(ctx, "new")
with tab_re:
    UI.render_submit(ctx, "restock")
with tab_trk:
    UI.render_tracking(ctx, ops=True)
