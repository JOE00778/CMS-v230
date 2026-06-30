"""入库状态（受領状態）ビュー · 受領書(ItemReceipt)ベースの実際入荷.

page30「📊 采购 · 在途订单」の「📦 入库状态 / 受領状態」タブから `render(conn)` で呼ぶ
（旧 page31「入荷实绩」を統合 · Boss 2026-06-30）。set_page_config / 認証 / テーマ /
lang_selector は呼び出し側（page30）が済ませている前提なので、ここでは UI 本体だけ描く。

数据源（database/.../019,020 · pull_receipt_doc 日次 06:35）:
  nst.item_receipt_line     受領書 明細行（行级单一事実源 · 2026今年 × 输出供货商白名单のみ）
  nst.receipt_po_match      受領 × 発注行 突合 view（入荷実績vs予定 · 有効予定日=手動ETA優先）
  nst.receipt_item_monthly  item × 月 × 倉庫 → 実受領数 / 検収金额

3 内 tab（Boss 2026-06-25 拍板）: 入荷历史 / 入荷实绩vs预定 / 检收金额
★ 表在 pull 时已按 2026 × po_export_vendor(输出供货商白名单) 收口 → 本页全行皆对象。
"""
from __future__ import annotations

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import t


def render(conn=None) -> None:
    """入库状态タブ本体。conn = 呼び出し側の get_connection()（None なら自前で取得）。"""
    if conn is None:
        conn = get_connection()

    def _df(sql: str, params=None) -> pd.DataFrame:
        from shared.cache import cached_df, data_version
        return cached_df(conn, sql, params, ver=data_version("purchase", "basic"))

    def _table_ready() -> bool:
        try:
            conn.execute("SELECT 1 FROM nst.item_receipt_line LIMIT 1")
            return True
        except Exception:
            conn.rollback()
            return False

    if not _table_ready():
        st.warning(t("收货单表 nst.item_receipt_line 还不存在（pull_receipt_doc 未执行）。"))
        return

    st.caption(t("数据源 NetSuite 收货单(ItemReceipt) · 今年(2026)× 有关系的供货商(白名单)的实际入荷 · "
                 "每天 06:35 自动更新"))

    # ---- 共通フィルタ部品 ----
    def _vendor_options() -> list[tuple[str, str]]:
        """(vendor_id, vendor_name) 一覧（有收货实绩的供货商のみ · _df 已缓存）。"""
        d = _df("SELECT DISTINCT vendor_id, vendor_name FROM nst.item_receipt_line "
                "WHERE vendor_id IS NOT NULL ORDER BY vendor_name")
        return [(r.vendor_id, r.vendor_name or r.vendor_id) for r in d.itertuples()]

    def _month_options() -> list[str]:
        """利用可能な受領月 'YYYY-MM' 一覧（新しい順 · _df 已缓存）。"""
        d = _df("SELECT DISTINCT to_char(receipt_date,'YYYY-MM') AS ym "
                "FROM nst.item_receipt_line WHERE receipt_date IS NOT NULL ORDER BY ym DESC")
        return [r.ym for r in d.itertuples()]

    def _month_picker(key: str, col: str):
        """收货月の多选フィルタ。戻り値 =(where片段, params, label)。空选=全部月份。"""
        picked = st.multiselect(t("收货月（留空=全部）"), _month_options(), key=f"{key}_ym",
                                help=t("按收货月份多选 · 留空=全部月份"))
        if not picked:
            return "TRUE", [], "all"
        ph = ",".join(["%s"] * len(picked))
        return f"to_char({col},'YYYY-MM') IN ({ph})", list(picked), "_".join(sorted(picked))

    def _vendor_filter(key: str):
        opts = _vendor_options()
        label_to_id = {f"{name}": vid for vid, name in opts}
        picked = st.multiselect(t("供货商（留空=全部）"), list(label_to_id.keys()), key=f"{key}_vendor")
        return [label_to_id[p] for p in picked]

    def _in_clause(ids: list[str]) -> tuple[str, list]:
        """vendor_id IN (...) 片段 + params（空=无过滤）。"""
        if not ids:
            return "", []
        ph = ",".join(["%s"] * len(ids))
        return f" AND irl.vendor_id IN ({ph})", list(ids)

    # ========================================================
    # tab1 · 入荷历史查询
    # ========================================================
    def _render_history():
        st.caption(t("哪张收货单何时、从哪个供货商、收了什么、多少钱。行级明细（±修正行作为实数保留）。"))
        mfrag, mparams, mlabel = _month_picker("h", "irl.receipt_date")
        fc1, fc2 = st.columns([3, 2])
        with fc1:
            vids = _vendor_filter("h")
        with fc2:
            kw = st.text_input(t("JAN / 品名 搜索（空格分隔多个）"), key="h_kw",
                               placeholder="4589546892226  シャンプー")

        vsql, vparams = _in_clause(vids)
        kw_sql, kw_params = "", []
        if kw.strip():
            tokens = [x for x in kw.replace("\n", " ").replace(",", " ").split() if x]
            jans = [x for x in tokens if x.isdigit()]
            names = [x for x in tokens if not x.isdigit()]
            if jans:
                kw_sql += f" AND im.jan IN ({','.join(['%s']*len(jans))})"
                kw_params += jans
            for nm in names:
                kw_sql += " AND im.display_name ILIKE %s"
                kw_params.append(f"%{nm}%")

        sql = (
            "SELECT irl.receipt_date, irl.receipt_no, irl.vendor_name, im.jan, im.display_name, "
            "       irl.location, irl.quantity, irl.rate, irl.amount, pol.po_number, pol.po_memo "
            "FROM nst.item_receipt_line irl "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = irl.item_internal_id "
            "LEFT JOIN (SELECT DISTINCT po_internal_id, po_number, po_memo FROM nst.purchase_order_line) pol "
            "       ON pol.po_internal_id = irl.po_internal_id "
            "WHERE " + mfrag + " "
            f"{vsql}{kw_sql} "
            "ORDER BY irl.receipt_date DESC, irl.receipt_no, im.display_name"
        )
        df = _df(sql, mparams + vparams + kw_params)
        if df.empty:
            st.info(t("没有符合条件的收货明细，请放宽条件。"))
            return

        m1, m2, m3, m4 = st.columns(4)
        m1.metric(t("收货单数"), int(df["receipt_no"].nunique()))
        m2.metric(t("明细行数"), len(df))
        m3.metric(t("检收金额合计 (¥)"), f"¥{df['amount'].fillna(0).sum():,.0f}")
        m4.metric(t("供货商数"), int(df["vendor_name"].nunique()))

        show = df.rename(columns={
            "receipt_date": t("收货日"), "receipt_no": t("收货单号"), "vendor_name": t("供货商"),
            "jan": "JAN", "display_name": t("品名"), "location": t("仓库"),
            "quantity": t("数量"), "rate": t("单价"), "amount": t("金额"), "po_number": t("对应PO"),
            "po_memo": t("発注メモ"),
        })
        st.dataframe(show, use_container_width=True, hide_index=True, column_config={
            t("金额"): st.column_config.NumberColumn(format="¥%,.0f"),
            t("单价"): st.column_config.NumberColumn(format="¥%,.1f"),
            t("数量"): st.column_config.NumberColumn(format="%,.0f"),
        })
        st.download_button(t("⬇️ 下载 CSV"), df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"receipt_history_{mlabel}.csv", mime="text/csv")

    # ========================================================
    # tab2 · 入荷实绩 vs 预定
    # ========================================================
    def _render_vs_plan():
        st.caption(t("有效预定日（优先 page30 的手动入荷预定日 · 没有则用 NST 预定日）与实际收货日的差。"
                     "发注→收货的提前期(LT)始终可算。※ 预定日未填时「差」为空。"))
        mfrag, mparams, mlabel = _month_picker("p", "m.receipt_date")
        vids = _vendor_filter("p")

        sql = (
            "SELECT m.receipt_date, m.receipt_no, m.vendor_name, m.po_number, "
            "       im.display_name, m.qty_received AS quantity, m.eff_eta, "
            "       m.eta_diff_days, m.lead_time_days "
            "FROM nst.receipt_po_match m "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = m.item_internal_id "
            "WHERE " + mfrag + " "
            + (f" AND m.vendor_id IN ({','.join(['%s']*len(vids))})" if vids else "")
            + " ORDER BY m.receipt_date DESC, m.receipt_no"
        )
        df = _df(sql, mparams + list(vids))
        if df.empty:
            st.info(t("没有符合条件的数据。"))
            return

        has_eta = df["eta_diff_days"].notna()
        lt = df["lead_time_days"].dropna()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(t("明细行数"), len(df))
        m2.metric(t("平均提前期 (天)"), f"{lt.mean():.0f}" if not lt.empty else "—")
        m3.metric(t("有预定日 行数"), int(has_eta.sum()))
        if has_eta.any():
            ontime = (df.loc[has_eta, "eta_diff_days"] <= 0).mean() * 100
            m4.metric(t("预定内入荷率"), f"{ontime:.0f}%")
        else:
            m4.metric(t("预定内入荷率"), "—")

        if not has_eta.any():
            st.info(t("「有效预定日」还没有。在 page30「📊 采购 · 在途订单 → 在途明细」填入手动入荷预定日后，"
                      "这里就会出现 预定vs实绩 的差。现在只显示提前期(发注→收货)。"))

        show = df.rename(columns={
            "receipt_date": t("收货日"), "receipt_no": t("收货单号"), "vendor_name": t("供货商"),
            "po_number": t("对应PO"), "display_name": t("品名"), "quantity": t("数量"),
            "eff_eta": t("有效预定日"), "eta_diff_days": t("预定差(天)"), "lead_time_days": t("提前期(天)"),
        })
        st.dataframe(show, use_container_width=True, hide_index=True, column_config={
            t("数量"): st.column_config.NumberColumn(format="%,.0f"),
            t("预定差(天)"): st.column_config.NumberColumn(
                format="%d", help=t("收货日−有效预定日。+ = 比预定晚 / − = 提前")),
            t("提前期(天)"): st.column_config.NumberColumn(format="%d", help=t("发注日→收货日的提前期")),
        })
        st.download_button(t("⬇️ 下载 CSV"), df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"receipt_vs_plan_{mlabel}.csv", mime="text/csv")

    # ========================================================
    # tab3 · 检收金额 / 入荷原价
    # ========================================================
    def _render_amount():
        st.caption(t("收货原价 = 逐行 单价×数量。月别检收金额推移 + item别 TOP。"))
        mfrag, mparams, mlabel = _month_picker("a", "irl.receipt_date")
        vids = _vendor_filter("a")
        vsql, vparams = _in_clause(vids)
        base_where = "WHERE " + mfrag + " " + vsql

        mdf = _df(
            "SELECT to_char(irl.receipt_date,'YYYY-MM') AS ym, "
            "       COUNT(DISTINCT irl.receipt_internal_id) AS receipts, "
            "       SUM(irl.quantity) AS qty, SUM(irl.amount) AS amount "
            "FROM nst.item_receipt_line irl " + base_where +
            " GROUP BY 1 ORDER BY 1",
            mparams + vparams)
        if mdf.empty:
            st.info(t("没有符合条件的数据。"))
            return

        tot = mdf["amount"].fillna(0).sum()
        m1, m2, m3 = st.columns(3)
        m1.metric(t("检收金额合计 (¥)"), f"¥{tot:,.0f}")
        m2.metric(t("收货单数"), int(mdf["receipts"].sum()))
        m3.metric(t("收货数量合计"), f"{mdf['qty'].fillna(0).sum():,.0f}")

        st.subheader(t("📈 月别 检收金额"))
        chart = alt.Chart(mdf).mark_bar(color="#d6000f").encode(
            x=alt.X("ym:N", title=t("月")),
            y=alt.Y("amount:Q", title=t("检收金额(¥)"), axis=alt.Axis(format=",.0f")),
            tooltip=[alt.Tooltip("ym:N", title=t("月")),
                     alt.Tooltip("amount:Q", title=t("检收金额(¥)"), format=",.0f"),
                     alt.Tooltip("receipts:Q", title=t("收货单数"))],
        ).properties(height=260)
        st.altair_chart(chart, use_container_width=True)

        st.subheader(t("🏆 item别 检收金额 TOP"))
        idf = _df(
            "SELECT im.jan, im.display_name, "
            "       SUM(irl.quantity) AS qty, SUM(irl.amount) AS amount, "
            "       COUNT(DISTINCT irl.receipt_internal_id) AS receipts "
            "FROM nst.item_receipt_line irl "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = irl.item_internal_id "
            + base_where +
            " GROUP BY im.jan, im.display_name ORDER BY amount DESC NULLS LAST LIMIT 50",
            mparams + vparams)
        show = idf.rename(columns={
            "jan": "JAN", "display_name": t("品名"), "qty": t("收货数量"),
            "amount": t("检收金额"), "receipts": t("收货单数"),
        })
        st.dataframe(show, use_container_width=True, hide_index=True, column_config={
            t("检收金额"): st.column_config.NumberColumn(format="¥%,.0f"),
            t("收货数量"): st.column_config.NumberColumn(format="%,.0f"),
        })
        st.download_button(t("⬇️ 下载 CSV"), idf.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"receipt_amount_{mlabel}.csv", mime="text/csv")

    # ========================================================
    # tab4 · 前日收货数据（昨天入荷スナップショット）
    # ========================================================
    def _render_yesterday():
        st.caption(t("前日（昨天）一天的收货明细快照 · 数据每天 06:35 自动更新后即为最新一天。"))
        target = str(dt.date.today() - dt.timedelta(days=1))
        # 兜底:前日无数据时落到最新有收货的一天
        chk = _df("SELECT 1 FROM nst.item_receipt_line WHERE receipt_date = %s LIMIT 1", [target])
        if chk.empty:
            mx = _df("SELECT MAX(receipt_date) AS d FROM nst.item_receipt_line")
            latest = mx["d"].iloc[0] if not mx.empty and mx["d"].iloc[0] is not None else None
            if latest is not None and str(latest) != target:
                st.info(t("前日暂无收货数据，下面显示最新收货日的明细：") + f" {latest}")
                target = str(latest)

        st.subheader(t("收货日") + f"：{target}")
        sql = (
            "SELECT irl.receipt_no, irl.vendor_name, im.jan, im.display_name, "
            "       irl.location, irl.quantity, irl.rate, irl.amount, pol.po_number "
            "FROM nst.item_receipt_line irl "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = irl.item_internal_id "
            "LEFT JOIN (SELECT DISTINCT po_internal_id, po_number FROM nst.purchase_order_line) pol "
            "       ON pol.po_internal_id = irl.po_internal_id "
            "WHERE irl.receipt_date = %s "
            "ORDER BY irl.receipt_no, im.display_name"
        )
        df = _df(sql, [target])
        if df.empty:
            st.info(t("这一天没有收货记录。"))
            return

        m1, m2, m3, m4 = st.columns(4)
        m1.metric(t("收货单数"), int(df["receipt_no"].nunique()))
        m2.metric(t("明细行数"), len(df))
        m3.metric(t("检收金额合计 (¥)"), f"¥{df['amount'].fillna(0).sum():,.0f}")
        m4.metric(t("供货商数"), int(df["vendor_name"].nunique()))

        show = df.rename(columns={
            "receipt_no": t("收货单号"), "vendor_name": t("供货商"), "jan": "JAN",
            "display_name": t("品名"), "location": t("仓库"), "quantity": t("数量"),
            "rate": t("单价"), "amount": t("金额"), "po_number": t("对应PO"),
        })
        st.dataframe(show, use_container_width=True, hide_index=True, column_config={
            t("金额"): st.column_config.NumberColumn(format="¥%,.0f"),
            t("单价"): st.column_config.NumberColumn(format="¥%,.1f"),
            t("数量"): st.column_config.NumberColumn(format="%,.0f"),
        })
        st.download_button(t("⬇️ 下载 CSV"), df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"receipt_{target}.csv", mime="text/csv")

    tab_hist, tab_plan, tab_amt, tab_yday = st.tabs([
        t("📜 入荷历史"), t("🎯 入荷实绩 vs 预定"), t("💴 检收金额"), t("📅 前日收货")])
    with tab_hist:
        _render_history()
    with tab_plan:
        _render_vs_plan()
    with tab_amt:
        _render_amount()
    with tab_yday:
        _render_yesterday()
