"""模块 · 入荷困難商品（legacy `difficult_items` 替代）。

纯本地表单 + 列表 + 历史日志。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from shared.editor_state import lang_stable_editor
from shared.i18n import t, lang_selector
from shared.i18n_columns import localize_records

from shared.db import get_connection

st.set_page_config(page_title=t("入荷困難商品"), page_icon="🚫", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🚫 入荷困難商品"))
st.caption(t("人工录入难以入荷的商品 + 原因 + 备注 · 全量历史保留"))


def _now():
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# 顶部：录入新条目
# ============================================================
with st.expander(t("➕ 新规录入"), expanded=False):
    with st.form("add_difficult", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            item_key = st.text_input(t("ブランド / 商品名 / JAN"), placeholder=t("例: ABC ブランド or 4901111310490"))
        with c2:
            reason = st.text_input(t("入荷困難理由"), placeholder=t("例: 廃番 / 在庫無し / 仕入価格高騰"))
        note = st.text_area(t("备注（可选）"), height=80)
        if st.form_submit_button(t("登録"), type="primary"):
            if not item_key.strip() or not reason.strip():
                st.error(t("ブランド/商品名/JAN 与 理由 都必须填写。"))
            else:
                now = _now()
                cur = conn.execute(
                    "INSERT INTO difficult_items (item_key, reason, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (item_key.strip(), reason.strip(), (note or "").strip() or None, now, now),
                )
                new_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO difficult_items_history (item_id, item_key, reason, note, action, action_at)
                    VALUES (?, ?, ?, ?, 'insert', ?)
                    """,
                    (new_id, item_key.strip(), reason.strip(), (note or "").strip() or None, now),
                )
                conn.commit()
                st.success(t("✅ 已登记 #{id}").format(id=new_id))
                st.rerun()


# ============================================================
# 列表 + 删除
# ============================================================
st.subheader(t("📋 现行リスト"))

rows = conn.execute(
    """
    SELECT id, item_key, reason, note, created_at, updated_at
    FROM difficult_items
    ORDER BY id DESC
    """
).fetchall()

if not rows:
    st.info(t("还没有任何记录。点上面「➕ 新规录入」开始。"))
else:
    df = pd.DataFrame([dict(r) for r in rows])
    df.insert(0, "sel", False)   # 列名は固定キー（言語を切り替えても選択を保つ）。表示名は label

    edited = lang_stable_editor(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "sel": st.column_config.CheckboxColumn(t("选择")),
            "id": st.column_config.NumberColumn("ID", disabled=True),
            "item_key": st.column_config.TextColumn(t("ブランド/商品"), disabled=True),
            "reason": st.column_config.TextColumn(t("理由"), disabled=True),
            "note": st.column_config.TextColumn(t("備考"), disabled=True),
            "created_at": st.column_config.TextColumn(t("作成"), disabled=True),
            "updated_at": st.column_config.TextColumn(t("更新"), disabled=True),
        },
        disabled=["id", "item_key", "reason", "note", "created_at", "updated_at"],
        key="diff_items_table", reset=tuple(df["id"]),
    )

    selected_ids = edited[edited["sel"]]["id"].tolist()
    c1, c2 = st.columns([1, 1])
    with c1:
        st.metric(t("已选行数"), f"{len(selected_ids)}")
    with c2:
        if st.button(
            t("🗑 删除选中 {n} 条").format(n=len(selected_ids)),
            type="primary" if selected_ids else "secondary",
            disabled=not selected_ids,
            use_container_width=True,
        ):
            now = _now()
            for _id in selected_ids:
                row = conn.execute(
                    "SELECT * FROM difficult_items WHERE id = ?", (_id,)
                ).fetchone()
                if row:
                    conn.execute(
                        """
                        INSERT INTO difficult_items_history (item_id, item_key, reason, note, action, action_at)
                        VALUES (?, ?, ?, ?, 'delete', ?)
                        """,
                        (row["id"], row["item_key"], row["reason"], row["note"], now),
                    )
                    conn.execute("DELETE FROM difficult_items WHERE id = ?", (_id,))
            conn.commit()
            st.success(t("✅ 已删除 {n} 条").format(n=len(selected_ids)))
            st.rerun()


# ============================================================
# 历史日志（最近 7 天）
# ============================================================
st.divider()
st.subheader(t("📜 操作历史（最近 7 天）"))

from datetime import datetime as _dt, timedelta as _td
_seven_days_ago = (_dt.now() - _td(days=7)).isoformat()
hist = conn.execute(
    """
    SELECT id, item_id, item_key, reason, note, action, action_at
    FROM difficult_items_history
    WHERE action_at >= ?
    ORDER BY id DESC
    LIMIT 200
    """,
    (_seven_days_ago,),
).fetchall()

if not hist:
    st.caption(t("（最近 7 天无操作）"))
else:
    st.dataframe(
        localize_records([dict(r) for r in hist]),
        use_container_width=True,
        hide_index=True,
    )
