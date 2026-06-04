"""模块 #商品检索 · 查询层.

把 SearchFilters 编译成参数化 SQL 并对 conn 执行（PG 本番 / ATTACH-SQLite 测试
两兼容 · 只用 `?` 占位 + 标准 ANSI 语法）。逻辑全沉淀在此，page 02 只调用。

数据源（对齐 pages/02 现有统一视图）：
- nst.item_master_raw    商品主档（display_name/maker/item_rank/cost_estimate/last_modified）
- nst.inventory_snapshot 最新库存快照（stock_status 派生）

stock_status 实现：LEFT JOIN 最新快照拿 qty_on_hand，IN=>0 / OUT<=0或无记录。
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from .filters import STOCK_ALL, STOCK_IN, STOCK_OUT, SearchFilters

# 输出列（page 表格 + CSV 导出共用）
RESULT_COLUMNS = [
    "internal_id", "item_code", "jan", "display_name", "maker",
    "item_rank", "handling_cd", "cost_estimate", "last_modified",
    "qty_on_hand",
]


def build_where(f: SearchFilters) -> tuple[str, list]:
    """SearchFilters → (WHERE 片段, params)。不含 'WHERE' 关键字。

    全文用 LIKE %kw%（display_name + maker · 大小写不敏感走 LOWER）。
    所有值参数化，杜绝注入。
    """
    f.validate()
    clauses: list[str] = []
    params: list = []

    kw = f.keyword.strip()
    if kw:
        like = f"%{kw.lower()}%"
        clauses.append(
            "(LOWER(COALESCE(im.display_name, '')) LIKE ? "
            "OR LOWER(COALESCE(im.maker, '')) LIKE ?)"
        )
        params += [like, like]

    if f.brands:
        ph = ",".join("?" for _ in f.brands)
        clauses.append(f"im.maker IN ({ph})")
        params += list(f.brands)

    if f.categories:
        ph = ",".join("?" for _ in f.categories)
        clauses.append(f"im.item_rank IN ({ph})")
        params += list(f.categories)

    if f.price_min is not None:
        clauses.append("im.cost_estimate >= ?")
        params.append(f.price_min)
    if f.price_max is not None:
        clauses.append("im.cost_estimate <= ?")
        params.append(f.price_max)

    if f.created_from is not None:
        clauses.append("im.last_modified >= ?")
        params.append(f.created_from)
    if f.created_to is not None:
        # 含当日：last_modified 可能带时分秒，用 < (to + 1日) 不易；这里按
        # 'YYYY-MM-DD' 前缀比较，追加 'z' 上界使同日 'YYYY-MM-DD...' 全部命中。
        clauses.append("im.last_modified <= ?")
        params.append(f.created_to + "￿")

    if f.hide_inactive:
        # is_inactive は PG では boolean（SQLite では INTEGER）→ FALSE で両対応
        clauses.append("COALESCE(im.is_inactive, FALSE) = FALSE")

    if f.stock_status == STOCK_IN:
        clauses.append("COALESCE(inv.qty_on_hand, 0) > 0")
    elif f.stock_status == STOCK_OUT:
        clauses.append("COALESCE(inv.qty_on_hand, 0) <= 0")
    # STOCK_ALL → 不加

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


def _base_sql(where: str) -> str:
    return f"""
        WITH inv AS (
            SELECT item_internal_id, qty_on_hand
            FROM nst.inventory_snapshot
            WHERE snapshot_date = (
                SELECT MAX(snapshot_date) FROM nst.inventory_snapshot
            )
        )
        SELECT
            im.internal_id, im.item_code, im.jan, im.display_name,
            im.maker, im.item_rank, im.handling_cd, im.cost_estimate,
            im.last_modified,
            COALESCE(inv.qty_on_hand, 0) AS qty_on_hand
        FROM nst.item_master_raw im
        LEFT JOIN inv ON inv.item_internal_id = im.internal_id
        WHERE {where}
        ORDER BY im.item_code
    """


def search_items(conn, f: SearchFilters) -> pd.DataFrame:
    """执行检索，返回 DataFrame（列 = RESULT_COLUMNS）。"""
    where, params = build_where(f)
    cur = conn.execute(_base_sql(where), params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description] if cur.description else RESULT_COLUMNS
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


def distinct_values(conn, column: str) -> list[str]:
    """取某列去重非空值（供 UI 下拉 · 仅白名单列防注入）。"""
    allowed = {"maker", "item_rank", "handling_cd"}
    if column not in allowed:
        raise ValueError(f"不允许的列: {column}（白名单 {allowed}）")
    cur = conn.execute(
        f"SELECT DISTINCT {column} AS v FROM nst.item_master_raw "
        f"WHERE {column} IS NOT NULL AND {column} <> '' ORDER BY {column}"
    )
    out = []
    for r in cur.fetchall():
        v = r[0] if not isinstance(r, sqlite3.Row) else r["v"]
        if v is not None and str(v).strip():
            out.append(str(v))
    return out
