"""page24 処分リストの読込: 空セル・数量の不正を黙って通さない（監査 M18 2026-11-17）。関数だけ AST で取り出して試す。"""
import ast
import io
import pathlib

import pandas as pd
import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1] / "pages" / "24_♻️_不良品処分CSV.py").read_text(encoding="utf-8")
fn = next(n for n in ast.parse(SRC).body if isinstance(n, ast.FunctionDef) and n.name == "_load_disposal_list")
ns = {"pd": pd, "io": io, "t": lambda s: s}
exec(compile(ast.Module([fn], []), "page24", "exec"), ns)
load = ns["_load_disposal_list"]


def _xlsx(rows):
    buf = io.BytesIO()
    pd.DataFrame(rows, columns=["アイテム", "表示名", "保管棚番号", "ステータス", "手持"]).to_excel(
        buf, index=False, sheet_name="処分リスト")
    return buf.getvalue()


def test_blank_cells_and_default_status():
    df = load(_xlsx([["4900000000011", None, "A-1", None, "2"]]))
    r = df.iloc[0]
    assert (r["display_name"], r["status"], r["qty"]) == ("", "不良品", 2)


@pytest.mark.parametrize("q", ["2個", "2.5", "0", None])
def test_bad_qty_rejected(q):
    with pytest.raises(ValueError):
        load(_xlsx([["4900000000011", "名", "A-1", "不良品", q]]))
