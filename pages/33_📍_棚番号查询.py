"""模块 #33 棚番号查询 · 弁天倉庫 bin 层在庫.

数据源: nst.inventory_bin_snapshot（pull_inventory_bin · 1 SKU × 1 bin = 1 行）
JOIN nst.item_master_raw 拿 item_code / jan / display_name / item_rank / maker.

JD-物流-千葉 不分 bin（3PL 聚合），不在本页范围。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("棚番号查询"), page_icon="📍", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📍 棚番号查询（弁天倉庫）"))
st.caption(t("数据源 NetSuite inventoryItemBinNumber · 弁天倉庫 1 SKU 多棚 bin 层快照"))


def _df(sql: str, params=None) -> pd.DataFrame:
    rs = conn.execute(sql, params or {}).fetchall()
    return pd.DataFrame([dict(r) for r in rs])


# 最新 snapshot_date（避免历史 snapshot 混入）
try:
    latest_row = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_bin_snapshot")
    latest_date = latest_row["d"].iloc[0] if not latest_row.empty else None
except Exception as e:
    st.error(t("⚠️ nst.inventory_bin_snapshot 读取失败（schema 未部署 / 未拉数据？）") + f"\n\n{e}")
    st.stop()

if not latest_date:
    st.info(t("尚无 bin 层快照数据。请先在元川跑 daily_pull --domains inventory_bin。"))
    st.stop()

st.caption(t("快照日期：{d}").format(d=str(latest_date)))

tab1, tab2 = st.tabs([t("🔎 按棚番号查商品"), t("🔍 按 SKU 查棚分布")])

# ============================================================
# tab1 · 按棚番号查商品
# ============================================================
with tab1:
    c1, c2 = st.columns([3, 2])
    bin_in = c1.text_input(t("棚番号（部分匹配·留空看全部棚）"), "", key="bin_filter")
    show_zero = c2.checkbox(t("含 0 库存"), value=False)

    where = "ibs.snapshot_date = %(d)s"
    params = {"d": latest_date}
    if bin_in.strip():
        where += " AND ibs.bin_number ILIKE %(b)s"
        params["b"] = f"%{bin_in.strip()}%"
    if not show_zero:
        where += " AND ibs.qty_on_hand > 0"

    try:
        df = _df(
            "SELECT ibs.bin_number, ibs.item_internal_id, im.item_code, im.jan, "
            "       im.display_name, im.maker, im.item_rank, "
            "       ibs.qty_on_hand, ibs.qty_available "
            "FROM nst.inventory_bin_snapshot ibs "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = ibs.item_internal_id "
            f"WHERE {where} "
            "ORDER BY ibs.bin_number, im.item_code",
            params,
        )
    except Exception as e:
        st.error(t("⚠️ 查询失败") + f"\n\n{e}")
        st.stop()

    if df.empty:
        st.info(t("无匹配数据（棚番号填写错误？或该棚全部 0 库存？）"))
    else:
        df["qty_on_hand"] = pd.to_numeric(df["qty_on_hand"], errors="coerce").fillna(0)
        df["qty_available"] = pd.to_numeric(df["qty_available"], errors="coerce").fillna(0)

        k1, k2, k3 = st.columns(3)
        k1.metric(t("命中棚数"), f"{df['bin_number'].nunique():,}")
        k2.metric(t("商品行数"), f"{len(df):,}")
        k3.metric(t("在庫合計"), f"{df['qty_on_hand'].sum():,.0f}")

        st.divider()
        disp = df[["bin_number", "item_code", "jan", "display_name", "maker",
                   "item_rank", "qty_on_hand", "qty_available"]].rename(columns={
            "bin_number": t("棚番号"), "item_code": t("商品コード"),
            "jan": t("JAN"), "display_name": t("商品名"), "maker": t("メーカー"),
            "item_rank": t("等级"), "qty_on_hand": t("手持"),
            "qty_available": t("利用可能"),
        })
        st.dataframe(disp, hide_index=True, use_container_width=True, height=560)
        st.download_button(t("📥 CSV 下载"),
                           disp.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"bin_query_{latest_date}.csv", mime="text/csv")

# ============================================================
# tab2 · 按 SKU 查棚分布
# ============================================================
with tab2:
    sku_in = st.text_input(t("商品コード / JAN（部分匹配）"), "", key="sku_filter")
    if sku_in.strip():
        try:
            df2 = _df(
                "SELECT ibs.bin_number, ibs.item_internal_id, im.item_code, im.jan, "
                "       im.display_name, ibs.qty_on_hand, ibs.qty_available "
                "FROM nst.inventory_bin_snapshot ibs "
                "JOIN nst.item_master_raw im ON im.internal_id = ibs.item_internal_id "
                "WHERE ibs.snapshot_date = %(d)s "
                "  AND (im.item_code ILIKE %(s)s OR im.jan ILIKE %(s)s) "
                "  AND ibs.qty_on_hand > 0 "
                "ORDER BY im.item_code, ibs.bin_number",
                {"d": latest_date, "s": f"%{sku_in.strip()}%"},
            )
        except Exception as e:
            st.error(t("⚠️ 查询失败") + f"\n\n{e}")
            st.stop()

        if df2.empty:
            st.info(t("无匹配 SKU（或该 SKU 在弁天 0 库存）"))
        else:
            df2["qty_on_hand"] = pd.to_numeric(df2["qty_on_hand"], errors="coerce").fillna(0)
            df2["qty_available"] = pd.to_numeric(df2["qty_available"], errors="coerce").fillna(0)
            disp2 = df2[["item_code", "jan", "display_name", "bin_number",
                         "qty_on_hand", "qty_available"]].rename(columns={
                "item_code": t("商品コード"), "jan": t("JAN"),
                "display_name": t("商品名"), "bin_number": t("棚番号"),
                "qty_on_hand": t("手持"), "qty_available": t("利用可能"),
            })
            st.dataframe(disp2, hide_index=True, use_container_width=True, height=480)
            st.download_button(t("📥 CSV 下载"),
                               disp2.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"sku_bin_{latest_date}.csv", mime="text/csv",
                               key="sku_csv")
    else:
        st.info(t("输入商品コード或 JAN 查询该 SKU 在弁天的棚分布"))
