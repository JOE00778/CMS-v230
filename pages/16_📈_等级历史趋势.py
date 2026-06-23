import streamlit as st
from shared.i18n import t, lang_selector
from shared.i18n_columns import localize_df
import pandas as pd
import sqlite3
from pathlib import Path

st.set_page_config(page_title=t("等级历史趋势"), page_icon="📈", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()

from shared.db import get_connection, DB_PATH
DB = DB_PATH
conn = get_connection()

st.title(t("📈 等级历史趋势"))
st.caption(t("跨季度等级变化跟踪 · 升级/降级/稳定 SKU 分析"))

# 季度选择 (走 PG adapter)
quarters = [
    r["quarter"] for r in conn.execute(
        "SELECT DISTINCT quarter FROM rank_history ORDER BY quarter DESC"
    ).fetchall()
]

if not quarters:
    st.info(t("暂无历史数据。请先在「🏷️ 商品等级判定」page 确认变更。"))
    st.stop()

# 月份选项（按变更时间 changed_at 的 YYYY-MM）
months = [
    r["m"] for r in conn.execute(
        "SELECT DISTINCT to_char(changed_at,'YYYY-MM') AS m "
        "FROM rank_history WHERE changed_at IS NOT NULL ORDER BY m DESC"
    ).fetchall()
]

_fq, _fm = st.columns(2)
sel_q = _fq.multiselect(t("季度筛选"), quarters,
                        default=quarters[:3] if len(quarters) >= 3 else quarters)
sel_m = _fm.multiselect(t("月份筛选"), months, placeholder=t("全部"))

if not sel_q:
    st.warning(t("请至少选择一个季度。"))
    st.stop()

# 季度白名单防御后 f-string 拼接 (sel_q 来自上面的 quarters，可信)
import re as _re
_safe_q = [_re.sub(r"[^A-Za-z0-9_-]", "", str(q))[:20] for q in sel_q]
_in_clause = ",".join(f"'{q}'" for q in _safe_q if q)
_rank_sql = (
    f"SELECT rh.*, im.display_name, "
    f"       COALESCE(inv.qty_on_hand, 0) AS qty_on_hand "
    f"FROM rank_history rh "
    f"LEFT JOIN nst.item_master_raw im ON im.item_code = rh.sku "
    f"LEFT JOIN ("
    f"    SELECT item_internal_id, qty_on_hand "
    f"    FROM nst.inventory_snapshot "
    f"    WHERE snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)"
    f") inv ON inv.item_internal_id = im.internal_id "
    f"WHERE rh.quarter IN ({_in_clause}) "
    f"ORDER BY rh.changed_at DESC"
)
from shared.cache import cached_df, data_version
df = cached_df(conn, _rank_sql, ver=data_version("basic", "inventory"))

if df.empty:
    st.info(t("选定季度内无变更记录。"))
    st.stop()

# 月份筛选（按 changed_at 的 YYYY-MM·空选=全部）
if sel_m:
    df = df[pd.to_datetime(df["changed_at"]).dt.strftime("%Y-%m").isin(sel_m)]
    if df.empty:
        st.info(t("选定月份内无变更记录。"))
        st.stop()

# 細筛：多 SKU/商品名 搜索 + 旧/新等级（空选=全部）
fc1, fc2, fc3 = st.columns([2, 1, 1])
_sku_kw = fc1.text_input(t("SKU / 商品名 搜索（多个用空格/逗号/换行分隔）"),
                         placeholder=t("留空=全部"))
_old_opts = sorted(df["old_rank"].dropna().unique().tolist())
_new_opts = sorted(df["new_rank"].dropna().unique().tolist())
_old_sel = fc2.multiselect(t("旧等级"), _old_opts, placeholder=t("全部"))
_new_sel = fc3.multiselect(t("新等级"), _new_opts, placeholder=t("全部"))

if _sku_kw.strip():
    _terms = [x.strip() for x in _re.split(r"[\s,，、]+", _sku_kw) if x.strip()]
    if _terms:
        _mask = pd.Series(False, index=df.index)
        for _term in _terms:
            _mask |= df["sku"].astype(str).str.contains(_term, case=False, na=False, regex=False)
            _mask |= df["display_name"].astype(str).str.contains(_term, case=False, na=False, regex=False)
        df = df[_mask]
if _old_sel:
    df = df[df["old_rank"].isin(_old_sel)]
if _new_sel:
    df = df[df["new_rank"].isin(_new_sel)]

if df.empty:
    st.info(t("筛选后无记录。请放宽 SKU / 等级筛选。"))
    st.stop()

# 等级评分映射
rank_score = {
    'A': 4, 'Aランク': 4,
    'B': 3, 'Bランク': 3,
    'C': 2, 'Cランク': 2,
    'NEW': 1, '新商品': 1,
    '停售': 0, '取扱中止': 0
}

df['old_score'] = df['old_rank'].map(rank_score).fillna(1.5)
df['new_score'] = df['new_rank'].map(rank_score).fillna(1.5)

up_count = (df['new_score'] > df['old_score']).sum()
down_count = (df['new_score'] < df['old_score']).sum()
stable_count = (df['new_score'] == df['old_score']).sum()

# KPI 卡片
c1, c2, c3, c4 = st.columns(4)
c1.metric(t("总变化"), len(df))
c2.metric(t("⬆️ 升级"), int(up_count))
c3.metric(t("⬇️ 降级"), int(down_count))
c4.metric(t("➡️ 稳定"), int(stable_count))

st.divider()

# 历史变化表
st.subheader(t("变更明细"))
display_df = df[['sku', 'display_name', 'quarter', 'old_rank', 'new_rank',
                 'qty_on_hand', 'changed_at']].copy()
display_df['qty_on_hand'] = display_df['qty_on_hand'].fillna(0).astype(int)
display_df = display_df.sort_values('changed_at', ascending=False)

st.dataframe(
    localize_df(display_df.head(500)),
    use_container_width=True,
    height=400,
    hide_index=True
)
st.caption(t(f"显示前 500 / 共 {len(df)} 条记录"))

st.divider()

# SKU 详情下钻
st.subheader(t("🔍 单 SKU 历史下钻"))
unique_skus = sorted(df['sku'].unique().tolist())

if unique_skus:
    sel_sku = st.selectbox(t("选 SKU"), unique_skus, key="sku_select")
    sku_history = df[df['sku'] == sel_sku].sort_values('changed_at', ascending=False)

    st.dataframe(
        localize_df(sku_history[['quarter', 'old_rank', 'new_rank', 'changed_at']]),
        use_container_width=True,
        hide_index=True
    )

    # 统计信息
    score_changes = sku_history['new_score'].iloc[0] - sku_history['old_score'].iloc[-1] if len(sku_history) > 1 else 0
    st.caption(t(f"总体变化：{score_changes:+.1f} 等级分（{sku_history['old_rank'].iloc[-1]} → {sku_history['new_rank'].iloc[0]}）"))

conn.close()
