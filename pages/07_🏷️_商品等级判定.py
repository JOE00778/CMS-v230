import streamlit as st
from shared.i18n import t, lang_selector, get_lang
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
from shared.rank_settings import DEFAULT_RANK_PARAMS, load_rank_params, save_rank_params

st.set_page_config(page_title=t("商品等级判定"), page_icon="🏷️", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
st.title(t("🏷️ 商品等级判定"))
_RP = load_rank_params()
st.caption(t(
    "基于销售前 {top:.0f}% × 利润率 ≥{mg:.0f}% 的 4 档判定 (Aランク/Bランク/Cランク/取扱中止) · "
    "财年 3 月开始 (Q1=3-5月 / Q2=6-8月 / Q3=9-11月 / Q4=12-2月) · 阈值可在「⚙️ 判定原理与参数」tab 修改"
).format(top=_RP["top_pct"] * 100, mg=_RP["a_margin"] * 100))

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
tab1, tab2, tab3 = st.tabs([t('🆕 新建议'), t('📜 历史回看'), t('⚙️ 判定原理与参数')])

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


# ============================================================
# Tab 3: 判定原理与参数（Boss 2026-09-15「把原理拉出来，做成可设置的」）
#   上: 現在の保存値で原理を動的表示 · 下: 編集 + 保存（data/files/rank_params.json）
#   保存後の「生成等级建议」から新パラメータで判定される（既存の履歴は書き換えない）
# ============================================================
with tab3:
    _p = load_rank_params()
    _ja7 = get_lang() == "ja"
    st.markdown("##### " + ("📖 判定ロジック（現在の設定値で表示）" if _ja7 else "📖 判定原理（按当前参数显示）"))
    _top = _p["top_pct"] * 100
    _mg = _p["a_margin"] * 100
    _n = int(_p["no_sales_months"])
    if _ja7:
        st.markdown(f"""
**データ**: NST 月次売上（売上額・数量・粗利）× 商品主档（取扱区分・現行ランク）。対象期間は「新建议」tab で選ぶ月 or 財年四半期（3 月起算）。

**判定順（上から順に見て、当たったところで確定）**

1. NST 取扱区分 が「取扱中止 / メーカー取扱中止」 → **取扱中止**
2. 直近 **{_n} ヶ月** の販売数 = 0 → **取扱中止**
3. 売上額の降順累計占比 ≤ **{_top:.0f}%**（頭部品）**かつ** 粗利率 ≥ **{_mg:.0f}%** → **Aランク**
4. 頭部品だが粗利率 < {_mg:.0f}% → **Bランク**
5. それ以外 → **Cランク**

**補則**: 現行ランクが取扱中止の商品は {"A/B/C へ戻さない（吸収態）" if _p["stop_absorbing"] else "再判定で A/B/C に戻り得る（吸収態オフ）"}。主档にランクの無い商品は NEW と表示。
粗利率 = 粗利 ÷ 売上（定義原価ベース · page04/05 と同じ口径）。

**発注目安（参考値）**: 再発注点 = 月販 × 進貨周期(月) × 安全係数 · 安全係数 A {_p["safety_a"]} / B {_p["safety_b"]} / C {_p["safety_c"]} / 停 {_p["safety_stop"]} · 進貨周期既定 {int(_p["lead_days"])} 日
""")
    else:
        st.markdown(f"""
**数据**：NST 月度销售（销售额、数量、毛利）× 商品主档（取扱区分、现等级）。期间在「新建议」tab 里选月度或财年季度（3 月起）。

**判定顺序（从上往下，命中即定）**

1. NST 取扱区分 =「取扱中止 / メーカー取扱中止」 → **取扱中止**
2. 最近 **{_n} 个月** 销量 = 0 → **取扱中止**
3. 销售额降序累计占比 ≤ **{_top:.0f}%**（头部品）**且** 毛利率 ≥ **{_mg:.0f}%** → **Aランク**
4. 头部品但毛利率 < {_mg:.0f}% → **Bランク**
5. 其余 → **Cランク**

**补则**：现等级已是取扱中止的商品{"不回升到 A/B/C（吸收态）" if _p["stop_absorbing"] else "可在重算时回到 A/B/C（吸收态关闭）"}。主档没有等级的显示 NEW。
毛利率 = 毛利 ÷ 销售额（定义原价口径，与 page04/05 一致）。

**订货参考**：再订货点 = 月销 × 进货周期(月) × 安全系数 · 安全系数 A {_p["safety_a"]} / B {_p["safety_b"]} / C {_p["safety_c"]} / 停 {_p["safety_stop"]} · 进货周期默认 {int(_p["lead_days"])} 天
""")

    st.divider()
    st.markdown("##### " + ("🛠️ パラメータ編集" if _ja7 else "🛠️ 参数设置"))
    c1, c2, c3 = st.columns(3)
    _top_in = c1.number_input(("頭部累計占比 ライン (%)" if _ja7 else "头部累计占比线 (%)"),
                              min_value=1.0, max_value=100.0, step=1.0,
                              value=float(_p["top_pct"] * 100), key="rp_top")
    _mg_in = c2.number_input(("A ランク粗利率 ライン (%)" if _ja7 else "A 档毛利率线 (%)"),
                             min_value=0.0, max_value=100.0, step=1.0,
                             value=float(_p["a_margin"] * 100), key="rp_mg")
    _n_in = c3.number_input(("無動销 → 取扱中止 の月数" if _ja7 else "无动销判停月数"),
                            min_value=1, max_value=24, step=1,
                            value=int(_p["no_sales_months"]), key="rp_n")
    _abs_in = st.checkbox(("現行取扱中止は A/B/C へ戻さない（吸収態）" if _ja7
                           else "现等级为取扱中止的不回升到 A/B/C（吸收态）"),
                          value=bool(_p["stop_absorbing"]), key="rp_abs")
    st.caption("発注目安の係数" if _ja7 else "订货参考系数")
    d1, d2, d3, d4, d5 = st.columns(5)
    _sa = d1.number_input("A", min_value=0.0, max_value=5.0, step=0.1, value=float(_p["safety_a"]), key="rp_sa")
    _sb = d2.number_input("B", min_value=0.0, max_value=5.0, step=0.1, value=float(_p["safety_b"]), key="rp_sb")
    _sc = d3.number_input("C", min_value=0.0, max_value=5.0, step=0.1, value=float(_p["safety_c"]), key="rp_sc")
    _ss = d4.number_input(("停" if not _ja7 else "取扱中止"), min_value=0.0, max_value=5.0, step=0.1,
                          value=float(_p["safety_stop"]), key="rp_ss")
    _ld = d5.number_input(("進貨周期 既定(日)" if _ja7 else "进货周期默认(天)"), min_value=1, max_value=365,
                          step=1, value=int(_p["lead_days"]), key="rp_ld")
    b1, b2 = st.columns([1, 1])
    if b1.button(("💾 保存" if _ja7 else "💾 保存参数"), type="primary", key="rp_save"):
        try:
            _saved = save_rank_params({
                "top_pct": _top_in / 100.0, "a_margin": _mg_in / 100.0,
                "no_sales_months": _n_in, "stop_absorbing": _abs_in,
                "safety_a": _sa, "safety_b": _sb, "safety_c": _sc, "safety_stop": _ss,
                "lead_days": _ld,
            })
            st.success(("保存しました。次回「生成等级建议」から適用されます。" if _ja7
                        else "已保存。下次点「生成等级建议」即按新参数判定。")
                       + f" top={_saved['top_pct']:.2f} margin={_saved['a_margin']:.2f} n={_saved['no_sales_months']}")
            st.rerun()
        except Exception as _e:  # noqa: BLE001
            st.error(("保存失敗: " if _ja7 else "保存失败: ") + str(_e))
    if b2.button(("↩️ 既定値に戻す" if _ja7 else "↩️ 恢复默认值"), key="rp_reset"):
        try:
            save_rank_params(DEFAULT_RANK_PARAMS)
            st.success("既定値に戻しました" if _ja7 else "已恢复默认值")
            st.rerun()
        except Exception as _e:  # noqa: BLE001
            st.error(("失敗: " if _ja7 else "失败: ") + str(_e))
    _diff = {k: (v, _p[k]) for k, v in DEFAULT_RANK_PARAMS.items() if _p[k] != v}
    st.caption((("既定値と異なる項目: " if _ja7 else "与默认值不同的项：")
                + (", ".join(f"{k} {d}→{c}" for k, (d, c) in _diff.items()) if _diff
                   else ("なし（すべて既定値）" if _ja7 else "无（全部为默认值）"))))
