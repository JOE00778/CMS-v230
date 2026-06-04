"""模块 #商品检索 · 筛选条件 dataclass + 校验.

设计与 modules/cost_sync/rules.py、modules/rank_classifier/rules.py 同级——
纯逻辑、零 streamlit/DB 依赖，便于 ATTACH-SQLite 单测直接验证。

筛选维度（≥5 维 · 对齐真实 nst.item_master_raw 列）：
  1. brand        → maker          列（NST「メーカー名 / 品牌」）
  2. category     → item_rank      列（NST 商品ランク · A/B/C/D + 中止/NEW）
  3. price_range  → cost_estimate  列（定義原価 · CMS 全局「商品原价」单一事实源）
  4. stock_status → in_stock 派生（库存快照 qty_on_hand · ALL/IN/OUT）
  5. created_at   → last_modified  列（NST 最終更新日 · 'YYYY-MM-DD' 区间）

全文搜索 keyword → display_name + maker（schema 无独立 description 列，
maker 是除商品名外唯一的自由文本描述字段）。

校验规则：
  - price_min / price_max 必须非负、min ≤ max
  - created_from / created_to 必须 'YYYY-MM-DD'、from ≤ to
  - stock_status ∈ {ALL, IN, OUT}
非法即 raise ValueError（让上层薄壳显式失败 · 对齐工作原则 #5）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STOCK_ALL = "ALL"
STOCK_IN = "IN"      # qty_on_hand > 0
STOCK_OUT = "OUT"    # qty_on_hand <= 0 或无库存记录
_STOCK_VALUES = {STOCK_ALL, STOCK_IN, STOCK_OUT}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class SearchFilters:
    """商品检索的全部筛选条件（≥5 维 + 全文）。

    所有字段都有默认（空 = 不筛该维），整体默认即「全量」。
    """

    keyword: str = ""                       # 全文：display_name + maker
    brands: list[str] = field(default_factory=list)        # maker IN (...)
    categories: list[str] = field(default_factory=list)    # item_rank IN (...)
    price_min: float | None = None          # cost_estimate >=
    price_max: float | None = None          # cost_estimate <=
    stock_status: str = STOCK_ALL           # ALL / IN / OUT
    created_from: str | None = None         # last_modified >= 'YYYY-MM-DD'
    created_to: str | None = None           # last_modified <= 'YYYY-MM-DD'
    hide_inactive: bool = True              # is_inactive 过滤

    def validate(self) -> "SearchFilters":
        """非法即 raise ValueError，合法返回 self（便于链式）。"""
        if self.price_min is not None and self.price_min < 0:
            raise ValueError(f"price_min 不能为负: {self.price_min}")
        if self.price_max is not None and self.price_max < 0:
            raise ValueError(f"price_max 不能为负: {self.price_max}")
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError(
                f"price_min({self.price_min}) > price_max({self.price_max})"
            )

        for label, v in (("created_from", self.created_from),
                         ("created_to", self.created_to)):
            if v is not None and not _DATE_RE.match(v):
                raise ValueError(f"{label} 必须 'YYYY-MM-DD' 格式: {v!r}")
        if (
            self.created_from is not None
            and self.created_to is not None
            and self.created_from > self.created_to
        ):
            raise ValueError(
                f"created_from({self.created_from}) > created_to({self.created_to})"
            )

        if self.stock_status not in _STOCK_VALUES:
            raise ValueError(
                f"stock_status 非法: {self.stock_status!r}（须 {_STOCK_VALUES}）"
            )
        return self

    def active_dims(self) -> list[str]:
        """返回当前生效的筛选维度名（供 UI / 日志显示）。"""
        dims = []
        if self.keyword.strip():
            dims.append("keyword")
        if self.brands:
            dims.append("brand")
        if self.categories:
            dims.append("category")
        if self.price_min is not None or self.price_max is not None:
            dims.append("price_range")
        if self.stock_status != STOCK_ALL:
            dims.append("stock_status")
        if self.created_from is not None or self.created_to is not None:
            dims.append("created_at")
        return dims
