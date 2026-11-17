"""改廃監視: 取得失敗（error）で前回の状態を消さない → active→error→discontinued でも停産を告警（監査 M21 2026-11-17）。"""
import ast
import pathlib

SRC = (pathlib.Path(__file__).resolve().parents[1] / "deploy/n8n/stock_monitor/scripts/check_products.py").read_text(encoding="utf-8")
ns: dict = {}
mod = ast.parse(SRC)
exec(compile(ast.Module([n for n in mod.body if isinstance(n, ast.FunctionDef)
                         and n.name in ("keep_last_known", "diff_status")], []), "cp", "exec"), ns)


def _st(status):
    return {"J": {"status": status, "name": "n", "maker": "m", "matched_name": None}}


def test_error_keeps_previous_and_later_discontinue_alerts():
    s1 = _st("active")
    s2, n = ns["keep_last_known"](s1, _st("error"))
    assert n == 1 and s2["J"]["status"] == "active"
    s3, _ = ns["keep_last_known"](s2, _st("discontinued"))
    assert [m["jan"] for m in ns["diff_status"](s2, s3)["newly_discontinued"]] == ["J"]
