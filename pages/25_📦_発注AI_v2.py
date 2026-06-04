"""模块 #25 発注AI · 旧版（JD基线）+ v2（多仕入先決定版）合并

旧版（来自旧 page 08 · JD 库存基线）:
  A/B/C 档 + sold×倍率 + lot 圆整 → orders_available_based.csv

v2（Boss 2026-05-11 多仕入先決定版）:
  月販トレンド × zone 优先 × 単価最安 → NST 発注 CSV

板块（顶层 tabs）:
  📦 旧版（JD基线）       — 一键计算 + 按供应商分组 CSV
  🆕 v2（多仕入先決定版）
      📊 発注計算 / 🚚 仕入先 / 🏷️ 商品 / 🏭 品牌
"""
from __future__ import annotations

import io
import math
import re
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t
from shared.purchase_engine import compute_recommendations, DEFAULT_TREND_FACTORS

st.set_page_config(page_title=t("発注AI"), page_icon="📦", layout="wide")
from shared.auth import require_admin
require_admin()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📦 発注AI"))
st.caption(t(
    "📦 旧版（JD基线・page 08 から合并）+ 🆕 v2（多仕入先決定版・Boss 2026-05-11）— 上方 tab で切替"
))


# ============================================================
# zone ラベル / NST メタ（v2 用）
# ============================================================
ZONE_LABEL = {
    "JD_DIRECT": "🟢 JD直送", "BENTEN_TRANSIT": "🟡 弁天経由(+3%)",
    "EMERGENCY": "🟠 応急", "PREPAID": "🔴 前払い", "OTHER": "⚪ 他",
}
TREND_LABEL = {"up": "📈 上昇", "flat": "➡️ 横ばい", "down": "📉 下降"}

NST_SUPPLIERS = [
    "0020 エンパイヤ自動車株式会社（KONNGU'S）", "0025 株式会社オンダ", "0073 株式会社　エィチ・ケイ",
    "0077 大分共和株式会社", "0085 中央物産株式会社", "0197 大木化粧品株式会社", "0201 現金仕入れ",
    "0202 トラスコ中山株式会社", "0256 株式会社 グランジェ", "0258 株式会社 ファイン",
    "0343 株式会社森フォレスト", "0376 菅野株式会社", "0402 ハリマ共和物産株式会社",
    "0411 株式会社ラクーンコマース（スーパーデリバリー）", "0435 株式会社 流久商事", "0444 ハナモンワークス 合同会社",
    "0445 富森商事 株式会社", "0457 カネイシ株式会社", "0468 王子国際貿易株式会社", "0469 株式会社 新日配薬品",
    "0474 株式会社　五洲", "0476 カード仕入れ", "0479 スケーター株式会社", "0482 風雲商事株式会社",
    "0486 Maple International株式会社", "0490 NEW WIND株式会社", "0491 アプライド株式会社",
    "0504 京浜商事株式会社", "C000510 太田物産 株式会社",
]
EMPLOYEES = ["079 隋艶偉", "005 川崎里子", "037 米澤和敏", "043 徐越"]
DEPARTMENTS = ["輸出事業", "輸出事業 : 輸出（中国）"]


# ============================================================
# helpers（旧版：page 08 移植）
# ============================================================
def _normalize_jan(x):
    s = str(x).strip() if x is not None else ""
    if re.fullmatch(r"\d+(\.0+)?", s):
        return str(int(float(s)))
    return s


def _normalize_rank_base(rank: str) -> str:
    """ランク -> A/B/C/'' (TEST/NEW 归 '')."""
    if not rank:
        return ""
    r = str(rank)
    for k in ("A", "B", "C"):
        if k in r:
            return k
    return ""


def _df(sql: str) -> pd.DataFrame:
    from shared.db_helpers import df
    return df(conn, sql)


# ============================================================
# helpers（v2）
# ============================================================
def _has_supplier_quotes() -> bool:
    try:
        return conn.execute("SELECT COUNT(*) FROM supplier_quote").fetchone()[0] > 0
    except Exception:
        return False


def _ingest_supplier_file(file_bytes: bytes, filename: str) -> dict:
    """⚠️ Deprecated 2026-05-19：手动 Excel ingester 已移除（迁移至 PG/NST API 路径）。"""
    raise RuntimeError(
        "supplier_ingest 已弃用。仕入先报价更新请走 PG SQL 或 NST API（T-NST-001）路径。"
    )


def _build_nst_csv(df_sup: pd.DataFrame, *, nst_supplier: str, order_date: date,
                   employee: str, department: str, memo: str) -> pd.DataFrame:
    """page 10 と同形式の NST 発注 CSV (13 列)。1 行 = 1 SKU。"""
    eid = datetime.now().strftime("%Y%m%d%H%M%S")
    rows = []
    for _, r in df_sup.iterrows():
        qty = int(r["suggested_qty"])
        price = int(r["unit_price"])
        amount = qty * price
        rows.append({
            "外部ID": eid, "仕入先": nst_supplier, "日付": order_date.strftime("%Y/%m/%d"),
            "従業員": employee, "部門": department, "メモ": memo, "場所": "JD-物流-千葉",
            "アイテム": f"{r['jan']} {r['display_name']}".strip(),
            "数量": qty, "単価/率": price, "金額": amount, "税額": 0, "総額": amount,
        })
    return pd.DataFrame(rows, columns=[
        "外部ID", "仕入先", "日付", "従業員", "部門", "メモ", "場所",
        "アイテム", "数量", "単価/率", "金額", "税額", "総額",
    ])


# ============================================================
# 旧版（JD基线）主体 — page 08 line 60-282 移植，封装为函数
# ============================================================
def render_legacy_jd_baseline() -> None:
    st.caption(t("✅ JD-千叶仓库库存为基线·自动算 need_qty + 最优 lot 选择（page 08 兼容算法）"))
    if not st.button(t("🤖 开始计算"), type="primary", key="legacy_calc_btn"):
        return

    with st.spinner(t("📦 数据加载中...")):
        df_sales = _df("SELECT * FROM sales_line")
        df_purchase = _df("SELECT * FROM purchase_data")
        df_master = _df("SELECT * FROM item_master")
        df_warehouse = _df("SELECT * FROM warehouse_stock")
        df_history = _df("SELECT * FROM purchase_history")
        df_benten = _df("SELECT * FROM benten_stock")

    if df_sales.empty or df_purchase.empty or df_master.empty:
        st.warning(t("必要数据不足（需要 sales / purchase_data / item_master）"))
        return
    if df_warehouse.empty:
        st.warning(t("warehouse_stock 数据不足"))
        return

    sales_jan_col = "jan" if "jan" in df_sales.columns else "internal_id"
    sold_col = "quantity_sold" if "quantity_sold" in df_sales.columns else "qty"
    df_sales["jan"] = df_sales[sales_jan_col].apply(_normalize_jan)
    df_sales["quantity_sold"] = pd.to_numeric(df_sales[sold_col], errors="coerce").fillna(0).astype(int)
    if "stock_available" not in df_sales.columns:
        df_sales["stock_available"] = 0
    df_sales["stock_available"] = pd.to_numeric(df_sales["stock_available"], errors="coerce").fillna(0).astype(int)

    df_purchase["jan"] = df_purchase["jan"].apply(_normalize_jan)
    df_purchase["order_lot"] = pd.to_numeric(df_purchase["order_lot"], errors="coerce").fillna(0).astype(int)
    df_purchase["price"] = pd.to_numeric(df_purchase["price"], errors="coerce")

    df_master["jan"] = df_master["jan"].apply(_normalize_jan)

    df_warehouse["product_code"] = df_warehouse["product_code"].apply(_normalize_jan)
    df_warehouse["stock_available"] = pd.to_numeric(df_warehouse["stock_available"], errors="coerce").fillna(0).astype(int)

    if df_history.empty:
        df_history = pd.DataFrame(columns=["jan", "quantity", "memo", "order_date"])
    df_history["jan"] = df_history["jan"].apply(_normalize_jan)
    df_history["quantity"] = pd.to_numeric(df_history["quantity"], errors="coerce").fillna(0).astype(int)
    df_history["memo"] = df_history["memo"].astype(str).fillna("")

    df_shanghai = df_history[df_history["memo"].str.contains("上海", na=False)]
    df_shanghai_grouped = df_shanghai.groupby("jan")["quantity"].sum().reset_index(name="shanghai_quantity")
    if "発注済" not in df_master.columns:
        df_master["発注済"] = 0
    df_master = df_master.merge(df_shanghai_grouped, on="jan", how="left")
    df_master["shanghai_quantity"] = df_master["shanghai_quantity"].fillna(0).astype(int)
    df_master["発注済_修正後"] = (
        pd.to_numeric(df_master["発注済"], errors="coerce").fillna(0) - df_master["shanghai_quantity"]
    ).clip(lower=0)

    df_sales.drop(columns=["発注済"], errors="ignore", inplace=True)
    df_sales = df_sales.merge(df_master[["jan", "発注済_修正後"]], on="jan", how="left")
    df_sales["発注済"] = df_sales["発注済_修正後"].fillna(0).astype(int)

    rank_multiplier = {"Cランク": 1.0, "TEST": 1.5, "NEW": 1.5}

    df_history["order_date_dt"] = pd.to_datetime(df_history["order_date"], errors="coerce").dt.date
    today = date.today()
    yesterday = today - timedelta(days=1)
    recent_jans = set(
        df_history[df_history["order_date_dt"].isin([today, yesterday])]["jan"]
        .dropna().astype(str).apply(_normalize_jan).unique().tolist()
    )

    results = []
    with st.spinner(t("🤖 計算中...")):
        for _, row in df_sales.iterrows():
            jan = row["jan"]
            sold = int(row["quantity_sold"])
            ordered = int(row["発注済"])

            stock_row = df_warehouse[df_warehouse["product_code"] == jan]
            stock = int(stock_row["stock_available"].values[0]) if not stock_row.empty else 0

            rank_row = df_master[df_master["jan"] == jan]
            rank = ""
            if not rank_row.empty and ("ランク" in df_master.columns):
                rk = rank_row.iloc[0]["ランク"]
                rank = str(rk) if pd.notna(rk) else ""
            base_rank = _normalize_rank_base(rank)

            if jan in recent_jans:
                continue

            current_total = stock + ordered

            if base_rank in ("A", "B"):
                reorder_point = max(math.ceil(sold * 1.2), 1)
                if current_total >= reorder_point:
                    continue
            else:
                reorder_point = max(math.floor(sold * 0.7), 1)
                if current_total > reorder_point:
                    continue

            if base_rank in ("A", "B"):
                base_needed = max(math.ceil(sold * 1.7), 0)
                if stock <= 1 and sold >= 1 and base_needed <= 0:
                    base_needed = 1
            else:
                m = rank_multiplier.get(rank, 1.0)
                need_raw = math.ceil(sold * m) - stock - ordered
                base_needed = 1 if (stock <= 1 and sold >= 1 and need_raw <= 0) else max(need_raw, 0)
                if base_needed <= 0:
                    continue

            options_all = df_purchase[df_purchase["jan"] == jan].copy()
            valid = pd.DataFrame()
            if not options_all.empty:
                lots_pos = options_all[options_all["order_lot"] > 0].copy()
                valid = lots_pos[lots_pos["price"].notna() & (lots_pos["price"] > 0)].copy()

            if valid.empty:
                results.append({
                    "jan": jan, "販売実績": sold, "在庫": stock, "発注済": ordered,
                    "理論必要数": base_needed,
                    "発注数": "", "ロット": "", "数量": "", "単価": "", "総額": "",
                    "仕入先": "", "ランク": rank,
                })
                continue

            options = valid.copy()
            need_for_lot = base_needed
            if base_rank in ("A", "B"):
                bigger_lots = options[options["order_lot"] >= need_for_lot]
                if not bigger_lots.empty:
                    best = bigger_lots.sort_values("order_lot").iloc[0]
                else:
                    best = options.sort_values("order_lot", ascending=False).iloc[0]
            else:
                options["diff"] = (options["order_lot"] - need_for_lot).abs()
                smaller = options[options["order_lot"] <= need_for_lot]
                if not smaller.empty:
                    best = smaller.loc[smaller["diff"].idxmin()]
                else:
                    near = options[
                        (options["order_lot"] > need_for_lot)
                        & (options["order_lot"] <= need_for_lot * 1.5)
                        & (options["order_lot"] != 1)
                    ]
                    if not near.empty:
                        best = near.loc[near["diff"].idxmin()]
                    else:
                        one = options[options["order_lot"] == 1]
                        best = one.iloc[0] if not one.empty else options.sort_values("order_lot").iloc[0]

            lot = int(best["order_lot"])
            sets = math.ceil(need_for_lot / lot)
            qty = sets * lot
            total_cost = qty * float(best["price"])

            results.append({
                "jan": jan, "販売実績": sold, "在庫": stock, "発注済": ordered,
                "理論必要数": base_needed,
                "発注数": int(qty), "ロット": lot, "数量": int(sets),
                "単価": int(best["price"]), "総額": int(total_cost),
                "仕入先": best.get("supplier", "不明") or "不明",
                "ランク": rank,
            })

    if not results:
        st.info(t("当前没有需要订货的商品。"))
        return

    result_df = pd.DataFrame(results)

    if "商品コード" in df_master.columns:
        df_master["商品コード"] = df_master["商品コード"].astype(str).str.strip()
        result_df["jan"] = result_df["jan"].astype(str).str.strip()
        m = df_master[["商品コード", "商品名", "取扱区分"]].copy().rename(columns={"商品コード": "jan"})
        result_df = result_df.merge(m, on="jan", how="left")

    if not df_benten.empty:
        bn = df_benten[["jan", "stock"]].copy().rename(columns={"stock": "弁天在庫"})
        bn["jan"] = bn["jan"].apply(_normalize_jan)
        result_df = result_df.merge(bn, on="jan", how="left")
        result_df["弁天在庫"] = result_df["弁天在庫"].fillna(0).astype(int)

    result_df.rename(columns={"在庫": "JD在庫"}, inplace=True)

    if "商品名" in result_df.columns:
        result_df = result_df[result_df["商品名"].notna()]
    if "取扱区分" in result_df.columns:
        result_df = result_df[result_df["取扱区分"] != "取扱中止"]
    else:
        st.warning(t("⚠️『取扱区分』列不存在,无法过滤已停售。"))

    column_order = [
        "jan", "商品名", "ランク", "販売実績", "JD在庫", "弁天在庫", "発注済",
        "理論必要数", "発注数", "ロット", "数量", "単価", "総額", "仕入先",
    ]
    result_df = result_df[[c for c in column_order if c in result_df.columns]]

    st.success(t(f"✅ 订货对象: {len(result_df)} 件"))
    st.dataframe(result_df, use_container_width=True)

    csv = result_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(t("📥 订货 CSV 下载"), data=csv, file_name="orders_available_based.csv",
                       mime="text/csv", key="legacy_dl_all")

    st.markdown("---")
    st.subheader(t("📦 按供应商分组下载"))
    if "仕入先" in result_df.columns:
        groups = result_df[result_df["仕入先"].notna() & (result_df["仕入先"] != "")].groupby("仕入先")
        for supplier, group in groups:
            sup_csv = group.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label=f"📥 {supplier}",
                data=sup_csv,
                file_name=f"orders_{supplier}.csv",
                mime="text/csv",
                key=f"legacy_sup_{supplier}",
            )
    else:
        st.info(t("仕入先列不存在。"))


# ============================================================
# 顶层 tabs（合并入口）
# ============================================================
top_legacy, top_v2 = st.tabs([t("📦 旧版（JD基线）"), t("🆕 v2（多仕入先決定版）")])

with top_legacy:
    render_legacy_jd_baseline()

with top_v2:
    tab_calc, tab_sup, tab_item, tab_maker = st.tabs([
        t("📊 発注計算"), t("🚚 仕入先ビュー"), t("🏷️ 商品ビュー"), t("🏭 品牌ビュー"),
    ])
    
    # ------------------------------------------------------------
    # Tab 1: 発注計算
    # ------------------------------------------------------------
    with tab_calc:
        st.subheader(t("① 仕入先管理リスト"))
        if _has_supplier_quotes():
            n = conn.execute("SELECT COUNT(*) FROM supplier_quote").fetchone()[0]
            ns = conn.execute("SELECT COUNT(DISTINCT supplier_name) FROM supplier_quote").fetchone()[0]
            st.metric(t("登録済み報価"), f"{n:,}", f"{ns} 仕入先")
        else:
            st.warning(t(
                "⚠️ supplier_quote 表为空。手动 Excel ingester 已于 2026-05-19 弃用，"
                "请通过 PG SQL 或 NST API（T-NST-001）路径补齐数据。"
            ))
    
        st.divider()
        st.subheader(t("② パラメータ"))
        st.caption(t("📅 月販: 【輸出】アイテム別売上（概要）_JO (export_item) ｜ 在庫: 【輸出】在庫のスナップショット (手持 − 確保済 + 注文済)"))
        st.caption(t("推奨月販 = max(平均月販,直近月販) × 完売率係数 ｜ 目標在庫 = 推奨月販 × 2.5 ｜ 発注数 = max(0, 目標在庫 − 実質在庫) を ケース倍数に切り上げ"))
        st.caption(t("仕入先選定: zone優先(JD直送>弁天>応急>前払い)→同zoneは最安。各SKUに主力+備用1〜2。さらにメーカー単位で1〜3仕入先に集約(zone劣化なし)"))
        sales_source = "export_item"
    
        p1, p2, p3 = st.columns([1.2, 1, 1.5])
        with p1:
            months = st.number_input(t("月販トレンド期間 (ヶ月)"), 1, 12, 4,
                                      help=t("Boss 2026-05-14: 既定 4 ヶ月"))
            use_inv = st.checkbox(t("実質在庫を差し引く (JD手持+注文済)"), value=True)
        with p2:
            incl_disc = st.checkbox(t("取扱中止品も含める"), value=False)
        with p3:
            st.caption(t("完売率係数 (Boss 2026-05-26: 月完売率で発注量を調整・トレンド係数を置換)"))
            st.caption(t("≥0.9 断货风险→×1.5 ｜ 0.5-0.9 正常→×1.0 ｜ <0.5 压库存→×0.5"))
    
        q0, q1, q2, q3 = st.columns([1.4, 1, 1, 1.3])
        with q0:
            opt_label = st.radio(
                t("発注先の選び方"),
                [t("zone優先(JD直送>弁天>応急>前払い)"), t("発注金額(line_cost)が最小")],
                help=t("『発注金額最小』はロット丸め・納期込みで一番安い仕入先を選ぶ。zone劣化(JD直送→応急/前払い)が起きる場合あり"),
            )
            optimize = "line_cost" if "金額" in opt_label else "zone"
        with q1:
            consol = st.checkbox(t("メーカー単位で仕入先を集約"), value=True)
        with q2:
            max_sup_brand = st.number_input(t("1メーカーの上限仕入先数"), 1, 5, 3, disabled=not consol)
        with q3:
            small_brand = st.number_input(t("これ以下のSKU数のメーカーは集約しない"), 1, 20, 5, disabled=not consol)
    
        r0, r1 = st.columns([1.6, 1.4])
        with r0:
            rank_sel = st.multiselect(
                t("対象ランク (空 = 全部)"), ["Aランク", "Bランク", "Cランク", "NEW", "取扱中止"],
                default=["Aランク", "Bランク"],
                help=t("まず A/B 等級だけ見たい等。空にすると全ランク(取扱中止は別途除外)"),
            )
        with r1:
            cap_on = st.checkbox(t("発注後の在庫月数に上限を設ける"), value=False,
                                 help=t("ロット起定量で買い過ぎになる SKU を『保留』にする。例: 上限4ヶ月なら発注後在庫が4ヶ月超の SKU は発注しない"))
            max_stock = st.number_input(t("在庫月数の上限 (ヶ月)"), 1.0, 12.0, 4.0, 0.5, disabled=not cap_on)
    
        if st.button(t("🔍 発注計算実行"), type="primary", disabled=not _has_supplier_quotes()):
            with st.spinner(t("計算中…")):
                df = compute_recommendations(
                    conn, months=int(months),
                    sales_source=sales_source,
                    include_discontinued=bool(incl_disc),
                    use_inventory=bool(use_inv),
                    consolidate_by_brand=bool(consol),
                    max_suppliers_per_brand=int(max_sup_brand),
                    small_brand_skip=int(small_brand),
                    optimize=optimize,
                    ranks=rank_sel or None,
                    max_stock_months=float(max_stock) if cap_on else None,
                )
            st.session_state["po_reco"] = df
    
        df = st.session_state.get("po_reco")
        if df is None:
            st.info(t("「🔍 発注計算実行」を押すと推奨清单が出ます"))
        elif df.empty:
            st.warning(t(
                "⚠️ 発注対象 0 件 — shop_sales に export_item データがないか、supplier_quote に報価がありません。"
                "page 99 で【輸出】アイテム別売上（概要）_JO をアップロードしてください。"
            ))
        else:
            periods = df.attrs.get("periods", [])
            n_disc_ex = df.attrs.get("n_discontinued_excluded", 0)
            n_rank_ex = df.attrs.get("n_rank_excluded", 0)
            inv_loaded = df.attrs.get("inventory_loaded", False)
            n_consol = df.attrs.get("n_consolidated", 0)
            n_over = df.attrs.get("n_overstock", 0)
            cap = df.attrs.get("max_stock_months", None)
            st.caption(t(
                f"📅 月販期間: {', '.join(periods) if periods else '(なし)'} ｜ "
                f"在庫差引: {'✅ 適用' if inv_loaded else '⚠️ 在庫データなし(全量発注)'} ｜ "
                f"取扱中止 除外: {n_disc_ex} SKU ｜ ランク対象外 除外: {n_rank_ex} SKU ｜ "
                f"品牌集約で発注先変更: {n_consol} SKU ｜ 在庫過多で保留: {n_over} SKU"
                + (f" (上限{cap}ヶ月)" if cap else "")
            ))
            # Boss 2026-05-14: needs_review は「出はするが人工確認」→ 発注リストに含める。
            # new_passive (NEW = 受動発注) と deferred_* は別枠。
            df_rec = df[df["status"].isin(["recommended", "needs_review"])]
            df_new = df[df["status"] == "new_passive"]
            df_hold = df[df["status"].isin(["deferred_overstock", "deferred_min_order"])]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric(t("発注 SKU (推奨+要確認)"), f"{len(df_rec):,}",
                      delta=f"うち要確認 {int((df_rec['status']=='needs_review').sum())}")
            m2.metric(t("発注コスト"), f"¥{int(df_rec['line_cost'].sum()):,}")
            m3.metric(t("仕入先数"), f"{df_rec['supplier_name'].nunique()}")
            m4.metric(t("保留 SKU (在庫過多/最低受注未達)"), f"{len(df_hold)}", delta=None)
    
            if len(df_hold):
                with st.expander(t(f"⚠️ 保留 SKU {len(df_hold)} 件 (発注しない — 在庫過多 / 最低受注額未達)"), expanded=False):
                    st.dataframe(
                        df_hold[["jan", "display_name", "maker", "rank", "status", "on_hand", "suggested_qty",
                                 "pack_size", "stock_months_after", "supplier_name", "zone", "unit_price",
                                 "line_cost", "reason"]]
                        .rename(columns={"rank": "ランク", "status": "状態", "on_hand": "JD在庫",
                                         "suggested_qty": "発注数(参考)", "pack_size": "ケース",
                                         "stock_months_after": "発注後在庫月数", "supplier_name": "仕入先",
                                         "unit_price": "単価", "line_cost": "金額(参考)", "reason": "理由"})
                        .sort_values("発注後在庫月数", ascending=False),
                        use_container_width=True, hide_index=True,
                    )
    
            # NEW = 受動発注 (Boss 2026-05-14: 引擎不下单, 但出在「待需求」列表)
            if len(df_new):
                with st.expander(t(f"🆕 NEW 待需求 SKU {len(df_new)} 件 (受動発注 — 引擎不出単, 需要が来たら手動)"), expanded=False):
                    st.dataframe(
                        df_new[["jan", "display_name", "maker", "rank", "rec_monthly", "on_hand", "supplier_primary",
                                "zone", "unit_price", "pack_size", "lead_time_text", "reason"]]
                        .rename(columns={"rank": "ランク", "rec_monthly": "推奨月販", "on_hand": "JD在庫",
                                         "supplier_primary": "候補仕入先", "unit_price": "単価", "pack_size": "ケース",
                                         "lead_time_text": "納期", "reason": "理由"}),
                        use_container_width=True, hide_index=True,
                    )
    
            # 仕入先別 → 品牌別 サマリ (推奨分のみ)
            st.markdown(t("#### 仕入先別 → 品牌別 発注清单 (推奨分)"))
            for sup in df_rec.groupby("supplier_name")["line_cost"].sum().sort_values(ascending=False).index:
                sub = df_rec[df_rec["supplier_name"] == sup]
                zone = sub["zone"].iloc[0]
                sup_total = int(sub["line_cost"].sum())
                moq = int(sub["min_order_amount"].iloc[0])
                ok = sub["meets_min_order"].iloc[0]
                badge = "✅" if ok else f"⚠️ 最低¥{moq:,}未達"
                nst_code = sub["nst_supplier_code"].iloc[0] or "(NST コード未設定)"
                with st.expander(
                    f"{ZONE_LABEL.get(zone, zone)}  {sup}  —  ¥{sup_total:,}  {badge}  ({len(sub)} SKU)  · {nst_code}",
                    expanded=False,
                ):
                    by_maker = (
                        sub.groupby("maker")
                        .agg(SKU数=("jan", "count"), 発注数=("suggested_qty", "sum"), コスト=("line_cost", "sum"))
                        .reset_index().sort_values("コスト", ascending=False)
                    )
                    st.dataframe(by_maker, use_container_width=True, hide_index=True)
                    st.dataframe(
                        sub[["jan", "display_name", "maker", "rank", "status", "avg_monthly", "latest_monthly",
                             "trend", "sellthrough", "sellthrough_factor", "rec_monthly",
                             "jd_on_hand", "on_order", "eff_stock", "target_stock", "shortfall",
                             "case_qty", "lot_size", "pack_size", "pack_source",
                             "boxes", "suggested_qty", "stock_months_after",
                             "unit_price", "line_cost", "lead_time_text",
                             "consolidated", "supplier_backup1", "backup1_price", "supplier_backup2", "backup2_price",
                             "alt_suppliers", "reason"]]
                        .rename(columns={
                            "rank": "ランク", "status": "状態",
                            "avg_monthly": "平均月販", "latest_monthly": "直近月販", "trend": "傾向(参考)",
                            "sellthrough": "完売率", "sellthrough_factor": "係数", "rec_monthly": "推奨月販",
                            "jd_on_hand": "JD在庫", "on_order": "注文済", "eff_stock": "実質在庫",
                            "target_stock": "目標(×1.5)", "shortfall": "必要数",
                            "case_qty": "ケース入数", "lot_size": "ロット",
                            "pack_size": "取整単位", "pack_source": "単位種別",
                            "boxes": "箱数", "suggested_qty": "発注数", "stock_months_after": "発注後月数",
                            "unit_price": "単価", "line_cost": "金額",
                            "lead_time_text": "納期", "consolidated": "集約",
                            "supplier_backup1": "備用①", "backup1_price": "備①単価",
                            "supplier_backup2": "備用②", "backup2_price": "備②単価",
                            "alt_suppliers": "全候補", "reason": "理由",
                        }),
                        use_container_width=True, hide_index=True,
                    )
    
            st.divider()
            # NST 発注 CSV 出力
            st.markdown(t("#### ③ NST 発注 CSV 出力 (仕入先別)"))
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                sel_sup = st.selectbox(t("仕入先を選択"), sorted(df_rec["supplier_name"].unique()))
            with cc2:
                od = st.date_input(t("発注日"), value=date.today())
                emp = st.selectbox(t("従業員"), EMPLOYEES)
            with cc3:
                dept = st.selectbox(t("部門"), DEPARTMENTS)
                memo = st.text_input(t("メモ"), value=f"自動発注 {date.today():%Y-%m}")
            sub_sel = df_rec[df_rec["supplier_name"] == sel_sup] if sel_sup else df_rec.iloc[0:0]
            if sub_sel.empty:
                st.info(t("推奨発注なし — 仕入先別 CSV は出力対象がありません。"))
            else:
                # NST コードの推定 (報価の nst_supplier_code → 一致するものを default に)
                guess_code = sub_sel["nst_supplier_code"].iloc[0]
                nst_idx = NST_SUPPLIERS.index(guess_code) if guess_code in NST_SUPPLIERS else 0
                nst_sup = st.selectbox(t("NST 仕入先コード"), NST_SUPPLIERS, index=nst_idx)
                df_csv = _build_nst_csv(sub_sel, nst_supplier=nst_sup, order_date=od, employee=emp, department=dept, memo=memo)
                st.dataframe(df_csv, use_container_width=True, hide_index=True, height=300)
                st.download_button(
                    t(f"⬇️ {sel_sup} 発注 CSV ダウンロード"),
                    data=df_csv.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"発注書_{sel_sup}_{datetime.now():%Y%m%d}.csv",
                    mime="text/csv", type="primary",
                )
            # 全件まとめ
            st.download_button(
                t("⬇️ 全 SKU 推奨清单 CSV (全仕入先)"),
                data=df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"発注推奨_全件_{datetime.now():%Y%m%d}.csv",
                mime="text/csv",
            )
    
    # ------------------------------------------------------------
    # Tab 2: 仕入先ビュー
    # ------------------------------------------------------------
    with tab_sup:
        if not _has_supplier_quotes():
            st.info(t("先に「📊 発注計算」タブで仕入先管理リストをアップロード"))
        else:
            sups = [r[0] for r in conn.execute(
                "SELECT supplier_name FROM supplier_quote "
                "GROUP BY supplier_name ORDER BY MIN(zone_rank), supplier_name"
            ).fetchall()]
            sel = st.selectbox(t("仕入先"), sups, key="sup_view_sel")
            rows = conn.execute(
                "SELECT jan, display_name, unit_price, lot_size, case_qty, min_order_amount, "
                "order_condition, lead_time_text, zone, zone_rank, nst_supplier_code "
                "FROM supplier_quote WHERE supplier_name = ? ORDER BY display_name", (sel,)
            ).fetchall()
            df_q = pd.DataFrame([dict(r) for r in rows])
            zone = df_q["zone"].iloc[0]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric(t("zone"), ZONE_LABEL.get(zone, zone))
            c2.metric(t("取扱 SKU 数"), f"{len(df_q):,}")
            moq_vals = df_q["min_order_amount"].dropna()
            c3.metric(t("注文最低金額"), f"¥{int(moq_vals.max()):,}" if len(moq_vals) and moq_vals.max() else t("制限なし"))
            c4.metric(t("NST コード"), df_q["nst_supplier_code"].iloc[0] or "-")
            # 発注条件の分布
            oc = df_q["order_condition"].dropna().value_counts()
            if len(oc):
                st.caption(t("発注条件: " + " / ".join(f"{k}({v})" for k, v in oc.items())))
            lt = df_q["lead_time_text"].dropna().value_counts()
            if len(lt):
                st.caption(t("納期: " + " / ".join(f"{k}({v})" for k, v in lt.items())))
    
            # 現在の発注金額 (発注計算済みなら)
            df_reco = st.session_state.get("po_reco")
            if df_reco is not None and not df_reco.empty:
                cur = df_reco[df_reco["supplier_name"] == sel]
                if not cur.empty:
                    st.success(t(
                        f"📦 現在の発注推奨: {len(cur)} SKU · ¥{int(cur['line_cost'].sum()):,}"
                        + ("" if cur["meets_min_order"].iloc[0] else " · ⚠️ 最低受注額未達")
                    ))
    
            st.dataframe(
                df_q[["jan", "display_name", "unit_price", "lot_size", "case_qty",
                      "min_order_amount", "order_condition", "lead_time_text"]]
                .rename(columns={"unit_price": "単価", "lot_size": "ロット", "case_qty": "入数",
                                 "min_order_amount": "注文最低金額", "order_condition": "発注条件",
                                 "lead_time_text": "納期"}),
                use_container_width=True, hide_index=True, height=480,
            )
            st.download_button(
                t(f"⬇️ {sel} 報価単 CSV"),
                data=df_q.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"報価単_{sel}.csv", mime="text/csv",
            )
    
    # ------------------------------------------------------------
    # Tab 3: 商品ビュー
    # ------------------------------------------------------------
    with tab_item:
        if not _has_supplier_quotes():
            st.info(t("先に「📊 発注計算」タブで仕入先管理リストをアップロード"))
        else:
            jan_in = st.text_input(t("JAN コードを入力"), placeholder="4987241173242")
            if jan_in.strip():
                jan = jan_in.strip()
                rows = conn.execute(
                    "SELECT supplier_name, display_name, unit_price, lot_size, min_order_amount, "
                    "order_condition, lead_time_text, zone, zone_rank "
                    "FROM supplier_quote WHERE jan = ? ORDER BY zone_rank, unit_price", (jan,)
                ).fetchall()
                if not rows:
                    st.warning(t(f"❌ JAN {jan} はどの仕入先 sheet にもありません (新商品 or SD/ハリマ に询价が必要)"))
                else:
                    from shared.purchase_engine import ZONE_MARKUP
                    df_c = pd.DataFrame([dict(r) for r in rows])
                    df_c["実質単価(比価)"] = df_c.apply(lambda r: round(r["unit_price"] * ZONE_MARKUP.get(r["zone"], 1.0)), axis=1)
                    df_c["zone"] = df_c["zone"].map(lambda z: ZONE_LABEL.get(z, z))
                    name = df_c["display_name"].dropna().iloc[0] if df_c["display_name"].notna().any() else "(名称不明)"
                    st.markdown(f"**{name}** — {len(df_c)} 仕入先で取扱")
                    # 月販 (輸出概要)
                    ms = conn.execute(
                        "SELECT period_start, SUM(qty_sold) q FROM shop_sales "
                        "WHERE jan = ? AND source = 'export_item' GROUP BY period_start ORDER BY period_start", (jan,)
                    ).fetchall()
                    if ms:
                        st.caption(t("月販 (輸出概要): " + " / ".join(f"{r[0]}:{int(r[1] or 0)}" for r in ms)))
                    # 推奨先 highlight
                    best = df_c.sort_values(["zone_rank", "実質単価(比価)"]).iloc[0]
                    st.success(t(f"→ 推奨仕入先: {best['supplier_name']} ({best['zone']}) · 単価 ¥{int(best['unit_price'])} · ロット {int(best['lot_size'] or 1)}"))
                    st.dataframe(
                        df_c[["supplier_name", "zone", "unit_price", "実質単価(比価)", "lot_size",
                              "min_order_amount", "order_condition", "lead_time_text"]]
                        .rename(columns={"supplier_name": "仕入先", "unit_price": "単価", "lot_size": "ロット",
                                         "min_order_amount": "注文最低金額", "order_condition": "発注条件",
                                         "lead_time_text": "納期"}),
                        use_container_width=True, hide_index=True,
                    )
    
    # ------------------------------------------------------------
    # Tab 4: 品牌ビュー
    # ------------------------------------------------------------
    with tab_maker:
        if not _has_supplier_quotes():
            st.info(t("先に「📊 発注計算」タブで仕入先管理リストをアップロード"))
        else:
            # item_v2 から maker → jan のマップ; supplier_quote と JOIN
            makers = [r[0] for r in conn.execute(
                "SELECT DISTINCT maker FROM item_v2 WHERE maker IS NOT NULL AND maker <> '' ORDER BY maker"
            ).fetchall()]
            if not makers:
                st.warning(t("item_v2 に maker データがありません (商品主档を先にインポート)"))
            else:
                sel_mk = st.selectbox(t("メーカー"), makers, key="maker_view_sel")
                rows = conn.execute(
                    "SELECT i.jan, i.display_name, q.supplier_name, q.unit_price, q.lot_size, "
                    "q.zone, q.zone_rank, q.min_order_amount "
                    "FROM item_v2 i JOIN supplier_quote q ON q.jan = i.jan "
                    "WHERE i.maker = ? ORDER BY i.jan, q.zone_rank, q.unit_price", (sel_mk,)
                ).fetchall()
                if not rows:
                    st.info(t(f"{sel_mk} の SKU は仕入先 sheet に見つかりません"))
                else:
                    df_m = pd.DataFrame([dict(r) for r in rows])
                    n_sku = df_m["jan"].nunique()
                    n_sup = df_m["supplier_name"].nunique()
                    c1, c2 = st.columns(2)
                    c1.metric(t("SKU 数 (報価あり)"), f"{n_sku}")
                    c2.metric(t("関与仕入先数"), f"{n_sup}")
                    # SKU 毎の最安仕入先
                    best_per_sku = (
                        df_m.sort_values(["jan", "zone_rank", "unit_price"])
                        .groupby("jan").first().reset_index()
                    )
                    best_per_sku["zone"] = best_per_sku["zone"].map(lambda z: ZONE_LABEL.get(z, z))
                    st.markdown(t("#### SKU 別 推奨仕入先 (zone 優先 → 単価最安)"))
                    st.dataframe(
                        best_per_sku[["jan", "display_name", "supplier_name", "zone", "unit_price", "lot_size"]]
                        .rename(columns={"supplier_name": "推奨仕入先", "unit_price": "単価", "lot_size": "ロット"}),
                        use_container_width=True, hide_index=True, height=400,
                    )
                    # 仕入先別 SKU 数
                    st.markdown(t("#### 仕入先別カバレッジ"))
                    cov = df_m.groupby(["zone", "supplier_name"])["jan"].nunique().reset_index().rename(columns={"jan": "SKU数"})
                    cov["zone"] = cov["zone"].map(lambda z: ZONE_LABEL.get(z, z))
                    st.dataframe(cov.sort_values("SKU数", ascending=False), use_container_width=True, hide_index=True)
