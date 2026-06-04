"""模块 #JDL vs NST 重量对比 · 查询 + 计算层.

逻辑全沉淀在此，page 02 Tab2 仅调用：
- load_compare(conn)   → 原始 DataFrame（活跃 SKU × NST/JDL 各重量列）
- compute_compare(df)  → 追加 diff_g / diff_pct 列（JDL − NST package）
- coverage_stats(df)   → 覆盖率/一致性统计 dict（指标卡用）

只用标准 SQL（PG 本番 / SQLite 测试两兼容），值不参数化（无外部输入）。
"""
from __future__ import annotations

import pandas as pd

# 输出列（page 表格 + CSV 共用，diff_* 由 compute_compare 追加）
COMPARE_COLUMNS = [
    "jan", "display_name", "maker", "item_rank",
    "nst_item_g", "nst_package_g", "nst_carton_g", "jdl_wms_g",
]

_COMPARE_SQL = """
    SELECT
        im.jan,
        im.display_name,
        im.maker,
        im.item_rank,
        im.item_weight_g    AS nst_item_g,
        im.package_weight_g AS nst_package_g,
        im.carton_weight_g  AS nst_carton_g,
        gd.wms_gross_weight_g AS jdl_wms_g
    FROM nst.item_master_raw im
    LEFT JOIN jdl.v_goods_dimensions gd ON gd.jan = im.jan
    WHERE im.is_inactive IS NOT TRUE
      AND im.jan IS NOT NULL AND im.jan <> ''
"""


def load_compare(conn) -> pd.DataFrame:
    """执行对比查询，返回 DataFrame（列 = COMPARE_COLUMNS）。"""
    cur = conn.execute(_COMPARE_SQL)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else COMPARE_COLUMNS
    df = pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)
    for c in ("nst_item_g", "nst_package_g", "nst_carton_g", "jdl_wms_g"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def compute_compare(df: pd.DataFrame) -> pd.DataFrame:
    """追加 diff_g / diff_pct（JDL 实测 − NST package_weight）。"""
    out = df.copy()
    out["diff_g"] = out["jdl_wms_g"] - out["nst_package_g"]
    out["diff_pct"] = (
        out["diff_g"] / out["nst_package_g"].where(out["nst_package_g"] > 0)
    ) * 100
    return out


def coverage_stats(df: pd.DataFrame) -> dict:
    """覆盖率/一致性统计（指标卡 + 明细基础）。

    返回 keys：n_total / n_nst / n_jdl / n_cmp / n_close / n_diff_big /
    comparable（可对比子集 · 已按 |diff_pct| 降序）。
    """
    n_total = len(df)
    n_nst = int(df["nst_package_g"].gt(0).sum())
    n_jdl = int(df["jdl_wms_g"].gt(0).sum())
    comparable = df[df["nst_package_g"].gt(0) & df["jdl_wms_g"].gt(0)].copy()
    n_cmp = len(comparable)
    n_close = int(comparable["diff_pct"].abs().le(10).sum())
    n_diff_big = int(comparable["diff_pct"].abs().gt(30).sum())
    if n_cmp:
        comparable["abs_diff_pct"] = comparable["diff_pct"].abs()
        comparable = comparable.sort_values(
            "abs_diff_pct", ascending=False, na_position="last",
        )
    return {
        "n_total": n_total,
        "n_nst": n_nst,
        "n_jdl": n_jdl,
        "n_cmp": n_cmp,
        "n_close": n_close,
        "n_diff_big": n_diff_big,
        "comparable": comparable,
    }
