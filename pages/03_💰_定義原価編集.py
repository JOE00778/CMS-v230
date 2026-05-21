"""模块 #1 定義原価編集（原「成本同步」· 2026-05-05 改名）· Streamlit 页面（v2 — 基于 inventory_snapshot 表）。

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

from data_warehouse.exports.cost_update import CostUpdateExporter
from modules.cost_sync.rules import (
    THRESHOLD_PCT,
    THRESHOLD_YEN,
    decide_action,
)
from shared.db import OUTPUTS_DIR, get_connection

st.set_page_config(page_title=t("定義原価編集"), page_icon="💰", layout="wide")
from shared.auth import require_admin
require_admin()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("💰 定義原価編集"))
st.caption(
    f"NetSuite Standard Cost（定義原価）統一編集口 · "
    f"自動判定阈值 |Δ|≥{THRESHOLD_YEN:.0f}¥ 或 |Δ%|≥{THRESHOLD_PCT:.0%} · "
    f"新值 = ⌈avg_cost⌉ · avg_cost = アイテム.xls H 列「平均原価」 (直接拉取)"
)
st.info(t(
    "📌 当前判断标准品 (纳入更新流程的 SKU) 范围: "
    "inventory_snapshot 中 ingest 已过滤为 JD-物流-千葉 + 弁天倉庫,"
    "默认两仓库都看,可在下方「場所」selector 单独选 JD 或 弁天。"
    "「单仓判断 / 默认仅 JD」改造后置 (跟 page 06 双仓判定一并)。"
))

# ============================================================
# 0. 检查数据
# ============================================================
inv_count = conn.execute("SELECT COUNT(*) AS c FROM inventory_snapshot").fetchone()["c"]
if inv_count == 0:
    st.warning(
        t("⚠️ `inventory_snapshot` 表为空。请先到「⚙️ 数据导入与设置」上传 "
        "`FB全倉庫通常在庫数残数検索結果.xls`。")
    )
    st.stop()

# ============================================================
# Session state
# ============================================================
if "cs_step" not in st.session_state:
    st.session_state.cs_step = 1
if "cs_decisions" not in st.session_state:
    st.session_state.cs_decisions = None
if "cs_output_path" not in st.session_state:
    st.session_state.cs_output_path = None


def _reset() -> None:
    st.session_state.cs_step = 1
    st.session_state.cs_decisions = None
    st.session_state.cs_output_path = None


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

    # 快照选项
    snapshots = conn.execute(
        "SELECT DISTINCT snapshot_at FROM inventory_snapshot ORDER BY snapshot_at DESC"
    ).fetchall()
    snapshot_choices = [r["snapshot_at"] for r in snapshots]
    sel_snapshot = st.selectbox(
        t("在库数据快照（默认最新）"), snapshot_choices, index=0
    )

    # 过滤条件（部门固定为含「輸出」的，不在 UI 暴露；仓库限定 JD + 弁天，ingest 时已过滤）
    DEPT_KEYWORD = "輸出"
    LOC_BOTH = "JD + 弁天（默认）"
    HANDLE_PRESET_ALL = "全部"

    # 自动锁定 部門：含「輸出」的全部
    sel_dept = [
        r["department"] for r in conn.execute(
            "SELECT DISTINCT department FROM inventory_snapshot WHERE snapshot_at=?",
            (sel_snapshot,)
        ).fetchall()
        if r["department"] and DEPT_KEYWORD in r["department"]
    ]

    # 数据源
    loc_all = [r["location"] for r in conn.execute(
        "SELECT DISTINCT location FROM inventory_snapshot WHERE snapshot_at=? ORDER BY location",
        (sel_snapshot,)
    ).fetchall() if r["location"]]
    handle_all = [r["handling_status"] for r in conn.execute(
        "SELECT DISTINCT handling_status FROM inventory_snapshot WHERE snapshot_at=? ORDER BY handling_status",
        (sel_snapshot,)
    ).fetchall() if r["handling_status"]]

    loc_choices = [LOC_BOTH] + loc_all
    handle_choices = [HANDLE_PRESET_ALL] + handle_all

    c1, c2 = st.columns(2)
    with c1:
        loc_pick = st.selectbox(t("場所（仓库）"), loc_choices, index=0)
    with c2:
        handle_pick = st.selectbox(t("取扱区分"), handle_choices, index=0)

    sel_locs = loc_all if loc_pick == LOC_BOTH else [loc_pick]
    sel_handle = handle_all if handle_pick == HANDLE_PRESET_ALL else [handle_pick]

    ignore_cost1 = st.checkbox(
        t("忽略当前定义原价为 1 的 SKU"), value=True,
        help=t("std_cost = 1 多为占位 / 异常值，默认排除"),
    )

    st.caption(
        f"📌 已选场所：{', '.join(sel_locs)} ｜ "
        f"取扱区分：{', '.join(sel_handle)} ｜ "
        f"部門（自动锁定）：{', '.join(sel_dept) or '（无）'}"
    )

    # 预览过滤后的 SKU 数（前缀 s. 因后续会 LEFT JOIN item_master_netsuite，department 等字段两表都有）
    where = ["s.snapshot_at = :snap"]
    params: dict = {"snap": sel_snapshot}
    if sel_locs:
        placeholders = ",".join(f":loc{i}" for i in range(len(sel_locs)))
        where.append(f"s.location IN ({placeholders})")
        params.update({f"loc{i}": v for i, v in enumerate(sel_locs)})
    if sel_handle:
        placeholders = ",".join(f":h{i}" for i in range(len(sel_handle)))
        where.append(f"s.handling_status IN ({placeholders})")
        params.update({f"h{i}": v for i, v in enumerate(sel_handle)})
    if sel_dept:
        placeholders = ",".join(f":d{i}" for i in range(len(sel_dept)))
        where.append(f"s.department IN ({placeholders})")
        params.update({f"d{i}": v for i, v in enumerate(sel_dept)})
    if ignore_cost1:
        where.append("(s.std_cost IS DISTINCT FROM 1)")

    where_sql = " AND ".join(where)
    sku_count = conn.execute(
        f"SELECT COUNT(DISTINCT s.internal_id) AS c FROM inventory_snapshot s WHERE {where_sql}",
        params,
    ).fetchone()["c"]

    st.metric(t("过滤后唯一 SKU 数"), f"{sku_count:,}")

    if sku_count == 0:
        st.warning(t("当前过滤条件下没有 SKU。请调整。"))
        st.stop()

    if st.button(t("🚀 计算并预览"), type="primary"):
        # ========================================================
        # 数据源 (Boss 2026-05 决定):
        # - std_cost_old / handling_status / qty_on_hand ← inventory_snapshot
        #   (来自 輸出通常在庫数残数検索結果.xls)
        # - avg_cost ← nst_item_summary.avg_cost (H 列「平均原価」)
        #   (来自 アイテム.xls 8 列原表)
        # std_cost_new = ⌈avg_cost⌉ 向上取整
        # ========================================================
        agg_sql = f"""
            SELECT
                s.internal_id,
                MAX(s.item_code) AS item_code,
                MAX(s.upc) AS upc,
                MAX(s.display_name) AS display_name,
                MAX(s.handling_status) AS handling_status,
                MAX(s.std_cost) AS std_cost,
                SUM(s.qty_on_hand) AS total_qty,
                MAX(im.maker) AS maker
            FROM inventory_snapshot s
            LEFT JOIN item_master_netsuite im ON im.internal_id = s.internal_id
            WHERE {where_sql}
            GROUP BY s.internal_id
        """
        rows = conn.execute(agg_sql, params).fetchall()

        # 从 nst_item_summary 直接拉 H 列 avg_cost (Boss 决定用此表)
        item_codes = [r["item_code"] for r in rows if r["item_code"]]
        avg_cost_by_code: dict[str, float] = {}
        if item_codes:
            placeholders = ",".join(f":c{i}" for i in range(len(item_codes)))
            ic_params = {f"c{i}": v for i, v in enumerate(item_codes)}
            item_rows = conn.execute(
                f"""
                SELECT item_code, avg_cost
                FROM nst_item_summary
                WHERE item_code IN ({placeholders})
                  AND avg_cost IS NOT NULL
                  AND avg_cost > 0
                """,
                ic_params,
            ).fetchall()
            for ir in item_rows:
                avg_cost_by_code[ir["item_code"]] = float(ir["avg_cost"])

        # ── Phase 3.3 · v2 fallback：avg_cost / maker 从 item_v2 兜底 ──
        # nst_item_summary 经常空 → 用 item_v2 (PK=jan) 作权威源补缺
        v2_by_jan: dict[str, dict] = {}
        try:
            jans = [r["upc"] for r in rows if r["upc"]]
            if jans:
                ph = ",".join(f":j{i}" for i in range(len(jans)))
                jp = {f"j{i}": v for i, v in enumerate(jans)}
                v2_rows = conn.execute(
                    f"SELECT jan, avg_cost, maker FROM item_v2 WHERE jan IN ({ph})",
                    jp,
                ).fetchall()
                v2_by_jan = {r["jan"]: dict(r) for r in v2_rows}
        except Exception:
            v2_by_jan = {}   # v2 表还没建则跳过

        # 跑业务规则
        decisions = []
        for r in rows:
            avg_cost = avg_cost_by_code.get(r["item_code"])  # 来自 nst_item_summary
            v2_row = v2_by_jan.get(r["upc"]) if r["upc"] else None
            # v2 fallback：avg_cost
            if avg_cost is None and v2_row and v2_row.get("avg_cost"):
                avg_cost = float(v2_row["avg_cost"])
            # v2 fallback：maker
            maker = r["maker"]
            if not maker and v2_row and v2_row.get("maker"):
                maker = v2_row["maker"]

            row = {
                "internal_id": r["internal_id"],
                "item_code": r["item_code"],
                "display_name": r["display_name"],
                "avg_cost": avg_cost,
                "std_cost_old": r["std_cost"],
            }
            master = {
                "handling_status": r["handling_status"],
                "display_name": r["display_name"],
            }
            d = decide_action(row, master)
            d["total_qty"] = r["total_qty"]
            d["maker"] = maker
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

    total = len(df_all)
    n_update = (df_all["action"] == "UPDATE").sum() if total else 0
    n_skip = total - n_update
    n_red = (df_all.get("severity") == "RED").sum() if total else 0
    n_yellow = (df_all.get("severity") == "YELLOW").sum() if total else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("候选 SKU 总数"), f"{total:,}")
    c2.metric(t("✅ 触发更新"), f"{n_update:,}")
    c3.metric(t("⏭️ 跳过"), f"{n_skip:,}")
    c4.metric(t("⚠️ 异常 R+Y"), f"{n_red + n_yellow:,}")

    if n_skip > 0:
        skip_breakdown = (
            df_all[df_all["action"] != "UPDATE"]["action"]
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

    tab_u, tab_s, tab_a = st.tabs([
        t(f"✅ 更新清单 ({n_update})"),
        t(f"⏭️ 跳过清单 ({n_skip})"),
        t(f"⚠️ 异常告警 ({n_red + n_yellow})"),
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
        df_u = df_all[df_all["action"] == "UPDATE"].copy()
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
        df_s = df_all[df_all["action"] != "UPDATE"].copy()
        if df_s.empty:
            st.info(t("没有任何 SKU 被跳过。"))
        else:
            df_show = df_s[["internal_id", "item_code", "display_name", "maker", "total_qty",
                            "avg_cost", "std_cost_old", "action", "skip_reason"]].copy()
            df_show = df_show.rename(columns=COL_RENAME)
            st.dataframe(df_show, use_container_width=True, hide_index=True)

    with tab_a:
        df_a = df_all[df_all.get("severity").isin(["RED", "YELLOW"])].copy()
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

    st.divider()
    btn_back, btn_next = st.columns(2)
    with btn_back:
        if st.button(t("← 重新选择数据"), use_container_width=True):
            _reset()
            st.rerun()
    with btn_next:
        if n_update == 0:
            st.button(
                t("确认并生成 CSV →"), type="primary", disabled=True, use_container_width=True
            )
            st.caption(t("没有需要更新的 SKU"))
        else:
            if st.button(
                t(f"确认并生成 CSV ({n_update} 行) →"), type="primary", use_container_width=True
            ):
                rows = CostUpdateExporter.build_rows(decisions)
                file_path, _ = CostUpdateExporter().export(
                    rows, OUTPUTS_DIR, conn,
                    notes=f"snapshot={st.session_state.cs_decisions[0].get('snapshot_at', '?') if decisions else '?'}",
                )

                # 写入 std_cost_history（驱动 page 03b 波动图）
                from datetime import datetime
                changed_at = datetime.utcnow().isoformat()
                hist_rows = []
                for d in decisions:
                    if d.get("action") != "UPDATE":
                        continue
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

                st.session_state.cs_output_path = file_path
                st.session_state.cs_step = 3
                st.rerun()


# ============================================================
# 步骤 3：下载
# ============================================================
elif step == 3:
    st.subheader(t("✅ 步骤 3 / 3：完成"))

    file_path = st.session_state.cs_output_path
    if file_path and file_path.exists():
        with file_path.open("rb") as f:
            data = f.read()
        st.success(t(f"已生成更新 CSV：`{file_path.name}`"))
        st.download_button(
            t("📥 下载更新 CSV"),
            data=data,
            file_name=file_path.name,
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
            4. **Field Mapping**：CSV `Internal ID` → NetSuite `Internal ID` · CSV `Standard Cost` → NetSuite `Standard Cost`
            5. 第一次配完保存为 `Cost_Sync_Update` 映射，下次秒上传
            """
        )
    else:
        st.error(t("⚠️ 输出文件丢失，重来一次"))

    if st.button(t("🔄 再做一次"), type="primary"):
        _reset()
        st.rerun()
