"""模块 #1 定義原価編集（原「成本同步」· 2026-05-05 改名）· Streamlit 页面（v3 — 2026-05-21 数据源迁移至 NST API）。

业务定位：NetSuite Standard Cost = 定義原価 字段的统一管理入口。
本质是修改 NetSuite Standard Cost（= 定義原価）。两种触发场景共用同一流程：
  ① 数据驱动：上传 NetSuite 在库 .xls → 系统检测 avg_cost 偏差 → 提议更新 std_cost
  ② Boss 决策：Boss 跟供应商谈了新价 / 政策调整 → 在结果表内手动覆盖 std_cost_new → 生成 CSV
两种场景输出**同一份 cost_update.csv**，由 Boss 上传 NetSuite Item Import。

数据来源：用户在「⚙️ 数据导入与设置」页上传过的 NetSuite 在库数据 .xls
        → 入到 inventory_snapshot 表（含 std_cost + avg_cost）

流程：
  1. 选择快照（默认最新）+ 过滤条件（場所 / 取扱区分 / 担当者 / 部門）
  2. 按 internal_id 聚合：
     - std_cost / avg_cost 取首个非空（同 SKU 在不同 location 应该一致）
     - qty_on_hand 求和（用于显示）
  3. 应用业务规则（5 类 SKIP + 阈值 + ceil）
  4. 预览三 Tab（更新 / 跳过 / 异常）
  5. 确认 → 生成 NetSuite CSV Import 文件
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from shared.i18n import t, lang_selector

from data_warehouse.templates.nst_item_master import (
    COL_COST,
    ID_LABEL,
    build_nst_master_csv,
    dated_filename,
)
from modules.cost_sync.rules import (
    THRESHOLD_PCT,
    THRESHOLD_YEN,
    decide_action,
)
from shared.db import get_connection

st.set_page_config(page_title=t("定義原価編集"), page_icon="💰", layout="wide")
from shared.auth import require_admin
require_admin()
from shared.theme import inject_theme
inject_theme()
# 本ページのみ：データ表が画面幅いっぱいに広がるよう全局 1400px 上限を解除
st.markdown(
    "<style>[data-testid='stMainBlockContainer'],.main .block-container"
    "{max-width:100%!important;}</style>",
    unsafe_allow_html=True,
)
lang_selector()
conn = get_connection()

st.title(t("💰 定義原価編集"))
st.caption(
    f"NetSuite Standard Cost（定義原価）統一編集口 · "
    f"自動判定阈值 |Δ|≥{THRESHOLD_YEN:.0f}¥ 或 |Δ%|≥{THRESHOLD_PCT:.0%} · "
    f"新值 = ⌈平均原価⌉ · 数据源 = NST 主档 nst.item_master_raw"
)

# ============================================================
# 0. 检查数据
# ============================================================
im_count = conn.execute("SELECT COUNT(*) AS c FROM nst.item_master_raw").fetchone()["c"]
if im_count == 0:
    st.warning(
        t("⚠️ `nst.item_master_raw` 为空。请先到「📥 NST 取得数据」执行 items 任务。")
    )
    st.stop()

# ============================================================
# Session state
# ============================================================
if "cs_step" not in st.session_state:
    st.session_state.cs_step = 1
if "cs_decisions" not in st.session_state:
    st.session_state.cs_decisions = None
if "cs_csv_bytes" not in st.session_state:
    st.session_state.cs_csv_bytes = None


def _reset() -> None:
    st.session_state.cs_step = 1
    st.session_state.cs_decisions = None
    st.session_state.cs_csv_bytes = None


# ============================================================
# 进度条
# ============================================================
step = st.session_state.cs_step
prog_cols = st.columns(3)
for i, label in enumerate([t("1️⃣ 选择数据 + 过滤"), t("2️⃣ 预览结果"), t("3️⃣ 下载输出")], 1):
    with prog_cols[i - 1]:
        if i == step:
            st.info(f"**{label}**")
        elif i < step:
            st.success(f"{label} ✓")
        else:
            st.caption(label)

st.divider()


# ============================================================
# 步骤 1：选择数据范围
# ============================================================
if step == 1:
    st.subheader(t("📋 步骤 1 / 3：选择数据范围"))

    # 快照选项（NST 库存スナップショット）
    snapshots = conn.execute(
        "SELECT DISTINCT snapshot_date FROM nst.inventory_snapshot ORDER BY snapshot_date DESC"
    ).fetchall()
    if not snapshots:
        st.warning(t("⚠️ `nst.inventory_snapshot` 无库存快照。请先到「📥 NST 取得数据」执行 inventory 任务。"))
        st.stop()
    snapshot_choices = [r["snapshot_date"] for r in snapshots]
    sel_snapshot = st.selectbox(
        t("在库数据快照（默认最新）"), snapshot_choices, index=0
    )

    # 部門固定含「輸出」（NST 仅采輸出事業·自动锁定不在 UI 暴露）
    LOC_BOTH = "全仓库（默认）"
    HANDLE_PRESET_ALL = "全部"

    # 場所(warehouse) 候选 = 该快照下的仓库
    loc_all = [r["warehouse"] for r in conn.execute(
        "SELECT DISTINCT warehouse FROM nst.inventory_snapshot "
        "WHERE snapshot_date=? AND warehouse IS NOT NULL ORDER BY warehouse",
        (sel_snapshot,)
    ).fetchall() if r["warehouse"]]
    handle_all = [r["handling_cd"] for r in conn.execute(
        "SELECT DISTINCT handling_cd FROM nst.item_master_raw "
        "WHERE handling_cd IS NOT NULL ORDER BY handling_cd"
    ).fetchall() if r["handling_cd"]]

    loc_choices = [LOC_BOTH] + loc_all
    handle_choices = [HANDLE_PRESET_ALL] + handle_all

    c1, c2 = st.columns(2)
    with c1:
        loc_pick = st.selectbox(t("場所（仓库）"), loc_choices, index=0)
    with c2:
        handle_pick = st.selectbox(t("取扱区分"), handle_choices, index=0)

    sel_locs = loc_all if loc_pick == LOC_BOTH else [loc_pick]
    sel_handle = handle_all if handle_pick == HANDLE_PRESET_ALL else [handle_pick]

    st.caption(
        f"📌 已选场所：{', '.join(sel_locs) or '（全部）'} ｜ "
        f"取扱区分：{', '.join(sel_handle) or '（全部）'} ｜ "
        f"部門：輸出（自动锁定）"
    )

    # 过滤 where（im=nst.item_master_raw · inv=nst.inventory_snapshot）· 部門锁定輸出
    where = ["inv.snapshot_date = :snap", "im.department LIKE :dept"]
    params: dict = {"snap": sel_snapshot, "dept": "%輸出%"}
    if sel_locs:
        placeholders = ",".join(f":loc{i}" for i in range(len(sel_locs)))
        where.append(f"inv.warehouse IN ({placeholders})")
        params.update({f"loc{i}": v for i, v in enumerate(sel_locs)})
    if sel_handle:
        placeholders = ",".join(f":h{i}" for i in range(len(sel_handle)))
        where.append(f"im.handling_cd IN ({placeholders})")
        params.update({f"h{i}": v for i, v in enumerate(sel_handle)})

    where_sql = " AND ".join(where)
    sku_count = conn.execute(
        f"SELECT COUNT(DISTINCT im.internal_id) AS c "
        f"FROM nst.item_master_raw im "
        f"JOIN nst.inventory_snapshot inv ON inv.item_internal_id = im.internal_id "
        f"WHERE {where_sql}",
        params,
    ).fetchone()["c"]

    st.metric(t("过滤后唯一 SKU 数"), f"{sku_count:,}")

    if sku_count == 0:
        st.warning(t("当前过滤条件下没有 SKU。请调整。"))
        st.stop()

    if st.button(t("🚀 计算并预览"), type="primary"):
        # ========================================================
        # 数据源 (NST · 2026-05-21 迁移):
        # - std_cost_old ← nst.item_master_raw.cost_estimate (定義原価)
        # - avg_cost     ← nst.item_master_raw.average_cost  (平均原価)
        # - qty_on_hand  ← nst.inventory_snapshot.qty_on_hand (选定快照+仓库 合计)
        # std_cost_new = ⌈avg_cost⌉ 向上取整
        # ========================================================
        agg_sql = f"""
            SELECT
                im.internal_id,
                MAX(im.item_code) AS item_code,
                MAX(im.display_name) AS display_name,
                MAX(im.handling_cd) AS handling_status,
                MAX(im.cost_estimate) AS std_cost,
                MAX(im.average_cost) AS avg_cost,
                MAX(im.maker) AS maker,
                COALESCE(SUM(inv.qty_on_hand), 0) AS total_qty
            FROM nst.item_master_raw im
            JOIN nst.inventory_snapshot inv ON inv.item_internal_id = im.internal_id
            WHERE {where_sql}
            GROUP BY im.internal_id
        """
        rows = conn.execute(agg_sql, params).fetchall()

        # 跑业务规则（avg_cost / std_cost_old 直接来自 NST 主档）
        def _is_one(x):
            try:
                return x is not None and float(x) == 1
            except (TypeError, ValueError):
                return False

        decisions = []
        for r in rows:
            row = {
                "internal_id": r["internal_id"],
                "item_code": r["item_code"],
                "display_name": r["display_name"],
                "avg_cost": float(r["avg_cost"]) if r["avg_cost"] is not None else None,
                "std_cost_old": float(r["std_cost"]) if r["std_cost"] is not None else None,
            }
            master = {
                "handling_status": r["handling_status"],
                "display_name": r["display_name"],
            }
            d = decide_action(row, master)
            d["total_qty"] = r["total_qty"]
            d["maker"] = r["maker"]
            # 标记 当前定义原价 / 平均原価 / 新定义原价 任一 = 1（占位 / 异常值）
            d["is_price_one"] = (
                _is_one(d.get("std_cost_old"))
                or _is_one(d.get("avg_cost"))
                or _is_one(d.get("std_cost_new"))
            )
            decisions.append(d)

        st.session_state.cs_decisions = decisions
        st.session_state.cs_step = 2
        st.rerun()


# ============================================================
# 步骤 2：预览
# ============================================================
elif step == 2:
    st.subheader(t("🔍 步骤 2 / 3：预览结果"))

    decisions = st.session_state.cs_decisions or []
    df_all = pd.DataFrame(decisions)

    # is_price_one 兜底（旧 session 无此列）
    if "is_price_one" not in df_all.columns:
        df_all["is_price_one"] = False
    df_all["is_price_one"] = df_all["is_price_one"].fillna(False).astype(bool)

    # 两个开关：忽略(显示分流到专门 tab) + 更改(是否写入 CSV)
    sw1, sw2 = st.columns(2)
    with sw1:
        ignore_cost1 = st.checkbox(
            t("忽略 当前定义原价 / 平均原价 / 新定义原价 = 1 的 SKU"), value=True,
            help=t("这三个原价中任一 = 1 多为占位 / 异常值；开启后从更新 / 跳过 / 异常清单移到「原价=1」tab"),
        )
    with sw2:
        update_cost1 = st.checkbox(
            t("更改这些原价 = 1 的 SKU（纳入生成 CSV）"), value=False,
            help=t("默认不更改；勾选后「原价=1」tab 中触发更新的 SKU 也写入上传 CSV"),
        )

    # 分类：原价=1 单列 / 主流程
    df_one = df_all[df_all["is_price_one"]].copy()
    df_main = df_all[~df_all["is_price_one"]].copy() if ignore_cost1 else df_all.copy()

    total = len(df_main)
    n_update = (df_main["action"] == "UPDATE").sum() if total else 0
    n_skip = total - n_update
    n_red = (df_main.get("severity") == "RED").sum() if total else 0
    n_yellow = (df_main.get("severity") == "YELLOW").sum() if total else 0
    n_one = len(df_one)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(t("候选 SKU 总数"), f"{total:,}")
    c2.metric(t("✅ 触发更新"), f"{n_update:,}")
    c3.metric(t("⏭️ 跳过"), f"{n_skip:,}")
    c4.metric(t("⚠️ 异常 R+Y"), f"{n_red + n_yellow:,}")
    c5.metric(t("💢 原价=1"), f"{n_one:,}")

    if n_skip > 0:
        skip_breakdown = (
            df_main[df_main["action"] != "UPDATE"]["action"]
            .value_counts()
            .to_dict()
        )
        st.caption(t("跳过原因分布：") + " · ".join(f"{k}: {v}" for k, v in skip_breakdown.items()))

    # ============================================================
    # 预览全量 CSV 下载 (上传用 · 含全部 SKU 的判断结果)
    # ============================================================
    if total > 0:
        from datetime import datetime as _dt
        # 列名 → 中日 i18n
        _COL_RENAME_FULL = {
            "internal_id": t("内部 ID"),
            "item_code": t("商品代码"),
            "display_name": t("商品名"),
            "maker": t("品牌"),
            "total_qty": t("库存数量"),
            "handling_status": t("取扱区分"),
            "std_cost_old": t("当前定义原价"),
            "avg_cost": t("平均原価"),
            "std_cost_new": t("新定义原价"),
            "diff": t("差额"),
            "diff_pct": t("差额率"),
            "severity": t("严重度"),
            "action": t("处理"),
            "skip_reason": t("跳过原因"),
        }
        cols_full = [c for c in _COL_RENAME_FULL.keys() if c in df_all.columns]
        df_full = df_all[cols_full].copy()
        if "diff_pct" in df_full.columns:
            df_full["diff_pct"] = df_full["diff_pct"].apply(
                lambda x: f"{x:+.4f}" if pd.notna(x) else ""
            )
        df_full = df_full.rename(columns=_COL_RENAME_FULL)
        _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        st.download_button(
            t(f"📥 下载预览全量 CSV (上传用,{total} 行)"),
            data=df_full.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"cost_preview_{_ts}.csv",
            mime="text/csv",
            key="dl_preview_full",
            help=t("预览阶段全量数据 (含 UPDATE / SKIP / 异常),用于审阅或外部导入"),
        )

    st.divider()

    tab_u, tab_s, tab_a, tab_o = st.tabs([
        t(f"✅ 更新清单 ({n_update})"),
        t(f"⏭️ 跳过清单 ({n_skip})"),
        t(f"⚠️ 异常告警 ({n_red + n_yellow})"),
        t(f"💢 原价=1 ({n_one})"),
    ])

    # 列名 → 中文/日文 (走 t() 走 i18n)
    COL_RENAME = {
        "internal_id": t("内部 ID"),
        "item_code": t("商品代码"),
        "display_name": t("商品名"),
        "maker": t("品牌"),
        "total_qty": t("库存数量"),
        "std_cost_old": t("当前定义原价"),
        "std_cost_new": t("新定义原价"),
        "avg_cost": t("平均原価"),
        "diff": t("差额"),
        "diff_pct": t("差额率"),
        "severity": t("严重度"),
        "action": t("处理"),
        "skip_reason": t("跳过原因"),
    }

    with tab_u:
        df_u = df_main[df_main["action"] == "UPDATE"].copy()
        if df_u.empty:
            st.info(t("本次没有 SKU 触发更新。"))
        else:
            df_show = df_u[["internal_id", "item_code", "display_name", "maker", "total_qty",
                            "std_cost_old", "std_cost_new", "diff", "diff_pct", "severity"]].copy()
            df_show["diff_pct"] = df_show["diff_pct"].apply(
                lambda x: f"{x:+.2%}" if pd.notna(x) else ""
            )
            df_show = df_show.rename(columns=COL_RENAME)
            st.dataframe(df_show, use_container_width=True, hide_index=True)

    with tab_s:
        df_s = df_main[df_main["action"] != "UPDATE"].copy()
        if df_s.empty:
            st.info(t("没有任何 SKU 被跳过。"))
        else:
            df_show = df_s[["internal_id", "item_code", "display_name", "maker", "total_qty",
                            "avg_cost", "std_cost_old", "action", "skip_reason"]].copy()
            df_show = df_show.rename(columns=COL_RENAME)
            st.dataframe(df_show, use_container_width=True, hide_index=True)

    with tab_a:
        df_a = df_main[df_main.get("severity").isin(["RED", "YELLOW"])].copy()
        if df_a.empty:
            st.success(t("✅ 无异常告警。"))
        else:
            df_a = df_a.sort_values(
                by=["severity", "diff_pct"],
                key=lambda s: s.map({"RED": 0, "YELLOW": 1}) if s.name == "severity" else s.abs(),
                ascending=[True, False],
            )
            df_show = df_a[["severity", "internal_id", "item_code", "display_name", "maker",
                            "std_cost_old", "avg_cost", "std_cost_new",
                            "diff", "diff_pct", "action"]].copy()
            df_show["diff_pct"] = df_show["diff_pct"].apply(
                lambda x: f"{x:+.2%}" if pd.notna(x) else ""
            )
            df_show = df_show.rename(columns=COL_RENAME)
            st.dataframe(df_show, use_container_width=True, hide_index=True)

    with tab_o:
        if df_one.empty:
            st.success(t("✅ 没有原价 = 1 的 SKU。"))
        else:
            _ocols = ["internal_id", "item_code", "display_name", "maker", "total_qty",
                      "std_cost_old", "avg_cost", "std_cost_new", "action", "skip_reason"]
            _ocols = [c for c in _ocols if c in df_one.columns]
            df_show = df_one[_ocols].rename(columns=COL_RENAME)
            st.dataframe(df_show, use_container_width=True, hide_index=True)
            n_one_upd = int((df_one["action"] == "UPDATE").sum())
            if update_cost1:
                st.caption(t(f"✅ 已勾选「更改」：其中 {n_one_upd} 个触发更新的将写入上传 CSV"))
            else:
                st.caption(t(f"默认不更改：这 {n_one} 个 SKU 不写入 CSV（勾选上方「更改」开关可纳入）"))

    # 写入 CSV 的 SKU = UPDATE 且 (非原价=1 或 已勾选更改原价=1)
    csv_decisions = [
        d for d in decisions
        if d.get("action") == "UPDATE" and d.get("std_cost_new") is not None
        and (not d.get("is_price_one") or update_cost1)
    ]
    n_csv = len(csv_decisions)

    st.divider()
    btn_back, btn_next = st.columns(2)
    with btn_back:
        if st.button(t("← 重新选择数据"), use_container_width=True):
            _reset()
            st.rerun()
    with btn_next:
        if n_csv == 0:
            st.button(
                t("确认并生成 CSV →"), type="primary", disabled=True, use_container_width=True
            )
            st.caption(t("没有需要更新的 SKU"))
        else:
            if st.button(
                t(f"确认并生成 CSV ({n_csv} 行) →"), type="primary", use_container_width=True
            ):
                # NST 上传模板格式 CSV：第一列 Internal ID + 「商品原価」(= 定義原価)
                csv_rows = [
                    {ID_LABEL: d["internal_id"], COL_COST: int(d["std_cost_new"])}
                    for d in csv_decisions
                ]
                st.session_state.cs_csv_bytes = build_nst_master_csv(csv_rows, [COL_COST])

                # 写入 std_cost_history（驱动波动图）· 表缺失不阻断下载
                try:
                    from datetime import datetime
                    changed_at = datetime.utcnow().isoformat()
                    hist_rows = []
                    for d in csv_decisions:
                        old = d.get("std_cost_old")
                        new = d.get("std_cost_new")
                        diff = (new - old) if (old is not None and new is not None) else None
                        diff_pct = (diff / old) if (diff is not None and old) else None
                        src = "manual-override" if d.get("manual_override") else "avg-driven"
                        hist_rows.append((
                            d.get("internal_id"), d.get("item_code"), d.get("display_name"),
                            old, new, diff, diff_pct, changed_at, "BOSS", src,
                            f"snapshot={d.get('snapshot_at', '?')}",
                        ))
                    if hist_rows:
                        conn.executemany(
                            "INSERT INTO std_cost_history(internal_id,item_code,display_name,"
                            "std_cost_old,std_cost_new,diff,diff_pct,changed_at,changed_by,source,notes) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            hist_rows,
                        )
                        conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

                st.session_state.cs_step = 3
                st.rerun()


# ============================================================
# 步骤 3：下载
# ============================================================
elif step == 3:
    st.subheader(t("✅ 步骤 3 / 3：完成"))

    csv_bytes = st.session_state.cs_csv_bytes
    if csv_bytes:
        st.success(t("已生成 NST 上传模板 CSV（第一列 Internal ID · 「商品原価」列 = 定義原価）"))
        st.download_button(
            t("📥 下载更新 CSV"),
            data=csv_bytes,
            file_name=dated_filename(),
            mime="text/csv",
            type="primary",
            use_container_width=True,
        )
        st.divider()
        st.markdown(
            """
            ### 📋 上传到 NetSuite 的步骤

            1. NetSuite → **Setup → Import/Export → Import CSV Records**
            2. **Import Type**: `Items` · **Record Type**: `Inventory Item` · **Import**: `Update`
            3. 上传刚下载的 CSV
            4. **Field Mapping**：CSV `Internal ID` → NetSuite `Internal ID` · CSV `商品原価` → NetSuite `Standard Cost`（定義原価）
            5. 第一次配完保存映射，下次秒上传
            """
        )
    else:
        st.error(t("⚠️ 输出内容丢失，重来一次"))

    if st.button(t("🔄 再做一次"), type="primary"):
        _reset()
        st.rerun()
