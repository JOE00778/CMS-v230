"""共享テスト用 nst.* スキーマ shim — PG 移行後の schema 限定表を SQLite で再現する。

本番（PostgreSQL）は `nst` スキーマに sales_monthly / item_master_raw /
inventory_snapshot / inventory_activity_monthly を持つ（2026-05-26 旧 shop_sales /
item_v2 / item_inventory_snapshot_v2 / item_monthly_turnover を置換）。
SQLite には schema 概念が無いので `ATTACH DATABASE ':memory:' AS nst` で同名前空間を
作り、業務コードの `FROM nst.<table>` 参照をそのまま解決させる。

JAN ↔ internal_id の対応は seed_item が透過的に張る（テスト側は JAN だけ意識すればよい）。
列名は業務コードのクエリ（shared/purchase_engine.py · modules/rank_classifier/proposal.py）
に合わせてある。
"""
from __future__ import annotations

import sqlite3

# 本番クエリが参照する列のみを持つ最小スキーマ
_NST_DDL = """
CREATE TABLE nst.item_master_raw (
    internal_id   TEXT,
    jan           TEXT,
    item_code     TEXT,
    display_name  TEXT,
    maker         TEXT,
    item_rank     TEXT,
    handling_cd   TEXT,
    is_inactive   INTEGER DEFAULT 0,
    cost_estimate REAL,
    last_modified TEXT
);
CREATE TABLE nst.sales_monthly (
    item_internal_id TEXT,
    year_month       TEXT,
    qty_sold         REAL,
    revenue          REAL,
    gross_profit     REAL
);
CREATE TABLE nst.inventory_snapshot (
    item_internal_id TEXT,
    warehouse        TEXT,
    snapshot_date    TEXT,
    qty_on_hand      REAL,
    qty_available    REAL,
    qty_on_order     REAL
);
CREATE TABLE nst.inventory_activity_monthly (
    item_internal_id TEXT,
    year_month       TEXT,
    opening_qty      REAL,
    received_qty     REAL,
    sold_qty         REAL
);
"""


def iid(jan: str) -> str:
    """JAN → 安定した internal_id（テスト内で JAN と 1:1）。"""
    return f"IID-{jan}"


def new_conn() -> sqlite3.Connection:
    """`nst` スキーマを ATTACH 済みの in-memory SQLite 接続を返す。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ':memory:' AS nst")
    c.executescript(_NST_DDL)
    return c


def seed_item(c, jan, *, display_name="商品", maker="メーカーX",
              item_rank=None, handling_cd="取扱中", item_code=None,
              internal_id=None, is_inactive=0, cost_estimate=None,
              last_modified=None) -> str:
    """nst.item_master_raw に 1 行入れて internal_id を返す。"""
    iid_ = internal_id or iid(jan)
    c.execute(
        "INSERT INTO nst.item_master_raw "
        "(internal_id, jan, item_code, display_name, maker, item_rank, handling_cd, "
        " is_inactive, cost_estimate, last_modified) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (iid_, jan, item_code or jan, display_name, maker, item_rank, handling_cd,
         1 if is_inactive else 0, cost_estimate, last_modified),
    )
    return iid_


def seed_sales(c, jan, year_month, qty_sold, *, revenue=0.0, gross_profit=0.0,
               internal_id=None) -> None:
    c.execute(
        "INSERT INTO nst.sales_monthly "
        "(item_internal_id, year_month, qty_sold, revenue, gross_profit) "
        "VALUES (?,?,?,?,?)",
        (internal_id or iid(jan), year_month, qty_sold, revenue, gross_profit),
    )


def seed_inventory(c, jan, *, qty_on_hand=0.0, committed=0.0, qty_on_order=0.0,
                   warehouse="JD-物流-千葉", snapshot_date="2026-04-30",
                   internal_id=None) -> None:
    """qty_available = qty_on_hand − committed（業務コードの committed 算出に合わせる）。"""
    c.execute(
        "INSERT INTO nst.inventory_snapshot "
        "(item_internal_id, warehouse, snapshot_date, qty_on_hand, qty_available, qty_on_order) "
        "VALUES (?,?,?,?,?,?)",
        (internal_id or iid(jan), warehouse, snapshot_date,
         qty_on_hand, qty_on_hand - committed, qty_on_order),
    )


def seed_turnover(c, jan, year_month, *, opening_qty=0.0, received_qty=0.0,
                  sold_qty=0.0, internal_id=None) -> None:
    c.execute(
        "INSERT INTO nst.inventory_activity_monthly "
        "(item_internal_id, year_month, opening_qty, received_qty, sold_qty) "
        "VALUES (?,?,?,?,?)",
        (internal_id or iid(jan), year_month, opening_qty, received_qty, sold_qty),
    )
