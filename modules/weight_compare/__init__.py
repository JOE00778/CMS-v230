"""模块 #JDL vs NST 重量对比（page 02 Tab2）— 查询 + 计算逻辑.

page 02 仅薄壳 UI（取连接 → 调 load_compare → 渲染表格/指标/CSV）。
所有 SQL + diff/覆盖率计算沉淀在此（对齐 modules/product_search 风格）。

数据源：
- nst.item_master_raw.{item_weight_g, package_weight_g, carton_weight_g}
- jdl.v_goods_dimensions.wms_gross_weight_g（仓库实测毛重，按 jan 关联）

口径：JDL 实测毛重 vs NST「包装重量」package_weight_g（同含包装总重）。
"""
from .queries import (
    COMPARE_COLUMNS,
    compute_compare,
    coverage_stats,
    load_compare,
)

__all__ = [
    "COMPARE_COLUMNS",
    "compute_compare",
    "coverage_stats",
    "load_compare",
]
