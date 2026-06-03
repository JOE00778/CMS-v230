"""测试 Exporter 基类。"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

pytest.skip(
    "data_warehouse.db 模块已移除；exports/ 子系统零 runtime 引用，去留待定"
    "（见 docs/20-cms-cleanup-report.md）。",
    allow_module_level=True,
)

from data_warehouse.db.migrations import init_db
from data_warehouse.exports.base import Exporter


class DemoExporter(Exporter):
    exporter_name = "demo"
    headers = ["Internal ID", "Standard Cost"]
    file_prefix = "cost_update"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return init_db(tmp_path / "test.db")


def test_writes_csv_with_headers(conn, tmp_path):
    rows = [
        {"Internal ID": "1234", "Standard Cost": 294},
        {"Internal ID": "1235", "Standard Cost": 512},
    ]
    file_path, export_id = DemoExporter().export(rows, tmp_path / "outputs", conn)
    assert file_path.exists()
    with file_path.open(encoding="utf-8", newline="") as f:
        rows_out = list(csv.reader(f))
    assert rows_out[0] == ["Internal ID", "Standard Cost"]
    assert rows_out[1] == ["1234", "294"]
    assert rows_out[2] == ["1235", "512"]


def test_export_audit_recorded(conn, tmp_path):
    rows = [{"Internal ID": "1", "Standard Cost": 100}]
    file_path, export_id = DemoExporter().export(rows, tmp_path / "outputs", conn)
    audit = conn.execute(
        "SELECT * FROM _export_runs WHERE export_id=?", (export_id,)
    ).fetchone()
    assert audit["exporter"] == "demo"
    assert audit["row_count"] == 1
    assert audit["output_file"] == str(file_path)


def test_extra_keys_in_rows_ignored(conn, tmp_path):
    """rows 中多余的字段不应阻塞 CSV 输出（只输出 headers 中列）。"""
    rows = [
        {"Internal ID": "1", "Standard Cost": 100, "Notes": "ignored", "Foo": "bar"},
    ]
    file_path, _ = DemoExporter().export(rows, tmp_path / "outputs", conn)
    with file_path.open(encoding="utf-8", newline="") as f:
        rows_out = list(csv.reader(f))
    assert rows_out[0] == ["Internal ID", "Standard Cost"]
    assert rows_out[1] == ["1", "100"]


def test_creates_output_dir_if_missing(conn, tmp_path):
    deep = tmp_path / "deep" / "nested" / "outputs"
    file_path, _ = DemoExporter().export(
        [{"Internal ID": "1", "Standard Cost": 100}], deep, conn
    )
    assert deep.exists()
    assert file_path.exists()


def test_empty_rows_writes_header_only(conn, tmp_path):
    file_path, _ = DemoExporter().export([], tmp_path / "outputs", conn)
    with file_path.open(encoding="utf-8", newline="") as f:
        rows_out = list(csv.reader(f))
    assert len(rows_out) == 1
    assert rows_out[0] == ["Internal ID", "Standard Cost"]
