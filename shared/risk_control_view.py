"""库存风控ビュー · 可售天数（JDL库存 / 前N天日均销量）による在庫リスク監視盘。

page06 库存监控 の「🛡️ 库存风控」タブから `render(conn)` で呼ぶ（旧 page18 を統合·
Boss 2026-06-04）。set_page_config / 認証 / テーマ / lang_selector は呼び出し側
（page06）が済ませている前提なので、ここでは UI 本体だけを描く。

データ源（current-snapshot · 月份次元なし）:
  · 前N天销量   = nst.sales_daily（直近 N 日 SUM·item_internal_id 単位）
  · 当前库存(JDL) = jdl.v_inventory_reconciliation.jdl_qty_in_stock（JD物流実物在仓·jan 単位）
  · 可售天数      = JDL库存 / 日均销量 → 阈値(天)で 3 档（断货风险 / 正常 / 压库存）
  · 资金占用 = JDL库存 × 平均单价。只取有商品等级的（item_rank 非空·无等级忽略）。

⚠️ リスク識別のみ。発注量・仕入先選択 → 📦 発注AI v2（page25·唯一の下单引擎）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db_helpers import df as _df_conn
from shared.i18n import t
from shared.inventory_risk import (
    RISK_STOCKOUT, RISK_NORMAL, RISK_OVERSTOCK, RISK_NO_DATA, RISK_LABELS,
    RANK_TARGET_KEYS, enrich, load_risk_thresholds, save_risk_thresholds,
    is_stockout, stockout_rate_by_rank, releasable_value, target_days_for_rank,
)


def render(conn) -> None:
    """库存风控タブ本体。conn = 呼び出し側の get_connection()。"""

    def _df(sql, params=None):
        from shared.cache import cached_df, data_version
        return cached_df(conn, sql, params, ver=data_version())

    st.title(t("🛡️ 库存风控"))
    st.caption(t("按可售天数(JDL库存/日均销量)识别断货 / 压库存风险 · 仅有等级商品 · "
                 "🛒 精确补货量请用「📦 発注AI v2」"))

    # ============================================================
    # ⚙️ 风险阈值（可售天数·Boss 随时可调·独立持久化）
    # ============================================================
    _saved = load_risk_thresholds()
    with st.expander(f"⚙️ {t('风险阈值设定 (可售天数·随时可调)')}", expanded=False):
        _window_label = st.radio(
            t("销量统计窗口"), [t("前30天"), t("前60天"), t("前90天")],
            index=0, horizontal=True, key="page18_window",
            help=t("前N天销量 → 日均销量 → 可售天数 / 可释放金额"))
        tcol1, tcol2, tcol3 = st.columns([1.5, 1.5, 1])
        reorder = tcol1.number_input(
            t("🔴 断货线 (可售天数 <)"),
            min_value=0.0, max_value=365.0, value=float(_saved["reorder_days"]), step=5.0,
            key="risk_th_reorder",
            help=t("可售天数 = JDL库存 / 日均销量(窗口销量/窗口天数)。低于此线 = 断货风险, 要补货"))
        overstock = tcol2.number_input(
            t("🟡 压库存线 (可售天数 >)"),
            min_value=0.0, max_value=999.0, value=float(_saved["overstock_days"]), step=5.0,
            key="risk_th_overstock",
            help=t("高于此线 = 压库存, 减少订货 / 优先消化"))
        # 各等级标准可售天数（A/B/C/NEW 独立·停售/其它走全局默认）
        st.markdown(f"**🎯 {t('各等级标准可售天数')}**")
        st.caption(t("理想库存水位(天)·各等级独立。压库存中超出此水位的部分 = 可释放库存金额；"
                     "停售 / 其它等级走全局默认 {d} 天").format(d=f"{_saved['target_days']:g}"))
        _saved_by_rank = _saved.get("target_days_by_rank", {})
        _rank_cols = st.columns(len(RANK_TARGET_KEYS))
        target_by_rank = {}
        for _col, _rk in zip(_rank_cols, RANK_TARGET_KEYS):
            target_by_rank[_rk] = _col.number_input(
                _rk, min_value=0.0, max_value=999.0,
                value=float(_saved_by_rank.get(_rk, _saved["target_days"])), step=5.0,
                key=f"risk_th_target_{_rk}")
        with tcol3:
            st.write("")
            st.write("")
            if st.button(t("💾 保存阈值"), use_container_width=True):
                save_risk_thresholds({"reorder_days": reorder, "overstock_days": overstock,
                                      "target_days": float(_saved["target_days"]),
                                      "target_days_by_rank": target_by_rank})
                st.success(t("✓ 已保存"))
        if reorder > overstock:
            st.warning(t("⚠️ 断货线应 < 压库存线"))
    window_days = {t("前30天"): 30, t("前60天"): 60, t("前90天"): 90}[_window_label]
    _th = {"reorder_days": reorder, "overstock_days": overstock,
           "target_days": float(_saved["target_days"]), "target_days_by_rank": target_by_rank}

    # ============================================================
    # 数据加载（current-snapshot·只取有等级）
    #   qty_sold = 前N天销量（sales_daily）· current_stock = JDL库存（recon view）
    # ============================================================
    try:
        df_all = _df(
            f"""
            WITH s30 AS (
                SELECT item_internal_id, SUM(qty_sold) AS qty_sold,
                       SUM(revenue) AS revenue_30d, SUM(gross_profit) AS gp_30d
                FROM nst.sales_daily
                WHERE sale_date >= CURRENT_DATE - INTERVAL '{window_days} days'
                GROUP BY item_internal_id
            )
            SELECT im.internal_id AS internal_id, im.item_code AS item_code, im.jan AS jan,
                   COALESCE(im.display_name, '') AS display_name,
                   im.item_rank AS rank, im.maker AS maker, im.date_created AS date_created,
                   im.cost_estimate AS cost_estimate, im.last_purchase_cost AS last_purchase_cost,
                   COALESCE(im.average_cost, im.cost_estimate) AS avg_unit_price,
                   COALESCE(s30.qty_sold, 0) AS qty_sold,
                   COALESCE(s30.revenue_30d, 0) AS revenue_30d,
                   COALESCE(s30.gp_30d, 0) AS gp_30d
            FROM nst.item_master_raw im
            LEFT JOIN s30 ON s30.item_internal_id = im.internal_id
            WHERE im.item_rank IS NOT NULL AND btrim(im.item_rank) <> ''
            """
        )
    except Exception as e:
        st.error(t("⚠️ 读取 nst.sales_daily / item_master_raw 失败（需 Postgres/NST 数据源）。"))
        st.caption(str(e))
        st.stop()

    if df_all.empty:
        st.warning(t("⚠️ 暂无有等级商品数据。"))
        st.stop()

    # 当前库存 = JDL 实物在仓（jdl.v_inventory_reconciliation·按 jan）
    try:
        _jdl = _df("SELECT jan, jdl_qty_in_stock AS current_stock FROM jdl.v_inventory_reconciliation")
    except Exception:
        _jdl = pd.DataFrame(columns=["jan", "current_stock"])
    if not _jdl.empty:
        df_all = df_all.merge(_jdl, how="left", on="jan")
    if "current_stock" not in df_all.columns:
        df_all["current_stock"] = 0
    df_all["current_stock"] = pd.to_numeric(df_all["current_stock"], errors="coerce").fillna(0)

    # 在途残（未关闭 PO 的入荷残·按 item_internal_id）
    try:
        _itx = _df(
            "SELECT item_internal_id, SUM(quantity - COALESCE(quantity_received,0)) AS in_transit_qty "
            "FROM nst.purchase_order_line "
            "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
            "GROUP BY item_internal_id")
    except Exception:
        _itx = pd.DataFrame(columns=["item_internal_id", "in_transit_qty"])
    if not _itx.empty:
        df_all = df_all.merge(_itx, how="left", left_on="internal_id", right_on="item_internal_id")
        if "item_internal_id" in df_all.columns:
            df_all = df_all.drop(columns=["item_internal_id"])
    if "in_transit_qty" not in df_all.columns:
        df_all["in_transit_qty"] = 0
    df_all["in_transit_qty"] = pd.to_numeric(df_all["in_transit_qty"], errors="coerce").fillna(0)

    # 派生列（可售天数 / risk_label）· 销量窗口 = window_days
    df_all = enrich(df_all, _th, days_in_period=window_days)
    # 资金占用 = 平均单价(COALESCE average_cost, 定義原価) × JDL库存
    df_all["avg_unit_price"] = pd.to_numeric(df_all.get("avg_unit_price", 0), errors="coerce").fillna(0)
    df_all["capital_exposure"] = df_all["current_stock"] * df_all["avg_unit_price"]
    # 毛利率 = 前N天 gross_profit / revenue
    _rev = pd.to_numeric(df_all.get("revenue_30d", 0), errors="coerce").fillna(0)
    _gp = pd.to_numeric(df_all.get("gp_30d", 0), errors="coerce").fillna(0)
    df_all["gross_margin"] = (_gp / _rev.replace(0, pd.NA)).fillna(0).astype(float)
    # 可释放库存金额 = 标准可售天数下超出部分 × 平均单价（标准天数按等级取·停售走全局默认）
    df_all["releasable"] = [
        releasable_value(stk, sld, prc,
                         target_days=target_days_for_rank(_th, rk), days_in_period=window_days)
        for stk, sld, prc, rk in zip(df_all["current_stock"], df_all["qty_sold"],
                                     df_all["avg_unit_price"], df_all["rank"])
    ]
    # 断货标记：前N天有销量 且 当前 JDL 库存 = 0
    df_all["is_stockout"] = [is_stockout(s, k) for s, k in zip(df_all["qty_sold"], df_all["current_stock"])]

    # ============================================================
    # 筛选器（无月份 / 无仓库）
    # ============================================================
    rank_opts = sorted([x for x in df_all["rank"].dropna().unique().tolist() if str(x).strip()])

    f1, f2, f3, f4 = st.columns([2, 2, 1.4, 1.4])
    with f1:
        # options 保持中文原值（risk_label 比较用）· format_func=t 只翻显示
        sel_risk = st.selectbox(
            t("风险等级"), options=["全部", *RISK_LABELS], index=0,
            key="page18_risk", format_func=t)
    with f2:
        sel_ranks = st.multiselect(t("商品等级"), options=rank_opts, default=[], key="page18_ranks")
    with f3:
        sel_intransit = st.selectbox(
            t("在途状态"), [t("全部"), t("有在途"), t("没在途")], index=0, key="page18_intransit")
    with f4:
        sel_instock = st.selectbox(
            t("库存状态"), [t("全部"), t("有货"), t("断货")], index=0, key="page18_instock",
            help=t("断货 = 前{d}天有销量 但 当前JDL库存=0").format(d=window_days))

    search_kw = st.text_area(
        t("JAN / item_code 搜索（多个用 空格 / 逗号 / 换行 分隔）"),
        placeholder="4901111  4902222\n01-0641-134", height=72, key="page18_search")

    # 应用筛选
    df = df_all.copy()
    if sel_risk != "全部":
        df = df[df["risk_label"] == sel_risk]
    if sel_ranks:
        df = df[df["rank"].astype(str).isin(sel_ranks)]
    if sel_intransit == t("有在途"):
        df = df[df["in_transit_qty"] > 0]
    elif sel_intransit == t("没在途"):
        df = df[df["in_transit_qty"] <= 0]
    if sel_instock == t("有货"):
        df = df[df["current_stock"] > 0]
    elif sel_instock == t("断货"):
        df = df[df["is_stockout"]]
    if search_kw and search_kw.strip():
        import re as _re
        _tokens = [tk for tk in _re.split(r"[\s,，、;；]+", search_kw.strip()) if tk]
        if _tokens:
            _ic = df["item_code"].astype(str)
            _jn = df["jan"].astype(str)
            _mask = pd.Series(False, index=df.index)
            for _tk in _tokens:
                _mask = (_mask
                         | _ic.str.contains(_tk, case=False, na=False, regex=False)
                         | _jn.str.contains(_tk, case=False, na=False, regex=False))
            df = df[_mask]

    # ============================================================
    # 顶部风险概览卡
    # ============================================================
    risk_counts = df["risk_label"].value_counts()
    overstock_capital = float(df.loc[df["risk_label"] == RISK_OVERSTOCK, "capital_exposure"].sum())

    releasable_total = float(df.loc[df["risk_label"] == RISK_OVERSTOCK, "releasable"].sum())
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric(t("SKU 总数"), int(df["item_code"].nunique()))
    k2.metric(t("🔴 断货风险"), int(risk_counts.get(RISK_STOCKOUT, 0)))
    k3.metric(t("🟡 压库存"), int(risk_counts.get(RISK_OVERSTOCK, 0)))
    k4.metric(t("🟢 正常"), int(risk_counts.get(RISK_NORMAL, 0)))
    k5.metric(t("💰 压库存资金占用"), f"¥{overstock_capital:,.0f}")
    k6.metric(t("♻️ 可释放库存金额(各等级标准)"), f"¥{releasable_total:,.0f}")

    st.caption(t("前{w}天销售 + JDL实物库存 · 断货线<{r}天 / 压库存线>{o}天 · "
                 "仅有等级商品 · 当前筛选 {n} 行").format(
                     w=window_days, r=f"{reorder:g}", o=f"{overstock:g}", n=len(df)))

    # 断货率卡片（按商品等级·有等级全量·断货=前N天有销量+JDL库存0）
    _so = stockout_rate_by_rank(df_all)
    if not _so.empty:
        _so_title = t("各等级断货率（前{d}天有销量 + 当前JDL库存=0）").format(d=window_days)
        st.markdown(f"##### 📊 {_so_title}")
        _rows = list(_so.itertuples(index=False))
        for _start in range(0, len(_rows), 6):
            _chunk = _rows[_start:_start + 6]
            _cards = st.columns(len(_chunk))
            for _c, _r in zip(_cards, _chunk):
                _c.metric(f"{_r.rank} " + t("断货率"),
                          f"{float(_r.rate) * 100:.1f}%",
                          f"{int(_r.stockout)}/{int(_r.total)}",
                          delta_color="off")
    st.divider()

    # ============================================================
    # 📋 SKU 360（决策上下文宽表·唯一视图）
    # ============================================================
    st.markdown(f"### 📋 {t('SKU 360 · 决策上下文宽表')}")
    st.caption(t("当前筛选范围 · 销量 / 库存 / 在途PO / 采购价 / 等级一览"))
    if df.empty:
        st.info(t("当前筛选无数据"))
        return
    # 在途供应商 + 最近 PO（按 item_internal_id）
    try:
        sup = _df(
            "SELECT item_internal_id, string_agg(DISTINCT vendor_name, ', ') AS in_transit_suppliers, "
            "MAX(po_number) AS latest_po FROM nst.purchase_order_line "
            "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
            "GROUP BY item_internal_id")
    except Exception:
        sup = pd.DataFrame(columns=["item_internal_id", "in_transit_suppliers", "latest_po"])
    wide = df[["internal_id", "item_code", "jan", "display_name", "maker", "rank",
               "date_created",
               "risk_label", "is_stockout", "qty_sold", "gross_margin", "current_stock",
               "days_of_supply", "in_transit_qty", "last_purchase_cost", "avg_unit_price",
               "capital_exposure", "releasable"]].drop_duplicates("internal_id").copy()
    if not sup.empty:
        wide = wide.merge(sup, how="left", left_on="internal_id", right_on="item_internal_id")
        if "item_internal_id" in wide.columns:
            wide = wide.drop(columns=["item_internal_id"])
    for c in ("in_transit_suppliers", "latest_po"):
        if c not in wide.columns:
            wide[c] = ""
        wide[c] = wide[c].fillna("")

    COLS360 = [
        ("item_code", t("item_code")), ("jan", t("JAN")), ("display_name", t("商品名")),
        ("maker", t("厂家")), ("rank", t("商品等级")), ("date_created", t("建立日期")),
        ("risk_label", t("风险")),
        ("is_stockout", t("断货")), ("qty_sold", t("前{d}天销量").format(d=window_days)),
        ("gross_margin", t("毛利率")),
        ("current_stock", t("JDL库存")), ("days_of_supply", t("可售天数")),
        ("in_transit_qty", t("在途残")), ("in_transit_suppliers", t("在途供应商")),
        ("latest_po", t("最近PO")), ("last_purchase_cost", t("最近采购价")),
        ("avg_unit_price", t("平均单价")), ("capital_exposure", t("资金占用(¥)")),
        ("releasable", t("可释放库存金额")),
    ]
    order = [k for k, _ in COLS360 if k in wide.columns]
    show360 = wide[order].copy()
    disp = show360.copy()
    disp.columns = [dict(COLS360)[k] for k in order]
    if t("建立日期") in disp.columns:
        disp[t("建立日期")] = pd.to_datetime(
            disp[t("建立日期")], errors="coerce").dt.strftime("%Y-%m-%d")
    if t("风险") in disp.columns:        # risk_label 业务值 → 显示层翻译（df 比较仍用原值）
        disp[t("风险")] = disp[t("风险")].map(t)
    if t("断货") in disp.columns:
        disp[t("断货")] = disp[t("断货")].map(lambda v: "🚫" + t("断货") if bool(v) else "")
    if t("可售天数") in disp.columns:
        disp[t("可售天数")] = pd.to_numeric(disp[t("可售天数")], errors="coerce").round(0)
    if t("毛利率") in disp.columns:
        disp[t("毛利率")] = (pd.to_numeric(disp[t("毛利率")], errors="coerce").fillna(0) * 100).round(1).astype(str) + "%"
    for _c in (t("资金占用(¥)"), t("最近采购价"), t("平均单价"), t("可释放库存金额")):
        if _c in disp.columns:
            disp[_c] = pd.to_numeric(disp[_c], errors="coerce").fillna(0).map(lambda v: f"¥{v:,.0f}")
    st.caption(f"{len(show360)} " + t("个 SKU"))
    st.dataframe(disp, use_container_width=True, height=560, hide_index=True)
    st.download_button(
        t("📥 下载 SKU 360 CSV"),
        data=disp.to_csv(index=False).encode("utf-8-sig"),  # disp = 翻译表头(所见即所得)
        file_name="sku360.csv", mime="text/csv", key="dl_sku360")
