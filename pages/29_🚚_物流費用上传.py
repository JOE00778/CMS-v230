"""模块 #29 物流費用上传 · JD 請求書 + BM(包裹号→店舗) を上传 → 配賦集計。

Boss 指示:
- 費用請求は上传のみで録入（NST 拉取不可）。
- 订单→店舗 マッピング(BM) も NST 拉取不可 → Boss が平台/JD WMS から導出 upload。
- 包裹号で紐付かない / 店舗が部署未分類 → 【不明】に集計（後で確認）。
- 列は表頭名で解決（JD 請求書/BM は列順不定）。費用は不含税。
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

import openpyxl
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("物流費用上传"), page_icon="🚚", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🚚 物流費用上传"))
st.caption(t(
    "JD 請求書（費用明细）と BM（包裹号→店舗）をアップロード → 自動で配賦集計。"
    "費用は不含税 · 紐付かない分は【不明】。結果は「🚚 物流費用分析」で確認。"
))

INV_SHEETS = {
    "OB-Pick&Pack":   ("pickpack", ("客户出库单号",)),
    "Last mile":      ("lastmile", ("客户运单编码",)),
    "Packing Charge": ("packing",  ("客户出库单编码",)),
}


def find_col(hdr, *kw, exclude=None):
    for i, h in enumerate(hdr):
        if not h:
            continue
        s = str(h)
        if any(k in s for k in kw) and (exclude is None or exclude not in s):
            return i
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", v)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
    return None


def _cell(r, c):
    v = r[c] if (c is not None and c < len(r)) else None
    return v


def parse_invoice(data: bytes, ym: str):
    # 普通模式（請求書/导出 xlsx は dimension 欠落で read_only 不可の場合あり）
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    rows, per = [], {}
    for sheet, (ct, joinkw) in INV_SHEETS.items():
        if sheet not in wb.sheetnames:
            continue
        it = wb[sheet].iter_rows(values_only=True)
        next(it, None)
        hdr = list(next(it, []))
        cj = find_col(hdr, *joinkw)
        ce = find_col(hdr, "不含税")
        ci = find_col(hdr, "含税金额", exclude="不含税")
        cd = find_col(hdr, "费用发生时间", "費用発生")
        cs = find_col(hdr, "商品编号", "SKU") if ct == "pickpack" else None
        cmc = find_col(hdr, "耗材编码") if ct == "packing" else None
        cmq = find_col(hdr, "耗材数量") if ct == "packing" else None
        n = 0
        for r in it:
            jk = _cell(r, cj)
            amt = _num(_cell(r, ce))
            if jk is None and amt is None:
                continue
            rows.append((
                ym, ct,
                str(jk).strip() if jk is not None else None,
                amt, _num(_cell(r, ci)),
                (str(_cell(r, cmc)).strip() if _cell(r, cmc) else None),
                _num(_cell(r, cmq)),
                (str(_cell(r, cs)).strip() if _cell(r, cs) else None),
                _to_date(_cell(r, cd)),
            ))
            n += 1
        per[ct] = n
    wb.close()
    return rows, per


def parse_bm(data: bytes):
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = None
    for s in wb.worksheets:
        h = list(next(s.iter_rows(values_only=True, max_row=1), []))
        if find_col(h, "包裹号", "包裹號") is not None:
            ws = s
            break
    if ws is None:
        wb.close()
        return []
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))
    cp = find_col(hdr, "包裹号", "包裹號")
    co = find_col(hdr, "订单号", "訂単")
    cw = find_col(hdr, "物流单号", "物流單号")
    cpl = find_col(hdr, "平台")
    csh = find_col(hdr, "店铺", "店舗")
    cdt = find_col(hdr, "发货时间", "發貨", "出荷")
    seen = {}
    for r in it:
        pk = _cell(r, cp)
        if not pk:
            continue
        key = str(pk).strip()
        seen[key] = (
            key,
            str(_cell(r, co)).strip() if _cell(r, co) else None,
            str(_cell(r, cw)).strip() if _cell(r, cw) else None,
            str(_cell(r, cpl)).strip() if _cell(r, cpl) else None,
            str(_cell(r, csh)).strip() if _cell(r, csh) else None,
            _to_date(_cell(r, cdt)),
        )
    wb.close()
    return list(seen.values())


def parse_billing(data: bytes, ym: str):
    """Billing sheet（JD 公式の費用全構成）→ (ym, seq, item_name, ex, in)。Total 行で停止。"""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    if "Billing" not in wb.sheetnames:
        wb.close()
        return []
    rows = list(wb["Billing"].iter_rows(values_only=True))
    hdr_idx = next((i for i, r in enumerate(rows)
                    if any(str(c).strip() == "Item" for c in r if c)
                    and any("Price before Tax" in str(c) for c in r if c)), None)
    if hdr_idx is None:
        wb.close()
        return []
    hdr = [(str(x).strip() if x else "") for x in rows[hdr_idx]]
    ci_cust = find_col(hdr, "Customer")
    ci_item = find_col(hdr, "Item")
    ci_ex = find_col(hdr, "Price before Tax", "before Tax")
    ci_in = find_col(hdr, "Tax inclusive")
    out, seq = [], 0
    for r in rows[hdr_idx + 1:]:
        cust = str(_cell(r, ci_cust)).strip() if _cell(r, ci_cust) else ""
        item = str(_cell(r, ci_item)).strip() if _cell(r, ci_item) else ""
        if cust.lower() == "total" or item.lower() == "total":
            break
        if not item:
            continue
        ex, inc = _num(_cell(r, ci_ex)), _num(_cell(r, ci_in))
        if ex is None and inc is None:
            continue
        seq += 1
        out.append((ym, seq, item.replace(chr(10), " ").replace(chr(13), " "), ex, inc))
    wb.close()
    return out


def run_recompute(ym=None):
    conn.execute(
        "DELETE FROM logistics.cost_monthly WHERE (%s IS NULL OR year_month=%s)",
        (ym, ym),
    )
    conn.execute(
        """
        INSERT INTO logistics.cost_monthly
            (year_month, dept, shop, cost_type, amount, qty, source, computed_at)
        SELECT r.year_month,
               COALESCE(d.dept, '不明'),
               COALESCE(m.shop, '(未マッチ包裹)'),
               r.cost_type,
               SUM(r.amount_ex_tax), COUNT(*), 'recompute', now()
        FROM logistics.cost_invoice_raw r
        LEFT JOIN logistics.order_shop_map m ON r.join_key = m.parcel_no
        LEFT JOIN logistics.shop_dept_map  d ON m.shop = d.shop
        WHERE (%s IS NULL OR r.year_month = %s)
        GROUP BY r.year_month, COALESCE(d.dept, '不明'),
                 COALESCE(m.shop, '(未マッチ包裹)'), r.cost_type
        """,
        (ym, ym),
    )
    conn.commit()


def show_match_feedback():
    rs = conn.execute(
        """SELECT r.year_month AS ym,
                  round(100.0*count(m.parcel_no)/count(*),1) AS match_pct,
                  round(coalesce(sum(r.amount_ex_tax) FILTER (WHERE m.parcel_no IS NULL),0))::bigint AS unknown_amt,
                  round(100.0*coalesce(sum(r.amount_ex_tax) FILTER (WHERE m.parcel_no IS NULL),0)/nullif(sum(r.amount_ex_tax),0),1) AS unknown_pct
           FROM logistics.cost_invoice_raw r
           LEFT JOIN logistics.order_shop_map m ON r.join_key = m.parcel_no
           GROUP BY r.year_month ORDER BY r.year_month"""
    ).fetchall()
    df = pd.DataFrame([dict(x) for x in rs])
    if df.empty:
        return
    df = df.rename(columns={"ym": t("月"), "match_pct": t("件数命中%"),
                            "unknown_amt": t("不明額(¥)"), "unknown_pct": t("不明率%")})
    st.markdown(t("###### 📊 各月 紐付状況（不明率が高い月 = 同期・全平台 BM の補完が必要）"))
    st.dataframe(df, hide_index=True, use_container_width=True,
                 column_config={t("不明額(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})


def detect_kind(data: bytes, name: str):
    """ファイル種別と対象月を自動判定（請求書=費用sheet有/月はファイル名、BM=包裹号列有）。"""
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    sheets = set(wb.sheetnames)
    wb.close()
    if sheets & {"OB-Pick&Pack", "Last mile", "Packing Charge", "Billing"}:
        m = re.search(r"(20\d{2})\D?(\d{2})", name)
        return "invoice", (f"{m[1]}-{m[2]}" if m else None)
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)  # 包裹导出は read_only 不可
    is_bm = any(find_col(list(next(s.iter_rows(values_only=True, max_row=1), [])), "包裹号", "包裹號") is not None
                for s in wb.worksheets)
    wb.close()
    return ("bm", None) if is_bm else ("unknown", None)


tab1, tab2 = st.tabs([
    t("📤 一括アップロード（請求書 + BM）"),
    t("店舗→部署 分類"),
])

# ============================================================
# tab1 · 一括アップロード（請求書 + BM 自動判定）
# ============================================================
with tab1:
    st.markdown(t("##### 請求書 + BM をまとめてアップロード（複数可・種別と月を自動判定）"))
    st.caption(t("請求書: OB-Pick&Pack/Last mile/Packing/Billing を取込（不含税）· 月はファイル名の YYYYMM。"
                 "BM: 包裹号→店舗。両方まとめて選んで一括処理。"))
    st.warning(t(
        "⚠️ BM は**対象月と同期 × 全平台**で導出（国内: 楽天/Amazon/Yahoo/Temu/TikTok ＋ 海外: Shopee/Lazada）。"
        "海外のみだと国内運送費(Last mile)が紐付かず【不明】激増。"
    ))
    ups = st.file_uploader(t("xlsx（複数選択可）"), type=["xlsx"],
                           accept_multiple_files=True, key="batch_up")
    if ups and st.button(t("💾 一括解析 → PG 書込 + 再集計"), key="batch_btn", type="primary"):
        inv_log, bm_log, err_log = [], [], []
        prog = st.progress(0.0, text=t("解析中…"))
        for i, up in enumerate(ups):
            data = up.getvalue()
            try:
                kind, ym = detect_kind(data, up.name)
                if kind == "invoice" and not ym:
                    err_log.append(t("❌ {f}: 月份識別不可（名前に YYYYMM 無し）").format(f=up.name))
                elif kind == "invoice":
                    rows, per = parse_invoice(data, ym)
                    if not rows:
                        err_log.append(t("❌ {f}: 取込0行").format(f=up.name))
                    else:
                        for ct in {r[1] for r in rows}:
                            conn.execute("DELETE FROM logistics.cost_invoice_raw WHERE year_month=%s AND cost_type=%s", (ym, ct))
                        conn.executemany(
                            """INSERT INTO logistics.cost_invoice_raw
                               (year_month, cost_type, join_key, amount_ex_tax, amount_in_tax,
                                material_cd, material_qty, sku, cost_date)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
                        bill = parse_billing(data, ym)
                        if bill:
                            conn.execute("DELETE FROM logistics.cost_billing WHERE year_month=%s", (ym,))
                            conn.executemany(
                                """INSERT INTO logistics.cost_billing
                                   (year_month, seq, item_name, amount_ex_tax, amount_in_tax)
                                   VALUES (%s,%s,%s,%s,%s)""", bill)
                        conn.commit()
                        inv_log.append(t("✅ 請求書 {ym}: ").format(ym=ym)
                                       + " ".join(f"{k}={v}" for k, v in per.items())
                                       + (f" +Billing{len(bill)}" if bill else ""))
                elif kind == "bm":
                    bm_rows = parse_bm(data)
                    if not bm_rows:
                        err_log.append(t("❌ {f}: 包裹0件").format(f=up.name))
                    else:
                        conn.executemany(
                            """INSERT INTO logistics.order_shop_map
                               (parcel_no, order_id, waybill_no, platform, shop, ship_date)
                               VALUES (%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (parcel_no) DO UPDATE SET
                                 order_id=EXCLUDED.order_id, waybill_no=EXCLUDED.waybill_no,
                                 platform=EXCLUDED.platform, shop=EXCLUDED.shop,
                                 ship_date=EXCLUDED.ship_date, imported_at=now()""", bm_rows)
                        conn.commit()
                        bm_log.append(t("✅ BM {f}: {n}包裹 / {s}店舗").format(
                            f=up.name, n=len(bm_rows), s=len({r[4] for r in bm_rows if r[4]})))
                else:
                    err_log.append(t("❌ {f}: 種別判定不可（請求書/BM どちらでもない）").format(f=up.name))
            except Exception as e:
                err_log.append(f"❌ {up.name}: {e}")
            prog.progress((i + 1) / len(ups), text=f"{i + 1}/{len(ups)}")
        with st.spinner(t("全月 再集計中…")):
            run_recompute(None)
        if inv_log:
            st.success(t("請求書 {n} 件").format(n=len(inv_log)) + "\n\n" + "\n\n".join(inv_log))
        if bm_log:
            st.success(t("BM {n} 件").format(n=len(bm_log)) + "\n\n" + "\n\n".join(bm_log))
        if err_log:
            st.warning("\n\n".join(err_log))
        show_match_feedback()

# ============================================================
# tab2 · 店舗 → 部署 分類
# ============================================================
with tab2:
    st.markdown(t("##### 店舗 → 部署（輸出 / EC）"))
    st.caption(t("未分類の店舗は【不明】に集計される。分類 → 保存 → 再集計で輸出/ECに反映。"))

    unknown = pd.DataFrame([dict(r) for r in conn.execute(
        """SELECT DISTINCT m.shop AS shop
           FROM logistics.order_shop_map m
           LEFT JOIN logistics.shop_dept_map d ON m.shop = d.shop
           WHERE d.shop IS NULL AND m.shop IS NOT NULL
           ORDER BY m.shop"""
    ).fetchall()])

    if not unknown.empty:
        st.warning(t("⚠️ 未分類 {n} 店舗（現在【不明】に集計中）").format(n=len(unknown)))
        unknown["dept"] = "輸出"
        edited_new = st.data_editor(
            unknown, hide_index=True, key="new_dept",
            column_config={
                "shop": st.column_config.TextColumn(t("店舗"), disabled=True),
                "dept": st.column_config.SelectboxColumn(t("部署"), options=["輸出", "EC"]),
            },
        )
        if st.button(t("➕ 登録 + 再集計"), key="new_dept_btn", type="primary"):
            conn.executemany(
                """INSERT INTO logistics.shop_dept_map (shop, dept) VALUES (%s,%s)
                   ON CONFLICT (shop) DO UPDATE SET dept=EXCLUDED.dept, updated_at=now()""",
                [(r["shop"], r["dept"]) for _, r in edited_new.iterrows()],
            )
            conn.commit()
            run_recompute(None)
            st.success(t("✅ 登録 + 再集計完了"))
            st.rerun()
    else:
        st.success(t("未分類店舗なし ✅"))

    st.divider()
    st.markdown(t("##### 既存マッピング"))
    cur_map = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT shop, dept FROM logistics.shop_dept_map ORDER BY dept, shop"
    ).fetchall()])
    if cur_map.empty:
        st.info(t("まだマッピングなし。上の未分類から登録してください。"))
    else:
        edited = st.data_editor(
            cur_map, hide_index=True, num_rows="dynamic", key="edit_dept",
            column_config={
                "shop": st.column_config.TextColumn(t("店舗")),
                "dept": st.column_config.SelectboxColumn(t("部署"), options=["輸出", "EC"]),
            },
        )
        if st.button(t("💾 保存 + 再集計"), key="edit_dept_btn"):
            conn.execute("DELETE FROM logistics.shop_dept_map")
            conn.executemany(
                """INSERT INTO logistics.shop_dept_map (shop, dept) VALUES (%s,%s)
                   ON CONFLICT (shop) DO UPDATE SET dept=EXCLUDED.dept, updated_at=now()""",
                [(r["shop"], r["dept"]) for _, r in edited.iterrows()
                 if r.get("shop") and r.get("dept")],
            )
            conn.commit()
            run_recompute(None)
            st.success(t("✅ 保存 + 再集計完了"))
            st.rerun()
