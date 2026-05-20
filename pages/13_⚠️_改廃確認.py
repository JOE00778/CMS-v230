"""改廃確認 (Page 13) — T-318 N8N 化重写 v2.0

变更：handle_action() 不再直写 SQLite，而是 POST N8N webhook，
        N8N → cms-api /v1/kaihai/confirm → PG update + 联动停售 + 飞书 + audit。

紧急回退：env LEGACY_KAIHAI=true → 走老 SQLite 直写逻辑（旧 v1.x 行为，
                                   T-318 完工前如 N8N 故障可救场）。

env 依赖：
    N8N_WEBHOOK_KAIHAI   — N8N webhook URL（默认 http://n8n:5678/webhook/cms-kaihai-confirm）
    LEGACY_KAIHAI=true   — 走老路径（默认 false）
"""
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

from shared.i18n import t, lang_selector
from shared.i18n_columns import localize_df

st.set_page_config(page_title=t("改廃確認"), page_icon="⚠️", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()

# ============================================================================
# 配置
# ============================================================================

N8N_WEBHOOK_KAIHAI = os.environ.get(
    "N8N_WEBHOOK_KAIHAI",
    "http://n8n:5678/webhook/cms-kaihai-confirm",
)
LEGACY_KAIHAI = os.environ.get("LEGACY_KAIHAI", "false").lower() == "true"
WEBHOOK_TIMEOUT = float(os.environ.get("N8N_WEBHOOK_TIMEOUT", "10"))


# ============================================================================
# 处理函数（新：webhook · 旧：SQLite 直写）
# ============================================================================

def handle_action_webhook(row, action):
    """新路径：POST N8N webhook，N8N 异步处理 PG + 联动 + 飞书 + audit。"""
    run_id = f"kaihai_{row['jan']}_{int(time.time())}"
    payload = {
        "run_id": run_id,
        "jan": row["jan"],
        "source": row["source"],
        "signal_type": row["signal_type"],
        "detected_at": row["detected_at"],
        "action": action,
        "operator": "BOSS",
    }
    try:
        resp = requests.post(N8N_WEBHOOK_KAIHAI, json=payload, timeout=WEBHOOK_TIMEOUT)
    except requests.RequestException as e:
        st.error(t(f"❌ N8N webhook 调用失败: {e}"))
        st.caption(t(f"webhook url: {N8N_WEBHOOK_KAIHAI}"))
        st.caption(t("救场：设 LEGACY_KAIHAI=true 切回老路径，或检查 N8N 容器"))
        return False
    if resp.status_code not in (200, 202):
        st.warning(t(f"⚠️ N8N 响应非 2xx: {resp.status_code} body={resp.text[:200]}"))
        return False
    st.toast(t(f"✅ {action} 已入队 · run_id={run_id}"), icon="🚀")
    return True


def handle_action_legacy(conn, row, action):
    """老路径（v1.x）：Streamlit 直写 SQLite + 联动 + 飞书。LEGACY_KAIHAI=true 时启用。"""
    ts = datetime.now().isoformat()
    conn.execute(
        """
        UPDATE discontinue_alerts
        SET acknowledged_by = ?, acknowledged_at = ?, action = ?
        WHERE jan = ? AND source = ? AND signal_type = ? AND detected_at = ?
        """,
        ("BOSS", ts, action, row["jan"], row["source"], row["signal_type"], row["detected_at"]),
    )
    if action == "取扱中止":
        conn.execute("UPDATE item_master SET rank = ? WHERE jan = ?", ("停売", row["jan"]))
    conn.commit()
    st.toast(t(f"✅ [LEGACY] {action} 已写入"), icon="📝")
    return True


def handle_action(conn, row, action):
    if LEGACY_KAIHAI:
        return handle_action_legacy(conn, row, action)
    return handle_action_webhook(row, action)


# ============================================================================
# 页面布局
# ============================================================================

st.title(t("⚠️ 改废确认"))
st.caption(t("月度改廃信号审核 · 三按钮：取扱中止 / 継続 / 代替品調査 · 取扱中止自动联动等级=停売"))

if LEGACY_KAIHAI:
    st.warning(t("⚠️ LEGACY 模式：直写 SQLite（紧急回退）· 解除回退请 unset LEGACY_KAIHAI 并重启容器"))
else:
    st.caption(t(f"🚀 N8N 模式 · webhook → cms-api → PG · `{N8N_WEBHOOK_KAIHAI}`"))

from shared.db import get_connection, DB_PATH
DB = DB_PATH
conn = get_connection()

tab1, tab2, tab3 = st.tabs([t("🆕 待確認"), t("📜 历史回看"), t("➕ 手动添加")])

# ============================================================================
# Tab 1: 待確認
# ============================================================================

with tab1:
    _rows_pending = conn.execute(
        """
        SELECT
            a.*,
            COALESCE(i.display_name, '-') AS name,
            i.qty_on_hand AS qty,
            i.handling_status AS netsuite_status
        FROM discontinue_alerts a
        LEFT JOIN (
            SELECT item_code, MIN(display_name) AS display_name,
                   SUM(qty_on_hand) AS qty_on_hand,
                   MIN(handling_status) AS handling_status
            FROM nst_inventory_snapshot
            GROUP BY item_code
        ) i ON a.jan = i.item_code
        WHERE a.acknowledged_by IS NULL
          AND (i.handling_status IS NULL OR i.handling_status NOT IN ('取扱中止', 'メーカー取扱中止'))
        ORDER BY a.detected_at DESC
        """
    ).fetchall()
    pending = pd.DataFrame([dict(r) for r in _rows_pending])

    if pending.empty:
        st.success(t("✅ 暂无待確認改廃信号"))
    else:
        st.markdown(t(f"### 待確認 {len(pending)} 条"))

        sources = sorted(pending["source"].unique().tolist())
        sel_src = st.multiselect(t("来源筛选"), options=sources, default=sources)
        view = pending[pending["source"].isin(sel_src)] if sel_src else pending

        for idx, row in view.iterrows():
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3, 1, 1, 2])

                c1.markdown(
                    f"""
**JAN**: `{row['jan']}` · **{row.get('name', '?')}**

来源: {row['source']} · 信号: {row['signal_type']} · 库存: {row.get('qty', '?')}

检测时间: {row['detected_at']}
                """
                )

                if c2.button(t("🚫 取扱中止"), key=f"halt-{idx}"):
                    if handle_action(conn, row, "取扱中止"):
                        st.rerun()

                if c3.button(t("✅ 継続"), key=f"keep-{idx}"):
                    if handle_action(conn, row, "継続"):
                        st.rerun()

                if c4.button(t("🔍 代替品調査"), key=f"investigate-{idx}"):
                    if handle_action(conn, row, "代替品調査"):
                        st.rerun()


# ============================================================================
# Tab 2: 历史回看
# ============================================================================

with tab2:
    _rows_hist = conn.execute(
        """
        SELECT * FROM discontinue_alerts
        WHERE acknowledged_by IS NOT NULL
        ORDER BY acknowledged_at DESC
        LIMIT 500
        """
    ).fetchall()
    history = pd.DataFrame([dict(r) for r in _rows_hist])

    if history.empty:
        st.info(t("暂无历史确认记录"))
    else:
        history["month"] = pd.to_datetime(history["acknowledged_at"], format="mixed", errors="coerce", utc=True).dt.strftime("%Y-%m")
        months = sorted(history["month"].unique().tolist(), reverse=True)
        sel_m = st.multiselect(t("月份筛选"), options=months, default=months)

        if sel_m:
            mask = history["month"].isin(sel_m)
            view_history = history[mask].drop(columns=["month"])
        else:
            view_history = history.drop(columns=["month"])

        st.dataframe(localize_df(view_history), use_container_width=True, height=500)


# ============================================================================
# Tab 3: ➕ 手动添加改廃信号
# ============================================================================

with tab3:
    # ────────────────────────────────────────────────
    # 上半部分：手动触发 N8N 改廃扫描（不用等月度 cron）
    # ────────────────────────────────────────────────
    st.markdown(t("### 🚀 手动启动 N8N 改廃扫描"))
    st.caption(t("调用 N8N stock-monitor workflow · 即时扫 NetSuite 库存 / 自动塞低库存 SKU 进待确认列表 · 完成后飞书通知"))

    n8n_stock_webhook = os.environ.get(
        "N8N_WEBHOOK_STOCK_MONITOR",
        "http://n8n:5678/webhook/stock-monitor-run",
    )

    col_x, col_y = st.columns([1, 3])
    if col_x.button(t("🚀 立即扫描"), type="primary", key="trigger_stock_monitor"):
        try:
            resp = requests.post(n8n_stock_webhook, json={"trigger": "manual_boss"},
                                  timeout=30)
            if resp.status_code in (200, 202):
                st.success(t(f"✅ N8N 扫描已启动 · 后台执行约 1-5 分钟 · 完成后飞书通知 · http {resp.status_code}"))
                st.caption(t("提示：扫完后切到「🆕 待確認」tab 看新出现的低库存 SKU"))
            else:
                st.error(t(f"❌ N8N 响应 {resp.status_code}: {resp.text[:200]}"))
        except requests.RequestException as e:
            st.error(t(f"❌ 调用 N8N 失败: {e}"))
            st.caption(t(f"webhook: {n8n_stock_webhook}"))
    col_y.caption(t(f"webhook url: `{n8n_stock_webhook}`"))

    st.markdown(t("---"))

    # ────────────────────────────────────────────────
    # 下半部分：手动添加单条改廃信号
    # ────────────────────────────────────────────────
    st.markdown(t("### 手动标记某 SKU 为待改廃確認"))
    st.caption(t("用于：临时下架、Boss 主动判定停售、月度 cron 之外的紧急流入"))

    with st.form("kaihai_manual_add", clear_on_submit=True):
        col_a, col_b = st.columns([2, 1])
        jan_input = col_a.text_input(t("JAN コード"), placeholder="4901234567890",
                                      help=t("必填 · 13 位 JAN/UPC 码"))
        signal_type_input = col_b.selectbox(
            t("信号类型"),
            options=["boss_flagged", "low_inventory", "supplier_discontinued",
                     "season_end", "manual_other"],
            help=t("Boss 手动判定一般选 boss_flagged"),
        )

        col_c, col_d = st.columns([1, 1])
        source_input = col_c.text_input(t("来源标签"), value="manual_boss",
                                          help=t("用于历史回看筛选区分自动 cron vs 手动"))
        memo_input = col_d.text_input(t("备注（可选）"), placeholder=t("如：供应商通告 / 销量持续低迷"))

        submitted = st.form_submit_button(t("➕ 添加到待确认队列"), type="primary")

        if submitted:
            if not jan_input or not jan_input.strip():
                st.error(t("❌ JAN 必填"))
            else:
                jan_clean = jan_input.strip()
                signal_full = signal_type_input
                if memo_input.strip():
                    signal_full = f"{signal_type_input}: {memo_input.strip()}"
                try:
                    # 用 cms-api 当前用的同一个 conn（兼容 SQLite + Postgres）
                    conn.execute(
                        """
                        INSERT INTO discontinue_alerts (jan, source, signal_type, detected_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (jan_clean, source_input.strip() or "manual_boss",
                         signal_full, datetime.now().isoformat()),
                    )
                    conn.commit()
                    st.success(t(f"✅ 已添加 JAN={jan_clean} 到待确认 · 切换到「🆕 待確認」tab 处理"))
                    st.balloons()
                except Exception as e:
                    st.error(t(f"❌ 写入失败: {e}"))

    # 显示最近 10 条手动添加的
    st.markdown(t("---"))
    st.markdown(t("#### 最近 10 条 (来源含 manual)"))
    _recent_manual = conn.execute(
        """
        SELECT jan, source, signal_type, detected_at, acknowledged_by, action
        FROM discontinue_alerts
        WHERE source LIKE ?
        ORDER BY detected_at DESC
        LIMIT 10
        """,
        ("%manual%",),
    ).fetchall()
    if _recent_manual:
        df_recent = pd.DataFrame([dict(r) for r in _recent_manual])
        st.dataframe(df_recent, use_container_width=True, hide_index=True)
    else:
        st.caption(t("暂无手动添加记录"))


conn.close()
