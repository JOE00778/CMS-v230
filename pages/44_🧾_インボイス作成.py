"""模块 · インボイス作成（Boss 2026-10-23）。

Shopify の注文番号を入れてボタンを押すと、FedEx のコマーシャル・インボイス
（CI）PDF が出る。**斑马には一切触らない**（Boss「斑马データを通さなくてもよい」）。

データの出どころ:
  · Shopify Admin GraphQL（読み取りのみ）… 宛先・明細・数量・金額・JAN
  · PG `logistics.item_customs`            … HS コード・原産国・重量
  雛形 PDF は shared/assets/fedex_ci_template.pdf（FedEx 公式フォームから
  注文ごとに変わる値だけを抜いたもの）。輸出者ブロックは雛形に焼き込み済み。

HS・原産国・重量が欠けている行は**勝手に埋めず**赤で出す。そのまま出すと
通関で止まるので、PG 側を直してから出し直す。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("インボイス作成"), page_icon="🧾", layout="wide")
from shared.auth import require_password  # noqa: E402

require_password()
from shared.theme import inject_theme  # noqa: E402

inject_theme()
lang_selector()

from shared import shopify_order as so  # noqa: E402
from shared.invoice_ci import build  # noqa: E402

st.title(t("🧾 インボイス作成"))
st.caption(t("Shopify の注文番号 → FedEx コマーシャル・インボイス PDF（斑马は経由しない）"))

col_a, col_b, col_c, col_d = st.columns([2, 2, 1, 1])
order_no = col_a.text_input(t("Shopify 注文番号"), placeholder="1074")
awb = col_b.text_input(t("Air Waybill No.（任意）"),
                       help=t("斑马で出荷した後に分かる番号。空でも PDF は出る"))
pkgs = col_c.number_input(t("個口数"), min_value=1, value=1, step=1)
gross = col_d.number_input(t("総重量 kg（0 なら正味合計）"), min_value=0.0,
                           value=0.0, step=0.01, format="%.2f")

if not st.button(t("インボイスを作る"), type="primary", disabled=not order_no):
    st.stop()

try:
    order = so.fetch_order(order_no)
except so.ShopifyNotConfigured as e:
    st.error(t("Shopify 未配置：") + str(e))
    st.stop()
except Exception as e:  # noqa: BLE001 — 通信・権限・GraphQL すべてここで見せる
    st.error(f"{t('Shopify 取得に失敗')}: {e}")
    st.stop()

if not order:
    st.warning(t("その注文番号は見つからなかった：") + so.normalize_order_no(order_no))
    st.stop()

items = (order.get("lineItems") or {}).get("nodes") or []
jans = [((li.get("variant") or {}).get("barcode") or li.get("sku") or "").strip()
        for li in items]
with get_readonly_connection() as conn:
    customs = so.customs_by_jan(conn, jans)

lines, warn = so.to_ci_lines(order, customs)
doc = so.to_ci_doc(order, lines, awb=awb.strip(), total_packages=int(pkgs),
                   total_gross_kg=float(gross) or None)

st.subheader(t("荷受人"))
st.write(" / ".join(x for x in (
    doc.consignee_name, *doc.consignee_addr, doc.consignee_city,
    doc.consignee_country, doc.consignee_tel) if x))

st.subheader(t("明細"))
st.dataframe(pd.DataFrame([{
    "JAN": j,
    t("品名"): l.description,
    t("数量"): l.units,
    "HS": l.hs_code or "—",
    t("原産国"): l.origin or "—",
    t("正味 kg"): l.net_weight_kg or "—",
    t("単価"): l.unit_value,
    t("金額"): l.total_value,
} for j, l in zip(jans, lines)]), use_container_width=True, hide_index=True)

c1, c2, c3 = st.columns(3)
c1.metric(t("ユニット数合計"), f"{doc.total_units:.0f}")
c2.metric(t("正味重量合計 kg"), f"{doc.total_net_kg:.2f}")
c3.metric(t("インボイス合計"), f"{doc.invoice_total:.2f} {order.get('currencyCode', '')}")

if warn:
    st.error(t("欠けている項目（PG `logistics.item_customs` を直してから出し直す）："))
    for w in warn:
        st.write("· " + w)

pdf = build(doc)
st.download_button(
    t("PDF をダウンロード"), data=pdf,
    file_name=f"CI_{so.normalize_order_no(order_no).lstrip('#')}.pdf",
    mime="application/pdf", type="primary")
