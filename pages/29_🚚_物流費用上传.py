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


tab1, tab2, tab3 = st.tabs([
    t("① JD 請求書（費用）"),
    t("② BM（包裹号→店舗）"),
    t("③ 店舗→部署 分類"),
])

# ============================================================
# tab1 · JD 請求書
# ============================================================
with tab1:
    st.markdown(t("##### JD 請求書（三金商事）.xlsx を上传"))
    st.caption(t("OB-Pick&Pack=梱包費用 / Last mile=国内運送費用 / Packing Charge=梱包材 の 3 sheet を取込（不含税）。"))
    c1, c2 = st.columns([1, 2])
    ym_in = c1.text_input(t("対象月 YYYY-MM"), placeholder="2026-02")
    up = c2.file_uploader(t("請求書 xlsx"), type=["xlsx"], key="inv_up")
    ym_ok = bool(re.fullmatch(r"20\d{2}-\d{2}", ym_in or ""))
    if up and not ym_ok:
        st.info(t("対象月を YYYY-MM 形式で入力してください（例 2026-02）。"))
    if up and ym_ok and st.button(t("💾 解析 → PG 書込 + 再集計"), key="inv_btn", type="primary"):
        with st.spinner(t("解析中…")):
            rows, per = parse_invoice(up.getvalue(), ym_in)
        if not rows:
            st.error(t("取込行 0 — sheet 名/表頭を確認してください。"))
        else:
            with st.spinner(t("PG 書込 + 再集計中…")):
                for ct in {r[1] for r in rows}:
                    conn.execute(
                        "DELETE FROM logistics.cost_invoice_raw WHERE year_month=%s AND cost_type=%s",
                        (ym_in, ct),
                    )
                conn.executemany(
                    """INSERT INTO logistics.cost_invoice_raw
                       (year_month, cost_type, join_key, amount_ex_tax, amount_in_tax,
                        material_cd, material_qty, sku, cost_date)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )
                conn.commit()
                run_recompute(ym_in)
            st.success(t("✅ {ym}: ").format(ym=ym_in)
                       + " ".join(f"{k}={v}" for k, v in per.items())
                       + t(" 行 書込 + 再集計完了"))
            show_match_feedback()

# ============================================================
# tab2 · BM
# ============================================================
with tab2:
    st.markdown(t("##### BM 包裹导出 .xlsx を上传（包裹号 → 店舗）"))
    st.caption(t("NST 拉取不可 · 平台/JD WMS から導出。包裹号/订单号/物流单号/店铺 を表頭名で自動認識。"))
    st.warning(t(
        "⚠️ BM は**対象月と同期 × 全平台**で導出してください"
        "（国内: 楽天/Amazon/Yahoo/Temu/TikTok ＋ 海外: Shopee/Lazada 等）。"
        "海外平台のみだと国内運送費(Last mile)が店舗に紐付かず【不明】が激増します。"
    ))
    upb = st.file_uploader(t("BM xlsx"), type=["xlsx"], key="bm_up")
    if upb and st.button(t("💾 解析 → PG 書込 + 全月再集計"), key="bm_btn", type="primary"):
        with st.spinner(t("解析中…")):
            bm_rows = parse_bm(upb.getvalue())
        if not bm_rows:
            st.error(t("包裹号 列が見つからない / 取込 0 件。"))
        else:
            shops = sorted({r[4] for r in bm_rows if r[4]})
            with st.spinner(t("PG upsert + 再集計中…")):
                conn.executemany(
                    """INSERT INTO logistics.order_shop_map
                       (parcel_no, order_id, waybill_no, platform, shop, ship_date)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (parcel_no) DO UPDATE SET
                         order_id=EXCLUDED.order_id, waybill_no=EXCLUDED.waybill_no,
                         platform=EXCLUDED.platform, shop=EXCLUDED.shop,
                         ship_date=EXCLUDED.ship_date, imported_at=now()""",
                    bm_rows,
                )
                conn.commit()
                run_recompute(None)
            st.success(t("✅ {n} 包裹 upsert / {s} 店舗 · 全月再集計完了")
                       .format(n=len(bm_rows), s=len(shops)))
            show_match_feedback()

# ============================================================
# tab3 · 店舗 → 部署 分類
# ============================================================
with tab3:
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
