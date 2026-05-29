"""模块 #32 系统参数设定 · 多模块阈值/白名单集中管理（系统设置·二级密码保护）。

每个业务模块独立 tab：
  - tab1 运营调整建议    → modules.operation_advice.settings
  - tab2 発注 AI v2       → shared.order_settings
  - tab3 NST vs JDL 对账   → shared.jdl_recon_settings（差异档位阈值）
  - tab4 数据同步         → nst.pull_schedule + jdl.pull_schedule（手动触发）
  - tab5 货架用途指定     → nst.bin_category (PG · 弁天棚号用途人工分类)
  - tab6 输出供应商名单   → nst.po_export_vendor (PG · 仕入先白名单)
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from shared.i18n import t, lang_selector

st.set_page_config(page_title=t("系统参数设定"), page_icon="⚙️", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()

from shared.db import get_connection
conn = get_connection()


def _df(sql: str, params=None) -> pd.DataFrame:
    """单一 SELECT 查询，失败回滚事务保证后续 query 可用。"""
    try:
        cur = conn.execute(sql, params or {})
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


st.title(t("⚙️ 系统参数设定"))
st.caption(t("仅授权人员可改 · 各业务模块阈值/白名单独立 tab 管理"))

tab1, tab2, tab3, tab4, tab5, tab_sync = st.tabs([
    t("💡 运营调整建议"),
    t("📦 発注 AI v2"),
    t("📊 NST vs JDL 账实对账"),
    t("🏷️ 货架用途指定"),
    t("🏢 输出供应商名单"),
    t("🔄 数据同步"),
])

# ============================================================
# tab1 · 运营调整建议（毛利 × 周转 双轴阈值）
# ============================================================
with tab1:
    from modules.operation_advice.settings import load_thresholds, save_thresholds

    st.subheader(t("双轴阈值"))
    st.caption(t("毛利率(%) × 月周转率 → 5 档建议的分界线"))

    _th = load_thresholds()
    c1, c2, c3, c4 = st.columns(4)
    _ml = c1.number_input(t("毛利 低界 (%)"), value=float(_th["margin_low"]),
                          step=1.0, min_value=0.0, key="op_margin_low")
    _mh = c2.number_input(t("毛利 高界 (%)"), value=float(_th["margin_high"]),
                          step=1.0, min_value=0.0, key="op_margin_high")
    _tl = c3.number_input(t("周转 低界"), value=float(_th["turn_low"]),
                          step=0.1, min_value=0.0, format="%.2f", key="op_turn_low")
    _thh = c4.number_input(t("周转 高界"), value=float(_th["turn_high"]),
                           step=0.1, min_value=0.0, format="%.2f", key="op_turn_high")

    if st.button(t("💾 保存阈值"), type="primary", key="save_op"):
        if _ml >= _mh:
            st.error(t("毛利低界必须 < 高界"))
        elif _tl >= _thh:
            st.error(t("周转低界必须 < 高界"))
        else:
            save_thresholds({"margin_low": _ml, "margin_high": _mh,
                             "turn_low": _tl, "turn_high": _thh})
            st.success(t("✅ 已保存。到「💡 运营调整建议」点【🔄 重新计算】生效。"))

    st.divider()
    st.caption(
        t("当前生效阈值") + f"：毛利 {_th['margin_low']:.0f}/{_th['margin_high']:.0f}% · "
        f"周转 {_th['turn_low']}/{_th['turn_high']}"
    )

# ============================================================
# tab2 · 発注 AI v2（月完売率发注策略）
# ============================================================
with tab2:
    from shared.order_settings import load_sellthrough_params, save_sellthrough_params

    st.subheader(t("月完売率发注策略"))
    st.caption(t("月完売率分档调整发注量：≥高界→加大 / 中间→正常 / <低界→减少"))

    _sp = load_sellthrough_params()
    s1, s2 = st.columns(2)
    _sh = s1.number_input(t("完売率 高界（加大订货）"), value=float(_sp["high"]),
                          step=0.05, min_value=0.0, max_value=5.0, format="%.2f",
                          key="st_high")
    _sl = s2.number_input(t("完売率 低界（减少订货）"), value=float(_sp["low"]),
                          step=0.05, min_value=0.0, max_value=5.0, format="%.2f",
                          key="st_low")
    f1, f2, f3 = st.columns(3)
    _fh = f1.number_input(t("系数·加大（≥高界）"), value=float(_sp["factor_high"]),
                          step=0.1, min_value=0.0, format="%.2f", key="st_fh")
    _fm = f2.number_input(t("系数·正常（中间）"), value=float(_sp["factor_mid"]),
                          step=0.1, min_value=0.0, format="%.2f", key="st_fm")
    _fl = f3.number_input(t("系数·减少（<低界）"), value=float(_sp["factor_low"]),
                          step=0.1, min_value=0.0, format="%.2f", key="st_fl")

    if st.button(t("💾 保存发注策略"), type="primary", key="save_st"):
        if _sl >= _sh:
            st.error(t("完売率低界必须 < 高界"))
        else:
            save_sellthrough_params({"high": _sh, "low": _sl, "factor_high": _fh,
                                     "factor_mid": _fm, "factor_low": _fl})
            st.success(t("✅ 已保存。到「📦 发注AI v2」点【执行发注计算】生效。"))

    st.divider()
    st.caption(
        t("当前生效") + f"：完売率 ≥{_sp['high']:.2f}→×{_sp['factor_high']:.1f}（加大） · "
        f"≥{_sp['low']:.2f}→×{_sp['factor_mid']:.1f}（正常） · "
        f"<{_sp['low']:.2f}→×{_sp['factor_low']:.1f}（减少）"
    )

# ============================================================
# tab3 · NST vs JDL 账实对账（差异档位阈值）
# ============================================================
with tab3:
    from shared.jdl_recon_settings import load_recon_params, save_recon_params

    st.subheader(t("差异档位阈值"))
    st.caption(t(
        "|NST 账面 − JDL 实物| 在阈值内 = MINOR_DIFF（轻微差异）· 超出 = MAJOR_DIFF（必须查）。"
        "两个阈值满足任一即 MINOR。"
    ))

    _rp = load_recon_params()
    r1, r2 = st.columns(2)
    _rabs = r1.number_input(
        t("绝对值阈值（差 N 件以内 = MINOR）"),
        value=int(_rp["minor_abs_threshold"]), step=1, min_value=0,
        key="recon_abs",
        help=t("建议 1-3。差异 ≤ 此值算轻微（盘点误差 / 单件订单时间差）"),
    )
    _rpct = r2.number_input(
        t("百分比阈值（差占 NST 账面 X% 以内 = MINOR）"),
        value=float(_rp["minor_pct_threshold"]), step=0.5, min_value=0.0, max_value=100.0,
        format="%.1f", key="recon_pct",
        help=t("默认 0 = 不启用比例判定。设 5 表示库存 100 件差 5 以内也算 MINOR（避免大库存 SKU 全部 MAJOR）"),
    )

    if st.button(t("💾 保存对账阈值"), type="primary", key="save_recon"):
        save_recon_params({
            "minor_abs_threshold": int(_rabs),
            "minor_pct_threshold": float(_rpct),
        })
        st.success(t("✅ 已保存。到「📦 库存构成分析 → 📊 NST vs JDL 对账」tab 刷新即生效。"))

    st.divider()
    st.caption(
        t("当前生效") + f"：绝对值 ≤ {_rp['minor_abs_threshold']} 件 · "
        + (t("百分比 ≤ {:.1f}%").format(_rp["minor_pct_threshold"])
           if _rp["minor_pct_threshold"] > 0 else t("百分比未启用"))
    )

# ============================================================
# tab_sync · 数据同步（NST / JDL 手动触发 · 各占 sub-tab）
# 数据源：nst.pull_schedule + jdl.pull_schedule
# 常驻 scheduler（nst_scheduler / jdl_scheduler）每分钟轮询，run_now=TRUE 立刻启动 daily_pull
# ============================================================
with tab_sync:
    st.caption(t("分别触发 NST / JDL 拉取，1 分钟内常驻 scheduler 启动 daily_pull。"))

    sync_nst, sync_jdl = st.tabs([t("🏬 NST（NetSuite）"), t("🚚 JDL（京东物流）")])

    # ── NST 手動更新（复用 page 27 同款 nst.pull_schedule 逻辑） ──
    with sync_nst:
        _CAT_LABEL = {"basic": "📦 商品マスタ", "inventory": "🏬 在庫", "sales": "💰 売上"}
        _FREQ_LABEL = {"daily": "毎日", "monthly": "毎月"}
        try:
            _nst_jobs = _df(
                "SELECT job_key, category, frequency, run_time, run_now, "
                "       last_run_at, last_status, description "
                "FROM nst.pull_schedule WHERE enabled ORDER BY job_key"
            )
        except Exception as e:
            st.error(t("⚠️ nst.pull_schedule 读取失败") + f"\n\n{e}")
            _nst_jobs = pd.DataFrame()

        if _nst_jobs.empty:
            st.info(t("有效なジョブがありません"))
        else:
            for _, r in _nst_jobs.iterrows():
                jk = str(r["job_key"])
                cat = _CAT_LABEL.get(r["category"], r["category"])
                freq = _FREQ_LABEL.get(r["frequency"], r["frequency"])
                n1, n2, n3, n4 = st.columns([2.5, 2, 2, 1.5])
                n1.markdown(f"**{cat}**  ·  {freq} {str(r['run_time'])[:5]} 自動")
                if r.get("description"):
                    n1.caption("📋 " + str(r["description"]))
                n2.caption(t("上次同步") + ": " +
                           (str(r["last_run_at"])[:16] if pd.notna(r["last_run_at"]) else "-"))
                n3.caption(t("上次状态") + ": " +
                           (str(r["last_status"]) if pd.notna(r["last_status"]) else "-"))
                if r["run_now"]:
                    n4.info(t("⏳ 实行待ち…"))
                else:
                    if n4.button(t("▶️ 今すぐ取得"), key=f"sys_nst_run_{jk}", type="primary"):
                        try:
                            conn.execute(
                                "UPDATE nst.pull_schedule SET run_now=TRUE, updated_at=now() "
                                "WHERE job_key=%(j)s", {"j": jk})
                            conn.commit()
                            st.success(t("{j} を予約しました（1分以内に実行）").format(j=jk))
                            st.rerun()
                        except Exception as e:
                            conn.rollback()
                            st.error(str(e))

        st.divider()
        with st.expander(t("⚙️ 编辑自动执行时间 / 启用状态"), expanded=False):
            try:
                _nst_all = _df(
                    "SELECT job_key, category, frequency, enabled, run_time, run_day "
                    "FROM nst.pull_schedule ORDER BY job_key"
                )
            except Exception as e:
                st.error(str(e))
                _nst_all = pd.DataFrame()
            if not _nst_all.empty:
                with st.form("nst_sched_form"):
                    _new = {}
                    for _, r in _nst_all.iterrows():
                        jk = str(r["job_key"])
                        cat = _CAT_LABEL.get(r["category"], r["category"])
                        freq = _FREQ_LABEL.get(r["frequency"], r["frequency"])
                        st.markdown(f"**{cat}**  ·  `{jk}`  ·  {freq}")
                        c1, c2, c3 = st.columns([1.2, 2, 2])
                        _en = c1.checkbox(t("启用"), value=bool(r["enabled"]),
                                          key=f"sys_en_{jk}")
                        _rt = c2.text_input(t("起动时刻 HH:MM"),
                                            value=str(r["run_time"])[:5],
                                            key=f"sys_rt_{jk}")
                        _rd = None
                        if r["frequency"] == "monthly":
                            _rd = c3.number_input(
                                t("执行日(1-28)"), 1, 28,
                                value=int(r["run_day"] or 1),
                                key=f"sys_rd_{jk}",
                            )
                        else:
                            c3.caption("—")
                        _new[jk] = (_en, _rt, _rd)
                    if st.form_submit_button(t("💾 保存定时设定"), type="primary"):
                        ok = 0
                        for jk, (en, rt, rd) in _new.items():
                            try:
                                conn.execute(
                                    "UPDATE nst.pull_schedule SET enabled=%(e)s, "
                                    "run_time=%(t)s, run_day=%(d)s, updated_at=now() "
                                    "WHERE job_key=%(j)s",
                                    {"e": en, "t": rt.strip(), "d": rd, "j": jk},
                                )
                                ok += 1
                            except Exception as e:
                                conn.rollback()
                                st.error(f"{jk}: {e}")
                        conn.commit()
                        st.success(t("保存成功（{n} 件）").format(n=ok))
                        st.rerun()

    # ── JDL 立即同步（jdl.pull_schedule） ─────────────────────
    with sync_jdl:
        try:
            _jdl_sched = _df(
                "SELECT job_key, run_now, last_run_at, last_status, frequency, run_time, description "
                "FROM jdl.pull_schedule ORDER BY job_key"
            )
        except Exception as e:
            st.error(t("⚠️ jdl.pull_schedule 读取失败") + f"\n\n{e}")
            _jdl_sched = pd.DataFrame()

        if _jdl_sched.empty:
            st.info(t("jdl.pull_schedule 未登录 job"))
        else:
            for _, r in _jdl_sched.iterrows():
                jk = str(r["job_key"])
                j1, j2, j3, j4 = st.columns([2.5, 2, 2, 1.5])
                j1.markdown(f"**🚚 {jk}**  ·  {r['frequency']} {str(r['run_time'])[:5]} 自動")
                if r.get("description"):
                    j1.caption("📋 " + str(r["description"]))
                j2.caption(t("上次同步") + ": " +
                           (str(r["last_run_at"])[:16] if pd.notna(r["last_run_at"]) else "-"))
                j3.caption(t("上次状态") + ": " +
                           (str(r["last_status"]) if pd.notna(r["last_status"]) else "-"))
                if r["run_now"]:
                    j4.info(t("⏳ 同步中…"))
                else:
                    if j4.button(t("▶️ 立即同步"), key=f"sys_jdl_run_{jk}", type="primary"):
                        try:
                            conn.execute(
                                "UPDATE jdl.pull_schedule SET run_now=TRUE, updated_at=now() "
                                "WHERE job_key=%(j)s", {"j": jk})
                            conn.commit()
                            st.success(t("已触发 · 1 分钟内启动 daily_pull"))
                            st.rerun()
                        except Exception as e:
                            conn.rollback()
                            st.error(str(e))

        st.divider()
        with st.expander(t("⚙️ 编辑自动执行时间 / 启用状态"), expanded=False):
            try:
                _jdl_all = _df(
                    "SELECT job_key, frequency, enabled, run_time "
                    "FROM jdl.pull_schedule ORDER BY job_key"
                )
            except Exception as e:
                st.error(str(e))
                _jdl_all = pd.DataFrame()
            if not _jdl_all.empty:
                with st.form("jdl_sched_form"):
                    _new_jdl = {}
                    for _, r in _jdl_all.iterrows():
                        jk = str(r["job_key"])
                        st.markdown(f"**🚚 `{jk}`**  ·  {r['frequency']}")
                        c1, c2 = st.columns([1.2, 2])
                        _en = c1.checkbox(t("启用"), value=bool(r["enabled"]),
                                          key=f"sys_jen_{jk}")
                        _rt = c2.text_input(t("起动时刻 HH:MM"),
                                            value=str(r["run_time"])[:5],
                                            key=f"sys_jrt_{jk}")
                        _new_jdl[jk] = (_en, _rt)
                    if st.form_submit_button(t("💾 保存定时设定"), type="primary",
                                              use_container_width=False):
                        ok = 0
                        for jk, (en, rt) in _new_jdl.items():
                            try:
                                conn.execute(
                                    "UPDATE jdl.pull_schedule SET enabled=%(e)s, "
                                    "run_time=%(t)s, updated_at=now() "
                                    "WHERE job_key=%(j)s",
                                    {"e": en, "t": rt.strip(), "j": jk},
                                )
                                ok += 1
                            except Exception as e:
                                conn.rollback()
                                st.error(f"{jk}: {e}")
                        conn.commit()
                        st.success(t("保存成功（{n} 件）").format(n=ok))
                        st.rerun()

# ============================================================
# tab4 · 货架用途指定（弁天棚号 → 输出中国 / 返品 / 不良品 标签）
# 数据源：nst.inventory_bin_snapshot 候选棚 + nst.bin_category 持久化标签
# ============================================================
with tab4:
    st.caption(t(
        "行=弁天棚番号·列=输出中国/返品/不良品。勾选保存后影响所有用途分类视图。"
        "未勾任何 → 通常输出。优先级: 返品 > 不良品 > 输出中国 > 通常输出。"
    ))

    try:
        _bin_date_df = _df(
            "SELECT max(snapshot_date) AS d FROM nst.inventory_bin_snapshot"
        )
        _bin_date = _bin_date_df["d"].iloc[0] if not _bin_date_df.empty else None
    except Exception as e:
        st.error(t("⚠️ 弁天 bin 快照表读取失败") + f"\n\n{e}")
        _bin_date = None

    try:
        cand_bins = _df(
            "SELECT DISTINCT ibs.bin_number, "
            "       COALESCE(bc.is_cb, FALSE) AS is_cb, "
            "       COALESCE(bc.is_return, FALSE) AS is_return, "
            "       COALESCE(bc.is_defect, FALSE) AS is_defect, "
            "       bc.note AS note "
            "FROM nst.inventory_bin_snapshot ibs "
            "LEFT JOIN nst.bin_category bc ON bc.bin_number = ibs.bin_number "
            "WHERE ibs.snapshot_date = %(d)s AND ibs.bin_number IS NOT NULL "
            "ORDER BY ibs.bin_number",
            {"d": _bin_date},
        ) if _bin_date is not None else pd.DataFrame()
    except Exception as e:
        st.error(t("⚠️ 候选棚号读取失败") + f"\n\n{e}")
        cand_bins = pd.DataFrame()

    if cand_bins.empty:
        st.info(t("暂无候选棚号（弁天 bin 快照为空？）"))
    else:
        cand_bins["is_cb"] = cand_bins["is_cb"].astype(bool)
        cand_bins["is_return"] = cand_bins["is_return"].astype(bool)
        cand_bins["is_defect"] = cand_bins["is_defect"].astype(bool)
        n_total = len(cand_bins)
        n_cb = int(cand_bins["is_cb"].sum())
        n_ret = int(cand_bins["is_return"].sum())
        n_def = int(cand_bins["is_defect"].sum())
        st.write(t("候选 {n} 棚 · 已指定 输出中国 {c} · 返品 {r} · 不良品 {d}")
                 .format(n=n_total, c=n_cb, r=n_ret, d=n_def))

        edited = st.data_editor(
            cand_bins, hide_index=True, use_container_width=True, height=560,
            column_config={
                "bin_number": st.column_config.TextColumn(t("棚番号"), disabled=True),
                "is_cb": st.column_config.CheckboxColumn(t("输出中国"), default=False),
                "is_return": st.column_config.CheckboxColumn(t("返品"), default=False),
                "is_defect": st.column_config.CheckboxColumn(t("不良品"), default=False),
                "note": st.column_config.TextColumn(t("备注"), required=False),
            },
            key="bin_cat_editor",
        )

        if st.button(t("💾 保存货架用途"), type="primary"):
            kept = 0
            removed = 0
            for _, r in edited.iterrows():
                bn = str(r["bin_number"])
                any_flag = bool(r["is_cb"]) or bool(r["is_return"]) or bool(r["is_defect"])
                note_v = str(r["note"]).strip() if pd.notna(r["note"]) else None
                if any_flag or note_v:
                    conn.execute(
                        "INSERT INTO nst.bin_category "
                        "(bin_number, is_cb, is_return, is_defect, note, updated_at) "
                        "VALUES (%(b)s, %(c)s, %(r)s, %(d)s, %(n)s, NOW()) "
                        "ON CONFLICT (bin_number) DO UPDATE SET "
                        "is_cb=EXCLUDED.is_cb, is_return=EXCLUDED.is_return, "
                        "is_defect=EXCLUDED.is_defect, note=EXCLUDED.note, "
                        "updated_at=NOW()",
                        {"b": bn, "c": bool(r["is_cb"]), "r": bool(r["is_return"]),
                         "d": bool(r["is_defect"]), "n": note_v},
                    )
                    kept += 1
                else:
                    res = conn.execute(
                        "DELETE FROM nst.bin_category WHERE bin_number = %(b)s",
                        {"b": bn},
                    )
                    if getattr(res, "rowcount", 0):
                        removed += 1
            conn.commit()
            st.success(t("✅ 已保存 · 有用途指定 {k} 棚 · 清除 {r} 棚").format(k=kept, r=removed))
            st.rerun()

    with st.expander(t("➕ 手动加棚号（候选列表里没有的，比如新建棚）")):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        new_bin = c1.text_input(t("棚番号"), key="new_bin")
        new_cb = c2.checkbox(t("输出中国"), value=False, key="new_cb")
        new_ret = c3.checkbox(t("返品"), value=False, key="new_ret")
        new_def = c4.checkbox(t("不良品"), value=False, key="new_def")
        if st.button(t("加入指定"), key="add_bin_cat"):
            bn = new_bin.strip()
            if bn:
                conn.execute(
                    "INSERT INTO nst.bin_category "
                    "(bin_number, is_cb, is_return, is_defect, updated_at) "
                    "VALUES (%(b)s, %(c)s, %(r)s, %(d)s, NOW()) "
                    "ON CONFLICT (bin_number) DO UPDATE SET "
                    "is_cb=EXCLUDED.is_cb, is_return=EXCLUDED.is_return, "
                    "is_defect=EXCLUDED.is_defect, updated_at=NOW()",
                    {"b": bn, "c": bool(new_cb), "r": bool(new_ret), "d": bool(new_def)},
                )
                conn.commit()
                st.success(t("✅ 已加入：{b}").format(b=bn))
                st.rerun()
            else:
                st.warning(t("请填棚番号"))

# ============================================================
# tab5 · 输出供应商名单（PO 数据无字段自动区分输出/CB → 人工白名单）
# 数据源：nst.purchase_order_line 候选 + nst.po_export_vendor 持久化
# ============================================================
with tab5:
    st.caption(t(
        "PO 采购数据无字段可自动区分 输出/CB（部门·子公司·仓库都试过不行）→ 人工白名单。"
        "下表列出「采购过输出主档商品(4/9)」的供货商，勾选「是否输出」剔除 CB/国内，保存后只有勾选的进白名单。"
    ))

    try:
        cand = _df(
            "SELECT DISTINCT pol.vendor_id, pol.vendor_name, "
            "       (ev.vendor_id IS NOT NULL) AS is_export, "
            "       COALESCE(ev.is_prepay, FALSE) AS is_prepay "
            "FROM nst.purchase_order_line pol "
            "JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
            "LEFT JOIN nst.po_export_vendor ev ON ev.vendor_id = pol.vendor_id "
            "WHERE pol.vendor_id IS NOT NULL "
            "ORDER BY pol.vendor_name"
        )
    except Exception as e:
        st.error(t("⚠️ 候选供货商读取失败（schema 未部署？）") + f"\n\n{e}")
        cand = pd.DataFrame()

    if cand.empty:
        st.info(t("暂无候选供货商（PO 数据空？）"))
    else:
        cand["is_export"] = cand["is_export"].astype(bool)
        cand["is_prepay"] = cand["is_prepay"].astype(bool)
        n_wl = int(cand["is_export"].sum())
        n_pp = int((cand["is_export"] & cand["is_prepay"]).sum())
        st.write(t("候选 {n} 家 · 当前白名单 {w} 家 · 其中预付款 {p} 家")
                 .format(n=len(cand), w=n_wl, p=n_pp))

        edited_v = st.data_editor(
            cand, hide_index=True, use_container_width=True, height=520,
            column_config={
                "vendor_id": st.column_config.TextColumn(t("供货商ID"), disabled=True),
                "vendor_name": st.column_config.TextColumn(t("仕入先"), disabled=True),
                "is_export": st.column_config.CheckboxColumn(t("是否输出"), default=False),
                "is_prepay": st.column_config.CheckboxColumn(
                    t("预付款"), default=False,
                    help=t("勾选则该供货商所有 PO 视为预付款（采购分析页单独列出）"),
                ),
            },
            key="wl_editor",
        )

        if st.button(t("💾 保存白名单"), type="primary"):
            kept = 0
            for _, r in edited_v.iterrows():
                vid = str(r["vendor_id"])
                if bool(r["is_export"]):
                    conn.execute(
                        "INSERT INTO nst.po_export_vendor "
                        "(vendor_id, vendor_name, is_prepay, source, updated_at) "
                        "VALUES (%(vid)s, %(vn)s, %(pp)s, 'sku_confirm', NOW()) "
                        "ON CONFLICT (vendor_id) DO UPDATE SET "
                        "vendor_name = EXCLUDED.vendor_name, "
                        "is_prepay = EXCLUDED.is_prepay, "
                        "updated_at = NOW()",
                        {"vid": vid, "vn": r["vendor_name"], "pp": bool(r["is_prepay"])},
                    )
                    kept += 1
                else:
                    conn.execute(
                        "DELETE FROM nst.po_export_vendor WHERE vendor_id = %(vid)s",
                        {"vid": vid})
            conn.commit()
            st.success(t("✅ 已保存 · 白名单 {n} 家").format(n=kept))
            st.rerun()

    with st.expander(t("➕ 手动加新供货商（候选列表里没有的）")):
        c1, c2, c3 = st.columns([2, 2, 1])
        new_vid = c1.text_input(t("供货商ID（NetSuite vendor internal id）"), key="new_vid")
        new_vname = c2.text_input(t("仕入先名"), key="new_vname")
        new_vpp = c3.checkbox(t("预付款"), value=False, key="new_vpp")
        if st.button(t("加入白名单"), key="add_manual_vendor"):
            if new_vid.strip():
                conn.execute(
                    "INSERT INTO nst.po_export_vendor "
                    "(vendor_id, vendor_name, is_prepay, source, updated_at) "
                    "VALUES (%(vid)s, %(vn)s, %(pp)s, 'manual', NOW()) "
                    "ON CONFLICT (vendor_id) DO UPDATE SET "
                    "vendor_name = EXCLUDED.vendor_name, "
                    "is_prepay = EXCLUDED.is_prepay, "
                    "updated_at = NOW()",
                    {"vid": new_vid.strip(), "vn": new_vname.strip() or None,
                     "pp": bool(new_vpp)},
                )
                conn.commit()
                st.success(t("✅ 已加入：{n}").format(n=new_vname or new_vid))
                st.rerun()
            else:
                st.warning(t("请填供货商ID"))
