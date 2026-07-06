import streamlit as st
from shared.i18n import t, lang_selector
from shared.i18n_columns import localize_df
import pandas as pd
import sqlite3
from pathlib import Path
from datetime import datetime
from modules.rank_classifier.proposal import generate_proposal
from data_warehouse.templates.nst_item_master import (
    COL_RANK,
    COL_ITEM_CODE,
    build_nst_master_csv,
    dated_filename,
)
from shared.db import get_connection
from shared.v2_browser import render_v2_quickview

st.set_page_config(page_title=t("商品等级判定"), page_icon="🏷️", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
render_v2_quickview(get_connection(), key_prefix="page07_")
st.title(t("🏷️ 商品等级判定"))
st.caption(t(
    "基于销售前 80% × 利润率 ≥59% 的 4 档判定 (Aランク/Bランク/Cランク/取扱中止) · "
    "财年 3 月开始 (Q1=3-5月 / Q2=6-8月 / Q3=9-11月 / Q4=12-2月)"
))

DB = Path(__file__).parent.parent / "data_warehouse" / "warehouse.db"

# ============================================================
# 财年季度定义 (Boss 决定: 公司财年 3 月开始)
# Q1: 3-5 月  代表月 = 4月
# Q2: 6-8 月  代表月 = 7月
# Q3: 9-11月  代表月 = 10月
# Q4: 12-2 月 代表月 = 1月 (跨年)
# 注: FY = 财年起始年份。如 FY2026-Q1 = 2026年3-5月
# ============================================================
QUARTER_TO_MONTH = {
    'FY2026-Q1': '2026-04',  # 2026 年 3-5 月
    'FY2026-Q2': '2026-07',  # 2026 年 6-8 月
    'FY2026-Q3': '2026-10',  # 2026 年 9-11 月
    'FY2026-Q4': '2027-01',  # 2026 年 12 月 - 2027 年 2 月
    'FY2025-Q4': '2026-01',  # 2025 年 12 月 - 2026 年 2 月 (跨年保留)
}

QUARTER_RANGES = {
    'FY2026-Q1': '2026-03 ~ 2026-05',
    'FY2026-Q2': '2026-06 ~ 2026-08',
    'FY2026-Q3': '2026-09 ~ 2026-11',
    'FY2026-Q4': '2026-12 ~ 2027-02',
    'FY2025-Q4': '2025-12 ~ 2026-02',
}

# 月度选项 (近 12 个月)
from datetime import date, timedelta as _td
def _gen_months(n=12):
    today = date.today().replace(day=1)
    months = []
    cur = today
    for _ in range(n):
        months.append(cur.strftime("%Y-%m"))
        # 上个月
        prev_last = cur - _td(days=1)
        cur = prev_last.replace(day=1)
    return months

MONTH_OPTIONS = _gen_months(12)

# Tab 1: 生成新建议  Tab 2: 历史回看
tab1, tab2 = st.tabs([t('🆕 新建议'), t('📜 历史回看')])

with tab1:
    if 'proposal_data' not in st.session_state:
        st.session_state.proposal_data = None
    if 'proposal_period_label' not in st.session_state:
        st.session_state.proposal_period_label = None

    # 月度 → 期间 (start/end) 映射: 当月 1 号 ~ 月末
    def _month_range(ym: str) -> tuple[str, str]:
        """'2026-04' → ('2026-04-01', '2026-04-30')"""
        from datetime import date as _date, timedelta as _td
        y, m = map(int, ym.split("-"))
        first = _date(y, m, 1)
        if m == 12:
            next_first = _date(y + 1, 1, 1)
        else:
            next_first = _date(y, m + 1, 1)
        last = next_first - _td(days=1)
        return first.isoformat(), last.isoformat()

    # 季度 → 期间 (start/end) 范围
    Q_RANGES_DATE = {
        'FY2026-Q1': ('2026-03-01', '2026-05-31'),
        'FY2026-Q2': ('2026-06-01', '2026-08-31'),
        'FY2026-Q3': ('2026-09-01', '2026-11-30'),
        'FY2026-Q4': ('2026-12-01', '2027-02-28'),
        'FY2025-Q4': ('2025-12-01', '2026-02-28'),
    }

    # 两个粒度并排:
    g1, g2 = st.columns(2)
    with g1:
        st.markdown(f"**{t('📅 按月度')}**")
        sel_month = st.selectbox(
            t("月度"), MONTH_OPTIONS, index=0, key="rank_month_sel",
        )
        if st.button(t("🔄 按月度 生成等级建议"), use_container_width=True, key="btn_rank_month"):
            ms, me = _month_range(sel_month)
            with st.spinner(t("跑 generate_proposal...")):
                data = generate_proposal(
                    sel_month, str(DB),
                    period_start=ms, period_end=me,
                )
                st.session_state.proposal_data = data
                st.session_state.proposal_period_label = f"月度 {sel_month} ({ms} ~ {me})"
            st.success(t(f"✓ [月度 {sel_month}] 已生成 {len(data)} 条建议"))

    with g2:
        st.markdown(f"**{t('📆 按季度 (财年 3 月开始)')}**")
        q = st.selectbox(
            t("季度"),
            ['FY2026-Q1', 'FY2026-Q2', 'FY2026-Q3', 'FY2026-Q4', 'FY2025-Q4'],
            index=0,
            format_func=lambda x: f"{x} ({QUARTER_RANGES.get(x, '?')})",
            key="rank_q_sel",
        )
        if st.button(t("🔄 按季度 生成等级建议"), use_container_width=True, type="primary", key="btn_rank_q"):
            qs, qe = Q_RANGES_DATE.get(q, (None, None))
            with st.spinner(t("跑 generate_proposal...")):
                data = generate_proposal(
                    q, str(DB),
                    period_start=qs, period_end=qe,
                )
                st.session_state.proposal_data = data
                st.session_state.proposal_period_label = f"季度 {q} ({QUARTER_RANGES.get(q,'?')})"
            st.success(t(f"✓ [季度 {q}] 已生成 {len(data)} 条建议"))

    # 当前已选期间提示
    if st.session_state.proposal_period_label:
        st.info(t(f"📌 当前预览期间: {st.session_state.proposal_period_label}"))

    if st.session_state.proposal_data:
        df = pd.DataFrame(st.session_state.proposal_data)

        # KPI 卡片 (4 档 A/B/C/停售)
        c1, c2, c3, c4, c5 = st.columns(5)
        c6, c7, c8 = st.columns(3)

        if 'new_rank' in df.columns:
            counts = df['new_rank'].value_counts()
            # 等级値は NST 合法値（中日 UI 共通・翻訳しない）
            c1.metric("Aランク", int(counts.get('Aランク', 0)))
            c2.metric("Bランク", int(counts.get('Bランク', 0)))
            c3.metric("Cランク", int(counts.get('Cランク', 0)))
            c4.metric("取扱中止", int(counts.get('取扱中止', 0)), help=t("含 3 个月无动销"))
            change_n = (df['old_rank'] != df['new_rank']).sum() if 'old_rank' in df.columns else 0
            c5.metric(t("有变化"), int(change_n))

        # 趋势计数
        if 'trend' in df.columns:
            trend_counts = df['trend'].value_counts()
            c6.metric(t("⬆️ 升级"), int(trend_counts.get('⬆️ 升级', 0)))
            c7.metric(t("⬇️ 降级"), int(trend_counts.get('⬇️ 降级', 0)))
            c8.metric(t("➡️ 维持"), int(trend_counts.get('➡️ 维持', 0)))

        # 过滤区块
        st.markdown(t("### 过滤"))
        f0, f1, f2, f3 = st.columns(4)
        old_rank_filter = f0.multiselect(
            t("旧档"),
            options=sorted(df['old_rank'].unique()) if 'old_rank' in df.columns else [],
            default=[]
        )
        rank_filter = f1.multiselect(
            t("新档"),
            options=sorted(df['new_rank'].unique()) if 'new_rank' in df.columns else [],
            default=[]
        )
        trend_filter = f2.multiselect(
            t("趋势"),
            options=sorted(df['trend'].unique()) if 'trend' in df.columns else [],
            default=[]
        )
        change_only = f3.checkbox(t("只看有变化"), value=True)

        view = df.copy()
        if old_rank_filter:
            view = view[view['old_rank'].isin(old_rank_filter)]
        if rank_filter:
            view = view[view['new_rank'].isin(rank_filter)]
        if trend_filter:
            view = view[view['trend'].isin(trend_filter)]
        if change_only and 'old_rank' in view.columns:
            view = view[view['old_rank'] != view['new_rank']]

        st.markdown(t(f"### 建议清单（{len(view)} 条）"))

        # 显示表（选择显示列）
        view_display = view.copy()
        display_cols = ['sku', 'name', 'old_rank', 'new_rank', 'trend', 'sales', 'margin', 'rank_pct']
        display_cols = [c for c in display_cols if c in view_display.columns]

        # 密度 + 列显示控件 (Phase 2A)
        _dctl1, _dctl2 = st.columns([1, 3])
        with _dctl1:
            _density = st.radio(
                t("密度"),
                [t("紧凑"), t("标准"), t("宽松")],
                horizontal=True,
                index=1,
                key=f"density_{__file__}",
                label_visibility="collapsed",
            )
        _density_class = {
            t("紧凑"): "density-compact",
            t("标准"): "",
            t("宽松"): "density-comfy",
        }.get(_density, "")

        with st.expander(t("⚙️ 显示列设置")):
            _picked_cols = st.multiselect(
                t("选择展示列"), display_cols, default=display_cols,
                key=f"colpick_{__file__}",
            )
        _final_cols = _picked_cols if _picked_cols else display_cols

        st.markdown(f'<div class="{_density_class}">', unsafe_allow_html=True)
        st.dataframe(localize_df(view_display[_final_cols]), use_container_width=True, height=400)
        st.markdown('</div>', unsafe_allow_html=True)

        # 确认操作区块
        st.markdown(t("### ✅ 确认变更"))
        n_to_confirm = len(view)
        st.warning(t(f"⚠️ 即将变更 {n_to_confirm} 个 SKU 的等级"))

        # 二次确认：checkbox + button
        confirmed = st.checkbox(t("我已审阅并确认"))
        if confirmed:
            if st.button(t("✅ 写入 rank_history + 导出 CSV")):
                # 找出有变化的行
                rows_to_insert = []
                for _, row in view.iterrows():
                    if row.get('old_rank') != row.get('new_rank'):
                        rows_to_insert.append((
                            row.get('sku'),
                            q,
                            row.get('old_rank'),
                            str(row.get('new_rank')),
                            'BOSS',
                            datetime.now().isoformat()
                        ))

                # 写入 rank_history
                conn = get_connection()
                if rows_to_insert:
                    conn.executemany(
                        """INSERT OR REPLACE INTO rank_history
                           (sku, quarter, old_rank, new_rank, changed_by, changed_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        rows_to_insert
                    )
                    conn.commit()
                conn.close()

                # 导出 NST 上传模板 CSV（第一列 型番 + 「商品ランク」· 仅有变化的 SKU）
                # NST CSV Import 以「型番」做匹配键(Boss 2026-05-24)，非 Internal ID
                csv_rows = [
                    {COL_ITEM_CODE: r["sku"], COL_RANK: r["new_rank"]}
                    for _, r in view.iterrows()
                    if r.get("old_rank") != r.get("new_rank") and pd.notna(r.get("sku"))
                ]
                # 存 session_state：download_button 必须渲染在 if st.button() 之外，
                # 否则点下载触发 rerun → button 返回 False → 下载控件消失 → 无法下载
                st.session_state["rank_csv_bytes"] = build_nst_master_csv(
                    csv_rows, [COL_RANK], id_label=COL_ITEM_CODE
                )
                st.session_state["rank_csv_stat"] = (len(rows_to_insert), len(csv_rows))

            # 渲染在 button 块外：rerun 后仍存在，下载才能完成
            if st.session_state.get("rank_csv_bytes") is not None:
                _n_ins, _n_csv = st.session_state.get("rank_csv_stat", (0, 0))
                st.success(t(f"✅ 写入 rank_history {_n_ins} 条 + 生成 CSV {_n_csv} 行"))
                st.download_button(
                    t("📥 下载 rank_update.csv"),
                    st.session_state["rank_csv_bytes"],
                    file_name=dated_filename(),
                    mime='text/csv',
                    key="dl_rank_csv",
                )

with tab2:
    conn = get_connection()
    _hist_rows = conn.execute(
        "SELECT * FROM rank_history ORDER BY changed_at DESC"
    ).fetchall()
    history = pd.DataFrame([dict(r) for r in _hist_rows])
    conn.close()

    if history.empty:
        st.info(t("暂无历史变更"))
    else:
        # 按季度过滤
        quarters = sorted(history['quarter'].unique().tolist(), reverse=True)
        sel_q = st.multiselect(t("季度筛选"), options=quarters, default=quarters)
        view2 = history[history['quarter'].isin(sel_q)] if sel_q else history
        st.dataframe(localize_df(view2), use_container_width=True, height=500)
