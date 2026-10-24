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
st.caption(t("Shopify の注文番号 → コマーシャル・インボイス PDF（斑马は経由しない）"))

col_a, col_b, col_c, col_d = st.columns([2, 2, 1, 1])
order_no = col_a.text_input(t("Shopify 注文番号"), placeholder="1074")
awb = col_b.text_input(t("Air Waybill No.（任意）"),
                       help=t("斑马で出荷した後に分かる番号。空でも PDF は出る"))
pkgs = col_c.number_input(t("個口数"), min_value=1, value=1, step=1)
gross = col_d.number_input(t("総重量 kg（0 なら正味合計）"), min_value=0.0,
                           value=0.0, step=0.01, format="%.2f")

import re  # noqa: E402

if st.button(t("注文を読み込む"), type="primary", disabled=not order_no):
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
    titles = [li.get("title") or "" for li in items]
    st.session_state.pop("ci_pdf", None)
    st.session_state["ci"] = {"order": order, "jans": jans, "warn": warn,
                              "lines": lines, "titles": titles, "no": order_no}

ci = st.session_state.get("ci")
if not ci:
    st.stop()
order, jans, lines = ci["order"], ci["jans"], ci["lines"]
WARN_TEXT = {
    "no_name": t("清関品名が無い"),
    "no_hs": t("HS コードが無い"),
    "no_origin": t("原産国が無い"),
    "no_weight": t("重量が無い"),
    "origin_conflict": t("原産国を JP で出力・要確認（ネット上の表記が割れている）"),
    "hs_flag": t("HS 要注意"),
}
warn_by_jan: dict[str, list[str]] = {}
for w in ci["warn"]:
    msg = WARN_TEXT[w["code"]] + (f": {t(w['arg'])}" if w.get("arg") else "")
    warn_by_jan.setdefault(w["jan"], []).append(msg)

st.subheader(t("明細（清関品名・HS・原産国・重量は直接直せる）"))
st.caption(t("赤字の「要確認」がある行を確認・補完してから PDF を作る。"
             "「PG にも保存」にチェックすると次回から自動で入る。"))
base = pd.DataFrame([{
    "JAN": j,
    t("商品名"): tt,
    t("清関品名"): l.description,
    t("数量"): l.units,
    "HS": l.hs_code or "",
    t("原産国"): l.origin or "",
    t("正味 g/個"): round(l.net_weight_kg * 1000, 1) if l.net_weight_kg else None,
    t("単価"): l.unit_value,
    t("要確認"): " / ".join(warn_by_jan.get(j, [])),
} for j, l, tt in zip(jans, lines, ci["titles"])])
edited = st.data_editor(
    base, hide_index=True, use_container_width=True, key=f"ed_{ci['no']}",
    disabled=["JAN", t("商品名"), t("数量"), t("単価"), t("要確認")],
    column_config={
        "HS": st.column_config.TextColumn(help=t("6〜10 桁の数字")),
        t("原産国"): st.column_config.TextColumn(help=t("ISO 2 文字（JP / CN / KR …）"), max_chars=2),
        t("正味 g/個"): st.column_config.NumberColumn(min_value=0.0, step=1.0),
    })

# 入力チェック（間違った値で PDF を出さない）
bad = []
for _, r in edited.iterrows():
    hs, cc, wg = str(r["HS"] or "").strip(), str(r[t("原産国")] or "").strip().upper(), r[t("正味 g/個")]
    if not str(r[t("清関品名")] or "").strip():
        bad.append(f"{r['JAN']}: " + t("清関品名が空"))
    if not re.fullmatch(r"\d{6,10}", hs):
        bad.append(f"{r['JAN']}: " + t("HS が 6〜10 桁の数字でない") + f"（{hs or '-'}）")
    if not re.fullmatch(r"[A-Z]{2}", cc):
        bad.append(f"{r['JAN']}: " + t("原産国が 2 文字コードでない") + f"（{cc or '-'}）")
    if not wg or pd.isna(wg) or wg <= 0:
        bad.append(f"{r['JAN']}: " + t("重量が空"))
if bad:
    st.error(t("このままでは PDF を出せない：") + "\n\n" + "\n\n".join("· " + b for b in bad))

new_lines = []
for l, (_, r) in zip(lines, edited.iterrows()):
    w = r[t("正味 g/個")]
    new_lines.append(type(l)(description=str(r[t("清関品名")] or "").strip(), units=l.units,
                             net_weight_kg=round(float(w) / 1000, 3) if w and not pd.isna(w) else 0.0,
                             hs_code=str(r["HS"] or "").strip(),
                             origin=str(r[t("原産国")] or "").strip().upper(),
                             unit_value=l.unit_value))
doc = so.to_ci_doc(order, new_lines, awb=awb.strip(), total_packages=int(pkgs),
                   total_gross_kg=float(gross) or None)

st.write(" / ".join(x for x in (
    doc.consignee_name, *doc.consignee_addr, doc.consignee_city,
    doc.consignee_country, doc.consignee_tel) if x))
c1, c2, c3 = st.columns(3)
c1.metric(t("ユニット数合計"), f"{doc.total_units:.0f}")
c2.metric(t("正味重量合計 kg"), f"{doc.total_net_kg:.2f}")
c3.metric(t("インボイス合計"), f"{doc.invoice_total:.2f} {order.get('currencyCode', '')}")

save = st.checkbox(t("直した値を PG にも保存する（次回から自動で入る）"), value=True)
if st.button(t("PDF を作る"), type="primary", disabled=bool(bad)):
    if save:
        changed = []
        for (_, b), (_, e) in zip(base.iterrows(), edited.iterrows()):
            row = {"jan": e["JAN"]}
            if str(e[t("清関品名")]).strip() != str(b[t("清関品名")]).strip():
                row["customs_name_en"] = str(e[t("清関品名")]).strip()
            if str(e["HS"]).strip() != str(b["HS"]).strip():
                row["hs_code"] = str(e["HS"]).strip()
            if str(e[t("原産国")]).strip().upper() != str(b[t("原産国")]).strip().upper():
                row["origin_country"] = str(e[t("原産国")]).strip().upper()
            if e[t("正味 g/個")] != b[t("正味 g/個")] and not pd.isna(e[t("正味 g/個")]):
                row["net_weight_g"] = float(e[t("正味 g/個")])
            if len(row) > 1 and row["jan"]:
                changed.append(row)
        if changed:
            from shared.db import get_connection
            conn = get_connection()
            try:
                n = so.save_manual(conn, changed)
            finally:
                conn.close()
            st.success(t("PG に保存：") + f"{n} " + t("件"))
    st.session_state["ci_pdf"] = build(doc)

if st.session_state.get("ci_pdf"):
    st.download_button(
        t("PDF をダウンロード"), data=st.session_state["ci_pdf"],
        file_name=f"CI_{so.normalize_order_no(ci['no']).lstrip('#')}.pdf",
        mime="application/pdf", type="primary")
