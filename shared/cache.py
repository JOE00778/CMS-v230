"""页面级查询缓存 · CMS 数据每天同步一次（NST daily_pull ~05:00 JST）。

缓存版本 = 以 JST 05:00 为分界的日期：当天内同一查询命中缓存（秒开），
每天数据同步后版本翻新、缓存自动失效重查。无需手动清缓存。

用法（替换页面里本地的 _df）：
    from shared.cache import cached_df, data_version

    def _df(sql, params=None):
        return cached_df(conn, sql, params, ver=data_version())

`cached_df` 第一参数下划线前缀（`_conn`）→ Streamlit 不参与哈希（连接对象
不可哈希、且同一 PG 可复用）；缓存 key = (sql, params, ver)。同一 PG 的相同
查询跨页面共享缓存。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import streamlit as st

# fallback 兜底：以 JST 06:00 为分界（NST Task Scheduler 注册在 05:00 JST + 余量）。
# 仅当读不到实际同步记录时使用。JST(UTC+9) − 6h = UTC+3。
_FALLBACK_BOUNDARY = timezone(timedelta(hours=3))


@st.cache_data(ttl=300, show_spinner=False)
def data_version() -> str:
    """缓存版本号 = NST 实际最后同步完成时间（`nst.pull_schedule.last_run_at` 最大值）。

    同步完成 → dispatcher 更新 last_run_at → 版本变 → cached_df 自动失效重查。
    **不依赖硬编码时间**：NST 用 dispatcher + `pull_schedule.run_time` 调度，同步
    几点跑都自动跟随。ttl=300 → 最多 5 分钟读一次 pull_schedule（很轻的 max 查询）。
    表不可用时 fallback 到「JST 06:00 分界的日期」。
    """
    try:
        from shared.db import get_connection
        row = get_connection().execute(
            "SELECT max(last_run_at) FROM nst.pull_schedule").fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return datetime.now(_FALLBACK_BOUNDARY).strftime("%Y-%m-%d")


@st.cache_data(ttl=3600, show_spinner=False)
def cached_df(_conn, sql: str, params: Optional[Sequence[Any]] = None,
              ver: str = ""):
    """带缓存的 SQL → DataFrame。包装 `shared.db_helpers.df`（行为完全一致）。

    - `_conn` 下划线前缀 → 不参与缓存哈希（连接不可哈希、同 PG 可复用）。
    - 缓存 key = (sql, params, ver)。`ver` 传 `data_version()` → 每天同步后失效。
    - `ttl=3600` 兜底：同一天内也最多缓存 1h，防极端长会话用过陈旧数据。
    - 仅用于只读 SELECT；写操作后需立即读最新的页面不要用本函数。
    """
    from shared.db_helpers import df as _raw_df
    return _raw_df(_conn, sql, params)
