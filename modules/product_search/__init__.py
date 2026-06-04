"""模块 #商品检索（T-008）— 多维筛选 + 全文搜索 + CSV 导出.

逻辑层（page 02 仅薄壳 UI · 全调本模块）：
- filters.SearchFilters  筛选条件 dataclass + validate()
- queries.search_items   编译参数化 SQL 并执行 → DataFrame
- export.to_csv_bytes    结果导出 CSV
"""
from .filters import (
    STOCK_ALL,
    STOCK_IN,
    STOCK_OUT,
    SearchFilters,
)
from .queries import (
    RESULT_COLUMNS,
    build_where,
    distinct_values,
    search_items,
)
from .export import to_csv_bytes, write_csv

__all__ = [
    "SearchFilters",
    "STOCK_ALL",
    "STOCK_IN",
    "STOCK_OUT",
    "RESULT_COLUMNS",
    "build_where",
    "distinct_values",
    "search_items",
    "to_csv_bytes",
    "write_csv",
]
