"""数据库初始化与迁移。

调用 `init_db(db_path)` 即可幂等建表。schema 写在同目录的 `schema.sql` 中。

**稳定性策略**：
- 不用 `executescript()`（一句失败会导致整体 abort,所有 page 挂掉）
- 改用逐句 `execute()` + try/except,单条 SQL 失败仅 log 不阻塞其他表
- 这样 schema.sql 即使有局部 bug,也只影响那一张表而非整个 app
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 21
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# 增量 ALTER（旧 db 已建过表,补充缺失列）
# 格式: (table, column_def_in_ALTER) — 如果列已存在会被 try/except 吞掉
ALTERS: list[tuple[str, str]] = [
    ("sales_line", "maker TEXT"),
    # Phase 4 v2 表字段扩展（旧库 ALTER 加列，新库 CREATE 已含）
    ("item_inventory_snapshot_v2", "item_code TEXT"),
    ("item_inventory_snapshot_v2", "internal_id TEXT"),
    ("item_inventory_snapshot_v2", "display_name TEXT"),
    ("item_inventory_snapshot_v2", "total_amount REAL"),
    ("item_inventory_snapshot_v2", "handling_status TEXT"),
    ("item_inventory_snapshot_v2", "status TEXT"),
    ("item_inventory_snapshot_v2", "owner TEXT"),
    ("item_inventory_snapshot_v2", "department TEXT"),
    ("item_inventory_snapshot_v2", "qty_on_order REAL"),
    ("item_inventory_snapshot_v2", "qty_waiting REAL"),
    ("item_inventory_snapshot_v2", "qty_in_transit REAL"),
    ("item_monthly_turnover", "rank TEXT"),
]

# 废弃表清单 — 启动时 DROP TABLE IF EXISTS（仅一次性影响）
# v2 模型上线后这些旧表无引用，统一退场
DEPRECATED_TABLES: list[str] = [
    "store_profit_lines",       # 无 SELECT 引用
    "store_profit_daily_lines", # 无 SELECT 引用
    "sales",                    # 空表，sales_line 替代
]

# Phase 4 v2 表 schema 重大变更（UNIQUE 约束等结构変化）
# 過去 shop_sales を毎回 DROP していたが、本番運用に入ったので空にする。
# 新たに schema 大改造する時のみ一時的に追加 (commit 後 1 週で外す運用)。
PHASE4_REBUILD_TABLES: list[str] = []

# Phase 4 桥接 VIEW · 旧表名 → 透传 v2 真表
# (legacy_table_name, target_view) - target_view 在 schema.sql 末尾定义
# 残るのは在庫スナップショット系 2 本のみ（item 主档・売上系は nst.* へ移行済）
PHASE4_LEGACY_VIEWS: list[tuple[str, str]] = [
    ("inventory_snapshot",     "v_inventory_snapshot"),
    ("nst_inventory_snapshot", "v_nst_inventory_snapshot"),
    # 2026-08-21 削除: sales_line / nst_store_sales / nst_item_summary /
    #   item_master_netsuite / item_master の 5 本。
    #   透過先の item_v2 / shop_sales を廃止したため橋も不要になった。
    #   （schema.sql から定義を消さないと、実表を DROP しても
    #     cms_streamlit 再起動時の init_db() で空表として復活する）
]

# 启动时收集 schema 错误（不阻塞 init,但供调试）
SCHEMA_ERRORS: list[tuple[str, str]] = []


def _split_sql(sql: str) -> list[str]:
    """按 ; 分割 SQL 语句。schema.sql 不含 trigger/begin-end 块,所以朴素分割即可。"""
    # 去掉行内注释 (-- 开头到行尾) 但保留字符串字面量
    cleaned = []
    for line in sql.splitlines():
        # 简单处理: 不在引号内的 -- 后面的内容删掉
        in_str = None
        out = []
        i = 0
        while i < len(line):
            c = line[i]
            if in_str:
                out.append(c)
                if c == in_str:
                    in_str = None
            elif c in ("'", '"'):
                in_str = c
                out.append(c)
            elif c == "-" and i + 1 < len(line) and line[i + 1] == "-":
                break
            else:
                out.append(c)
            i += 1
        cleaned.append("".join(out))
    text = "\n".join(cleaned)

    stmts = []
    buf = []
    in_str = None
    for ch in text:
        buf.append(ch)
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == ";":
            stmts.append("".join(buf).strip())
            buf = []
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return [s for s in stmts if s]


def init_db(db_path: Path) -> sqlite3.Connection:
    """初始化数据库（幂等 + 容错）。

    - 自动创建父目录
    - 逐句执行 schema.sql,单条失败仅记录到 SCHEMA_ERRORS,不影响其他语句
    - 应用 ALTERS 增量补列（已存在则忽略）
    - 写入 schema 版本号
    - 返回打开的 connection（调用方负责关闭）
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False：Streamlit 跨 rerun 复用 cache_resource 连接，
    # 不同 session/rerun 可能在不同 thread。SQLite 的串行写入特性 + 我们的
    # commit-per-write 模式保证安全。
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Phase 4 v2 表 schema 重建（必须在 CREATE TABLE 之前 DROP，让 schema.sql 重建）
    for tbl in PHASE4_REBUILD_TABLES:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        except sqlite3.Error:
            pass

    SCHEMA_ERRORS.clear()
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    for stmt in _split_sql(sql):
        try:
            conn.execute(stmt)
        except sqlite3.Error as e:
            # 摘要记录: 取首个 80 字符 + 错误类型
            head = re.sub(r"\s+", " ", stmt)[:80]
            SCHEMA_ERRORS.append((head, str(e)))

    # 增量列补丁（旧 db 已建过 sales_line 等表,通过 ALTER 补列）
    for table, col_def in ALTERS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # 列已存在 / 表不存在 都跳过

    # 废弃表退场（DROP TABLE IF EXISTS，幂等）
    for tbl in DEPRECATED_TABLES:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        except sqlite3.Error:
            pass

    # Phase 4 · 旧表名 → VIEW 桥接（让 page SQL 不用改）
    # 1. DROP 旧表（之前是真表，现在不要了）
    # 2. CREATE VIEW 同名 → 指向 v_xxx 提供的 v2 数据
    for legacy, target_view in PHASE4_LEGACY_VIEWS:
        try:
            conn.execute(f"DROP VIEW IF EXISTS {legacy}")
            conn.execute(f"DROP TABLE IF EXISTS {legacy}")
            conn.execute(f"CREATE VIEW {legacy} AS SELECT * FROM {target_view}")
        except sqlite3.Error as e:
            SCHEMA_ERRORS.append((f"legacy_view {legacy}", str(e)))

    # 写入版本号（幂等）
    try:
        conn.execute(
            "INSERT OR IGNORE INTO _schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()),
        )
    except sqlite3.Error:
        pass  # _schema_version 表本身可能没建成,不阻塞
    conn.commit()
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    """返回当前 schema 版本（无记录返回 0）。"""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM _schema_version"
    ).fetchone()
    return row["v"] or 0


def run(db_path: str = "data_warehouse/warehouse.db") -> sqlite3.Connection:
    """幂等初始化数据库，返回连接。用于命令行或测试。"""
    return init_db(Path(db_path))
