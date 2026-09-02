"""schema.postgres.sql の 1 文ずつ実行（2026-09-02 の障害の再発防止）。

起きたこと: `coupang_product_info` が元川の PG に作られず、page41 が
`relation "coupang_product_info" does not exist` で落ちた。
原因は 2 つあって、どちらも**無言で効く**タイプだった:
  1. compose の volumes に deploy/ が無く、容器はイメージ内の**古い** schema を見ていた
     （git pull しても反映されない。redeploy しないと入らない）
  2. schema 全体を 1 回の execute で流していたので、1 文でも落ちると全部 rollback、
     しかも外側の except に握り潰されて「テーブルが 1 つも増えない」だけになる
"""
from __future__ import annotations

from pathlib import Path

from shared.db import _split_sql

SCHEMA = Path(__file__).resolve().parents[1] / "deploy" / "windows" / "schema.postgres.sql"
TEXT = SCHEMA.read_text("utf-8")
STMTS = _split_sql(TEXT)


def test_文に割れている():
    assert len(STMTS) > 200
    assert all(s.rstrip().endswith(";") for s in STMTS), "; で終わらない文がある"
    assert not any(s.strip().startswith("--") for s in STMTS), "コメントだけの文が混じった"


def test_関数本体が無いこと():
    """`$$ ... $$` が入ると単純な `;` 分割では切れない。入れるなら分割方法を見直すこと。"""
    assert "$$" not in TEXT, "関数本体が追加された。_split_sql の分割方法を見直す"


def test_ECMSとCoupangのテーブルが含まれる():
    joined = "\n".join(STMTS)
    for table in ("ecms_shipment", "ecms_tracking_event",
                  "coupang_shipment_queue", "coupang_product_info"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in joined, table


def test_compose_が_schema_を_bind_mount_している():
    """ここを外すと git pull しても DDL が容器に入らない（2026-09-02 の原因その 1）。"""
    compose = (SCHEMA.parent / "docker-compose.yml").read_text("utf-8")
    assert "deploy/windows/schema.postgres.sql:/app/deploy/windows/schema.postgres.sql" in compose


def test_行末コメントは文の中に残る():
    src = "CREATE TABLE t (\n  a TEXT,  -- 説明\n  b TEXT\n);\nCREATE INDEX i ON t(a);"
    out = _split_sql(src)
    assert len(out) == 2
    assert "-- 説明" in out[0]        # 行末コメントは PG が読み飛ばす。切ってはいけない


def test_行頭コメントだけの塊は捨てる():
    src = "-- =====\n-- 見出し\n-- =====\nCREATE TABLE t (a TEXT);"
    assert _split_sql(src) == ["CREATE TABLE t (a TEXT);"]
