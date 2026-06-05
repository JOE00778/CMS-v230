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

# JST(UTC+9) − 5h = UTC+4：以每日 05:00 JST（NST 同步时刻）为缓存翻新分界。
# 用固定偏移时区取日期，避免依赖宿主时区设置。
_DAILY_BOUNDARY = timezone(timedelta(hours=4))


def data_version() -> str:
    """当前缓存版本号（按「JST 05:00 分界的日期」）。

    每天 05:00 JST（NST daily_pull 时刻）之后翻新 → 缓存自动失效、重查到当天
    同步的新数据；同一天内保持不变 → 命中缓存秒开。
    """
    return datetime.now(_DAILY_BOUNDARY).strftime("%Y-%m-%d")


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
