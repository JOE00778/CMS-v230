"""_PostgresAdapter SQL 改写测试（不需要真实 Postgres）。

验证：
- INSERT OR REPLACE INTO X (cols) VALUES (...) → ON CONFLICT (pk) DO UPDATE SET ...=EXCLUDED.*
- INSERT OR IGNORE INTO X (cols) VALUES (...) → ON CONFLICT DO NOTHING
- ? 占位符 → %s
- 普通 SELECT / INSERT 不动
- 未登记的表抛 RuntimeError
"""
from __future__ import annotations

import pytest

from shared.db import _PostgresAdapter


def test_insert_or_replace_basic():
    sql = "INSERT OR REPLACE INTO shopee_payouts (payout_id, total_payout) VALUES (?, ?)"
    out = _PostgresAdapter._adapt_sql(sql)
    assert "INSERT INTO shopee_payouts" in out
    assert "ON CONFLICT (payout_id) DO UPDATE SET" in out
    assert "total_payout=EXCLUDED.total_payout" in out
    assert "%s" in out and "?" not in out


def test_insert_or_replace_composite_pk():
    sql = (
        "INSERT OR REPLACE INTO inventory_snapshot "
        "(internal_id, location, bin_number, snapshot_at, qty_on_hand) "
        "VALUES (:a, :b, :c, :d, :e)"
    )
    out = _PostgresAdapter._adapt_sql(sql)
    assert "ON CONFLICT (internal_id, location, bin_number, snapshot_at)" in out
    assert "qty_on_hand=EXCLUDED.qty_on_hand" in out
    # PK 列不应出现在 SET 子句
    assert "internal_id=EXCLUDED.internal_id" not in out
    assert "snapshot_at=EXCLUDED.snapshot_at" not in out


def test_insert_or_replace_multiline():
    sql = """
        INSERT OR REPLACE INTO inventory_turnover (
            item_code, description, cost,
            period_start, period_end
        ) VALUES (
            :item_code, :description, :cost,
            :period_start, :period_end
        )
    """
    out = _PostgresAdapter._adapt_sql(sql)
    assert "INSERT INTO inventory_turnover" in out
    assert "ON CONFLICT (item_code, period_start, period_end)" in out
    assert "description=EXCLUDED.description" in out
    assert "cost=EXCLUDED.cost" in out


def test_insert_or_ignore():
    sql = "INSERT OR IGNORE INTO _schema_version (version, applied_at) VALUES (?, ?)"
    out = _PostgresAdapter._adapt_sql(sql)
    assert "INSERT INTO _schema_version" in out
    assert "ON CONFLICT DO NOTHING" in out
    assert "OR IGNORE" not in out


def test_insert_or_ignore_store_monthly():
    sql = """
        INSERT OR IGNORE INTO store_monthly (
            year_month, market, store_id, revenue
        ) VALUES (?, ?, ?, ?)
    """
    out = _PostgresAdapter._adapt_sql(sql)
    assert "INSERT INTO store_monthly" in out
    assert "ON CONFLICT DO NOTHING" in out


def test_unknown_table_raises():
    sql = "INSERT OR REPLACE INTO unknown_table (a, b) VALUES (?, ?)"
    with pytest.raises(RuntimeError, match="未登记表"):
        _PostgresAdapter._adapt_sql(sql)


def test_plain_select_unchanged():
    sql = "SELECT * FROM item WHERE jan = ?"
    out = _PostgresAdapter._adapt_sql(sql)
    assert out == "SELECT * FROM item WHERE jan = %s"


def test_plain_insert_unchanged():
    sql = "INSERT INTO _ingest_runs (ingestor, source_file) VALUES (?, ?)"
    out = _PostgresAdapter._adapt_sql(sql)
    assert "ON CONFLICT" not in out
    assert "INSERT INTO _ingest_runs" in out
    assert "%s" in out


def test_named_param_basic():
    """SQLite :name → Postgres %(name)s（psycopg2 pyformat）"""
    sql = "INSERT OR REPLACE INTO nst_item_summary (item_code, avg_cost) VALUES (:item_code, :avg_cost)"
    out = _PostgresAdapter._adapt_sql(sql)
    assert "%(item_code)s" in out
    assert "%(avg_cost)s" in out
    assert ":item_code" not in out


def test_named_param_with_update():
    sql = "UPDATE item_v2 SET maker = :maker, updated_at = :ts WHERE jan = :jan"
    out = _PostgresAdapter._adapt_sql(sql)
    assert "%(maker)s" in out
    assert "%(ts)s" in out
    assert "%(jan)s" in out


def test_named_param_does_not_break_type_cast():
    """::text Postgres 类型转换不能被误改"""
    sql = "SELECT id::text, name::varchar FROM foo WHERE id = :id"
    out = _PostgresAdapter._adapt_sql(sql)
    assert "::text" in out
    assert "::varchar" in out
    assert "%(id)s" in out


def test_all_known_ingest_tables_register():
    """确保所有 ingest 链路上会写入的表都登记了 conflict 列。"""
    expected_tables = {
        "shopee_payouts", "inventory_snapshot", "inventory_turnover",
        "shopee_orders_raw", "shopee_income_lines", "shopee_orders",
        "supplier_cost", "supply_cycle", "supplier_jan_list",
        "item", "item_master", "item_master_netsuite", "store_monthly",
        "nst_turnover", "nst_store_sales", "nst_inventory_snapshot",
        "nst_item_summary",
        "operation_advice_monthly", "stock_sales_ratio_monthly",
        "cross_ratio_monthly", "health_grade_monthly", "rank_history",
        "_schema_version",
    }
    registered = set(_PostgresAdapter._UPSERT_CONFLICT.keys())
    missing = expected_tables - registered
    assert not missing, f"未登记的表：{missing}"


# ============================================================
# 只読オートフィニッシュ（2026-08-07 · idle in transaction 対策）
# ============================================================
from shared.db import _AutoFinishCursor  # noqa: E402

_TXN_IDLE, _TXN_INTRANS = 0, 2  # psycopg2: PQTRANS_IDLE / PQTRANS_INTRANS


class _FakeCursor:
    def __init__(self, raw):
        self._raw = raw
        self.executed: list[tuple] = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._raw.status = _TXN_INTRANS  # 実 psycopg2 同様、実行で事務が開く

    def executemany(self, sql, seq):
        self.executed.append((sql, list(seq)))
        self._raw.status = _TXN_INTRANS

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


class _FakeRaw:
    def __init__(self, status=_TXN_IDLE):
        self.status = status
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def get_transaction_status(self):
        return self.status

    def commit(self):
        self.commits += 1
        self.status = _TXN_IDLE

    def rollback(self):
        self.rollbacks += 1
        self.status = _TXN_IDLE


def _adapter(status=_TXN_IDLE):
    raw = _FakeRaw(status)
    return _PostgresAdapter(raw), raw


def test_readonly_select_autocommits_and_leaves_no_txn():
    conn, raw = _adapter()
    conn.execute("SELECT jan FROM nst.item_master_raw WHERE jan = ?", ("4901",))
    assert raw.commits == 1 and raw.status == _TXN_IDLE


def test_write_does_not_autocommit():
    conn, raw = _adapter()
    conn.execute("INSERT OR IGNORE INTO _schema_version (version, applied_at) VALUES (?, ?)", (1, 2))
    assert raw.commits == 0 and raw.status == _TXN_INTRANS


def test_select_inside_write_txn_is_untouched():
    """page34 保存フロー回帰：UPDATE の合間に挟む SELECT で事務を切ってはいけない。"""
    conn, raw = _adapter()
    conn.execute("UPDATE sourcing.supplier SET note=? WHERE supplier_name=?", ("x", "y"))
    assert raw.status == _TXN_INTRANS
    conn.execute("SELECT 1 FROM sourcing.supplier WHERE supplier_name=?", ("z",))
    assert raw.commits == 0 and raw.status == _TXN_INTRANS  # まだ事務中
    conn.commit()
    assert raw.commits == 1


def test_with_cte_readonly_autocommits():
    conn, raw = _adapter()
    conn.execute("WITH win AS (SELECT 1 AS x) SELECT * FROM win")
    assert raw.commits == 1


def test_with_cte_containing_write_is_untouched():
    conn, raw = _adapter()
    conn.execute("WITH ins AS (INSERT INTO t (a) VALUES (1) RETURNING a) SELECT * FROM ins")
    assert raw.commits == 0


def test_locking_and_multi_statement_and_select_into_not_finished():
    for sql in (
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t FOR NO KEY UPDATE",
        "SELECT 1; UPDATE t SET a=1",
        "SELECT * INTO newtab FROM t",
    ):
        conn, raw = _adapter()
        conn.execute(sql)
        assert raw.commits == 0, sql


def test_readonly_with_comment_prefix_and_trailing_semicolon():
    conn, raw = _adapter()
    conn.execute("-- KPI 集計\nSELECT count(*) FROM t;")
    assert raw.commits == 1


def test_is_readonly_sql_word_boundaries():
    # updated_at 等の列名で書き込み扱いに誤爆しない
    assert _PostgresAdapter._is_readonly_sql(
        "WITH x AS (SELECT updated_at, deleted_flag FROM t) SELECT * FROM x")
    # 文字列内の ';' や 'delete' は書き扱いに倒れる（無害側）
    assert not _PostgresAdapter._is_readonly_sql("SELECT * FROM t WHERE note = 'a;b'")
    assert not _PostgresAdapter._is_readonly_sql("WITH x AS (SELECT 1) SELECT 'delete me'")


def test_pandas_cursor_path_autocommits_readonly():
    """pd.read_sql は conn.cursor() 経由 → wrapper が同じ判定で事務を閉じる。"""
    conn, raw = _adapter()
    cur = conn.cursor()
    assert isinstance(cur, _AutoFinishCursor)
    cur.execute("SELECT * FROM marketing.v_marketing_data_freshness")
    assert raw.commits == 1 and raw.status == _TXN_IDLE
    assert cur.fetchall() == []          # 透過委譲
    cur.close()


def test_pandas_cursor_path_write_untouched():
    conn, raw = _adapter()
    cur = conn.cursor()
    cur.execute("INSERT INTO t (a) VALUES (%s)", (1,))
    assert raw.commits == 0 and raw.status == _TXN_INTRANS
