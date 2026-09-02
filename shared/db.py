"""共享：DB 路径与连接获取，所有页面统一从这里拿连接。

后端选择：
- 默认（无 DATABASE_URL）→ SQLite，本地文件 data_warehouse/warehouse.db
- 设 DATABASE_URL=postgresql://... → Postgres（Windows 笔记本 / NAS Self-hosted 模式）

兼容性：
- 返回对象始终支持 .execute(sql, params)、.executemany、.commit、.close
- row 始终支持 row["col"] / row[index] 双重访问
- SQL 占位符两边都接受 `?`（Postgres 模式自动转 %s）
- INSERT OR REPLACE / INSERT OR IGNORE 在 Postgres 模式下被适配层自动改写为
  ON CONFLICT (...) DO UPDATE SET ... = EXCLUDED.* / DO NOTHING（透明，业务代码无需改动）
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

import streamlit as st

from shared.schema_bootstrap import init_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
INPUTS_DIR = DATA_DIR / "inputs"
OUTPUTS_DIR = DATA_DIR / "outputs"
DB_PATH = PROJECT_ROOT / "data_warehouse" / "warehouse.db"


def _is_postgres() -> bool:
    """检测是否走 Postgres 后端。仅当 DATABASE_URL 以 postgres 开头才启用。"""
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith(("postgresql://", "postgres://"))


def _get_sqlite_connection() -> sqlite3.Connection:
    """SQLite 连接（默认 / 现有 Cloud 部署用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH).close()
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


_pg_schema_initialized = False  # 模块级 flag · 仅在 streamlit app 启动后第一次连库时跑


def _get_postgres_connection():
    """Postgres 连接（Windows 笔记本部署用）。延迟 import psycopg2，未装时优雅报错。

    第一次调用时自动跑 deploy/windows/schema.postgres.sql（IF NOT EXISTS 幂等），
    确保 v2 等新表在 Boss 重启 streamlit 时无需手工 DDL 即生效。
    """
    global _pg_schema_initialized
    try:
        import psycopg2
        from psycopg2.extras import DictCursor
    except ImportError as e:
        raise RuntimeError(
            "psycopg2 未安装但设置了 DATABASE_URL=postgresql://。"
            "请装 psycopg2-binary>=2.9 或取消 DATABASE_URL 走 SQLite。"
        ) from e
    raw = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=DictCursor)
    raw.set_session(autocommit=False)
    adapter = _PostgresAdapter(raw)

    if not _pg_schema_initialized:
        try:
            from shared.schema_bootstrap import (
                DEPRECATED_TABLES, PHASE4_REBUILD_TABLES,
            )
            # Phase 4 v2 表 schema 重建（必须在 schema.sql 跑前 DROP）
            for tbl in PHASE4_REBUILD_TABLES:
                try:
                    cur = raw.cursor()
                    cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
                    raw.commit()
                except Exception:
                    raw.rollback()

            schema_path = PROJECT_ROOT / "deploy" / "windows" / "schema.postgres.sql"
            if schema_path.exists():
                cur = raw.cursor()
                cur.execute(schema_path.read_text(encoding="utf-8"))
                raw.commit()
            # ALTER 加列（与 SQLite ALTERS 对齐，幂等）
            from shared.schema_bootstrap import ALTERS
            for table, col_def in ALTERS:
                try:
                    cur = raw.cursor()
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_def}")
                    raw.commit()
                except Exception:
                    raw.rollback()
            # 废弃表 DROP
            for tbl in DEPRECATED_TABLES:
                try:
                    cur = raw.cursor()
                    cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
                    raw.commit()
                except Exception:
                    raw.rollback()

            # Phase 4 · 旧表名 → VIEW 桥接（让 page SQL 不用改）
            from shared.schema_bootstrap import PHASE4_LEGACY_VIEWS
            for legacy, target_view in PHASE4_LEGACY_VIEWS:
                try:
                    cur = raw.cursor()
                    cur.execute(f"DROP VIEW IF EXISTS {legacy} CASCADE")
                    cur.execute(f"DROP TABLE IF EXISTS {legacy} CASCADE")
                    cur.execute(f"CREATE VIEW {legacy} AS SELECT * FROM {target_view}")
                    raw.commit()
                except Exception as e:
                    print(f"[postgres legacy view warn] {legacy}: {e}")
                    raw.rollback()
        except Exception as e:
            print(f"[postgres init warn] {e}")
            try:
                raw.rollback()
            except Exception:
                pass
        _pg_schema_initialized = True
    return adapter


# psycopg2 の定数（未インストール環境＝SQLite/テストでも db.py を import できるよう
# フォールバックを持つ。0 = PQTRANS_IDLE：トランザクション外）。
try:
    from psycopg2.extensions import TRANSACTION_STATUS_IDLE as _TXN_IDLE
except ImportError:
    _TXN_IDLE = 0


class _AutoFinishCursor:
    """pandas read_sql 等が conn.cursor() 経由で流す SELECT でも事務を残さないための wrapper。

    素の psycopg2 cursor に透過委譲しつつ、execute() だけ横取りして
    「トランザクション外から発行された単発の只読 SQL」なら実行直後に commit で
    事務を閉じる（判定は _PostgresAdapter.execute と同一）。SQL の書き換えは
    しない（従来の透過挙動そのまま）。
    """

    def __init__(self, cur, raw):
        self._cur, self._conn_raw = cur, raw

    def execute(self, sql, params=None):
        finish = (self._conn_raw.get_transaction_status() == _TXN_IDLE
                  and _PostgresAdapter._is_readonly_sql(sql))
        self._cur.execute(sql, params or ())
        if finish:
            self._conn_raw.commit()
        return self._cur

    def executemany(self, sql, params_seq):
        return self._cur.executemany(sql, params_seq)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._cur.close()
        return False


class _PostgresAdapter:
    """psycopg2 连接的 SQLite-like wrapper.

    让现有代码（用 conn.execute(sql, params)、row["col"]、? 占位符）
    在 Postgres 后端无缝工作，不改任何 ingester / page。

    透明改写：
    - `?` → `%s`
    - `INSERT OR REPLACE INTO X (cols) VALUES (...)`
        → `INSERT INTO X (cols) VALUES (...) ON CONFLICT (pk) DO UPDATE SET col=EXCLUDED.col, ...`
    - `INSERT OR IGNORE INTO X (cols) VALUES (...)`
        → `INSERT INTO X (cols) VALUES (...) ON CONFLICT DO NOTHING`

    只読オートフィニッシュ（2026-08-07·Boss 拍板 B案）：
    ページの大半は書き込み接続で只読 SELECT を流し、commit も rollback もしない
    → 接続が idle in transaction のままロックを持ち続け、DDL や autovacuum を
    ブロックしていた（page34 無言フリーズの温床）。対策として execute() は
    「トランザクション外から発行された単発の只読 SQL」に限り、実行直後に
    commit してトランザクションを閉じる。書き込みトランザクションの途中
    （get_transaction_status() != IDLE）では従来どおり何もしない＝
    UPDATE の合間に SELECT を挟むフロー（page34 保存処理等）は壊れない。
    """

    # 表 → conflict 列（PK 或 UNIQUE 约束的列），用于 INSERT OR REPLACE 改写。
    # 新增表如果用 INSERT OR REPLACE 写入，必须在此登记，否则会抛 RuntimeError。
    _UPSERT_CONFLICT: dict[str, tuple[str, ...]] = {
        "shopee_payouts": ("payout_id",),
        "inventory_snapshot": ("internal_id", "location", "bin_number", "snapshot_at"),
        "inventory_turnover": ("item_code", "period_start", "period_end"),
        "shopee_orders_raw": ("order_no",),
        "shopee_income_lines": ("order_no", "refund_id"),
        "shopee_orders": ("order_no", "sku_or_jan"),
        "supplier_cost": ("jan", "supplier_name"),
        "supply_cycle": ("jan",),
        "supplier_jan_list": ("jan", "supplier_name"),
        "supplier_quote": ("supplier_name", "jan"),
        "item": ("item_code",),
        "item_master": ("jan",),
        "item_master_netsuite": ("internal_id",),
        "store_monthly": ("year_month", "market", "store_id"),
        "nst_turnover": ("item_code", "department"),
        "nst_store_sales": ("fb_store", "item_code"),
        "nst_inventory_snapshot": ("internal_id", "location", "bin_number"),
        "nst_item_summary": ("item_code",),
        "operation_advice_monthly": ("sku", "year_month"),
        "stock_sales_ratio_monthly": ("sku", "year_month"),
        "cross_ratio_monthly": ("sku", "year_month"),
        "health_grade_monthly": ("sku", "year_month"),
        "rank_history": ("sku", "quarter"),
        "_schema_version": ("version",),
        # v2 数据模型（Phase 3.1, 2026-05-09）
        "item_v2": ("jan",),
        "market_segment": ("market_id",),
        "shop": ("shop_id",),
        "shop_monthly": ("shop_id", "year_month"),
        "item_purchase_history": ("po_number", "jan", "source"),
        "item_sales_history": ("jan", "period_start", "period_end", "channel", "source"),
        "item_inventory_snapshot_v2": ("jan", "location", "bin_number", "snapshot_at"),
        "shop_sales": ("shop_id", "jan", "granularity", "period_start", "period_end", "source"),
        "item_supplier_link": ("jan", "supplier_name"),
        # KPI 月度快照（Phase 2C, 2026-05-09）
        "kpi_monthly_history": ("year_month",),
        # ECMS 发货（2026-08-28）
        "ecms_shipment": ("reference_code", "env"),
        # Coupang → ECMS 发货（2026-08-30）
        "coupang_shipment_queue": ("order_id", "shipment_box_id"),
        "coupang_product_info": ("sku",),
    }

    _RE_OR_REPLACE = re.compile(
        r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\b",
        re.IGNORECASE | re.DOTALL,
    )
    _RE_OR_IGNORE = re.compile(
        r"\bINSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\b",
        re.IGNORECASE | re.DOTALL,
    )
    # 命名占位符 :name → %(name)s （psycopg2 pyformat 风格）
    # (?<!:) 跳过 ::text 类型转换；(?<!\w) 跳过单词中间的冒号（如 'http://'）
    _RE_NAMED_PARAM = re.compile(r"(?<!:)(?<!\w):([a-zA-Z_]\w*)\b")
    _RE_SQL_COMMENT = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.S)
    _RE_LOCKING_CLAUSE = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b")
    _RE_WRITE_WORD = re.compile(r"\b(insert|update|delete|merge)\b")

    @classmethod
    def _is_readonly_sql(cls, sql: str) -> bool:
        """単発で発行しても安全に commit で閉じられる只読 SQL かの保守判定。

        誤判定の方向を「読を書と誤る（→従来挙動のまま・無害）」に倒してある：
        - 文字列リテラル内の 'update' や ';' で False になっても、何も変わらないだけ
        - True と誤ると書き込み事務を分断しかねないので、迷う形は全部 False：
          複文（;）/ SELECT ... INTO（実は建表）/ ロック読（FOR UPDATE 等）/
          CTE 内に書き込み動詞を含む WITH
        """
        s = cls._RE_SQL_COMMENT.sub(" ", sql).strip().lower()
        core = s.rstrip(";").strip()          # 末尾の空 ; は許容
        if ";" in core:                        # 複文は不判定
            return False
        if core.startswith("select"):
            if cls._RE_LOCKING_CLAUSE.search(core):
                return False
            if re.search(r"\binto\b", core):   # SELECT INTO は建表＝書き込み
                return False
            return True
        if core.startswith("with"):
            return not cls._RE_WRITE_WORD.search(core)
        return False

    def __init__(self, raw):
        self._raw = raw

    @classmethod
    def _rewrite_upsert(cls, sql: str) -> str:
        """SQLite INSERT OR REPLACE/IGNORE → Postgres ON CONFLICT 等价语法。"""
        m = cls._RE_OR_REPLACE.search(sql)
        if m:
            table = m.group(1)
            cols_raw = m.group(2)
            cols = [c.strip() for c in cols_raw.split(",")]
            conflict = cls._UPSERT_CONFLICT.get(table)
            if not conflict:
                raise RuntimeError(
                    f"_PostgresAdapter: 未登记表 {table!r} 的 conflict 列。"
                    f"在 _UPSERT_CONFLICT 字典加映射后再跑（PK 或 UNIQUE 约束列）。"
                )
            update_set = ", ".join(
                f"{c}=EXCLUDED.{c}" for c in cols if c not in conflict
            )
            head = sql[: m.start()] + f"INSERT INTO {table} ({cols_raw}) VALUES"
            rest = sql[m.end():]
            tail = (
                f" ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {update_set}"
                if update_set
                else f" ON CONFLICT ({', '.join(conflict)}) DO NOTHING"
            )
            return head + rest + tail
        m = cls._RE_OR_IGNORE.search(sql)
        if m:
            table = m.group(1)
            cols_raw = m.group(2)
            head = sql[: m.start()] + f"INSERT INTO {table} ({cols_raw}) VALUES"
            rest = sql[m.end():]
            return head + rest + " ON CONFLICT DO NOTHING"
        return sql

    @classmethod
    def _adapt_sql(cls, sql: str) -> str:
        """SQLite-→-Postgres 语法适配。
        · INSERT OR REPLACE / IGNORE → ON CONFLICT
        · `?`     → `%s`              （positional 占位符）
        · `:name` → `%(name)s`        （named 占位符 → psycopg2 pyformat）
        """
        sql = cls._rewrite_upsert(sql)
        sql = cls._RE_NAMED_PARAM.sub(r"%(\1)s", sql)
        return sql.replace("?", "%s")

    def execute(self, sql, params=None):
        # 実行**前**の状態で判定：既に事務中（＝直前に書き込みがある）なら触らない。
        finish = (self._raw.get_transaction_status() == _TXN_IDLE
                  and self._is_readonly_sql(sql))
        cur = self._raw.cursor()
        cur.execute(self._adapt_sql(sql), params or ())
        if finish:
            # client-side cursor は結果を取得済みなので commit 後も fetchall 可能
            self._raw.commit()
        return cur

    def executemany(self, sql, params_seq):
        cur = self._raw.cursor()
        cur.executemany(self._adapt_sql(sql), list(params_seq))
        return cur

    def executescript(self, script: str):
        cur = self._raw.cursor()
        cur.execute(script)  # Postgres 接受多语句
        return cur

    # pandas / 其他库直接调 conn.cursor() 时透传到底层 psycopg2
    def cursor(self, *args, **kwargs):
        """暴露 cursor 接口供 pandas read_sql_query 等使用（只読は自動で事務を閉じる）。"""
        return _AutoFinishCursor(self._raw.cursor(*args, **kwargs), self._raw)

    @property
    def con(self):
        """部分 pandas 内部会摸 .con；返回自身保证 cursor() 仍能调到。"""
        return self

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


def get_readonly_connection():
    """只读页面统一入口(cms_reader · Boss 2026-07-27 存量收权指示)。

    - PG 模式且设置 READONLY_DATABASE_URL → 以 cms_reader 连接(仅 SELECT,
      DB 层强制;页面若意外触发写入会显式报 permission denied,不会静默)
    - 其余情况(本地 SQLite / 未配置)→ 回退 get_connection(),行为与从前一致
    - 不跑 schema bootstrap(只读角色无 DDL 权限,也不需要)
    """
    url = os.environ.get("READONLY_DATABASE_URL", "")
    if not (_is_postgres() and url.startswith(("postgresql://", "postgres://"))):
        return get_connection()
    import psycopg2
    from psycopg2.extras import DictCursor
    raw = psycopg2.connect(url, cursor_factory=DictCursor)
    raw.set_session(autocommit=False)
    return _PostgresAdapter(raw)


def get_connection():
    """统一入口。根据 DATABASE_URL 自动选 SQLite or Postgres。

    返回对象兼容 SQLite Connection 接口：
        conn.execute(sql, params).fetchall()
        conn.executemany(sql, params_seq)
        conn.commit() / conn.close()
        row["col"] 索引访问

    注：不用 @st.cache_resource —— 连接不能跨线程共享。
    """
    if _is_postgres():
        return _get_postgres_connection()
    return _get_sqlite_connection()
