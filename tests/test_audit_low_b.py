"""監査 低危 B 組（L4/L5/L6/L7/L13/L14/L16/L18/L25）の回帰テスト。DB・網なし。"""
from __future__ import annotations

import pathlib
import sqlite3
from unittest import mock

import pytest
from streamlit.testing.v1 import AppTest

PAGES = pathlib.Path(__file__).resolve().parents[1] / "pages"


def _page(prefix: str) -> pathlib.Path:
    return next(PAGES.glob(f"{prefix}_*.py"))


# ── L18: 入荷困難商品の履歴 item_id ──────────────────────────────

class _PgLikeConn:
    """psycopg2 と同じく cursor.lastrowid が 0（OID なし表）を返す SQLite 接続。"""

    class _Cur:
        def __init__(self, cur):
            self._cur = cur
            self.lastrowid = 0

        def __getattr__(self, name):
            return getattr(self._cur, name)

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        return self._Cur(self._raw.execute(sql, params))

    def __getattr__(self, name):
        return getattr(self._raw, name)


def _diff_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE difficult_items (id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT NOT NULL,
            reason TEXT NOT NULL, note TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE difficult_items_history (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER,
            item_key TEXT, reason TEXT, note TEXT, action TEXT, action_at TEXT);
        INSERT INTO difficult_items (item_key, reason) VALUES ('既存', '廃番');   -- 新規は id=2
    """)
    return c


@pytest.mark.parametrize("pg_like", [True, False], ids=["pg-lastrowid-0", "sqlite"])
def test_difficult_items_history_gets_real_id_audit_l18(pg_like):
    raw = _diff_db()
    conn = _PgLikeConn(raw) if pg_like else raw
    with mock.patch("shared.auth.require_password", lambda: None), \
            mock.patch("shared.db.get_connection", lambda: conn):
        at = AppTest.from_file(str(_page("12")), default_timeout=60)
        at.run()
        at.text_input[0].input("4901111310490")
        at.text_input[1].input("在庫無し")
        at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    hist = raw.execute("SELECT item_id, action FROM difficult_items_history").fetchall()
    assert [tuple(r) for r in hist] == [(2, "insert")]



def _page_func(prefix: str, name: str, **glb):
    """ページ内の関数定義だけを取り出して実行可能にする（ページ全体は DB が要るので回さない）。"""
    import ast
    tree = ast.parse(_page(prefix).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = dict(glb)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), name, "exec"), ns)
    return ns[name]


# ── L16: SKU 詳細の原価が NULL →「¥nan」 ─────────────────────────

def test_sku_detail_cost_blank_shows_dash_audit_l16():
    import pandas as pd
    src = _page("02").read_text(encoding="utf-8")
    for col in ("cost_estimate", "average_cost", "last_purchase_cost"):
        assert f"_yen(row['{col}'])" in src, f"{col} が _yen を通っていない（NaN or 0 は NaN →「¥nan」）"
    yen = _page_func("02", "_yen", pd=pd)
    assert yen(float("nan")) == "—" and yen(None) == "—" and yen(pd.NA) == "—"
    assert yen(1234.5) == "¥1,234.50" and yen(0) == "¥0.00"


# ── L13: 網紅名册の空欄が 'none' / 'None' で保存される ────────────────

def test_roster_blank_cells_saved_as_empty_audit_l13():
    import json
    import pandas as pd
    import shared.editor_state as ES
    real = ES.lang_stable_editor

    def editor(data, *, key, **kw):
        out = real(data, key=key, **kw)
        if key == "roster_editor":       # 動的行で名前と時給だけ入れた行（他は None）
            out = pd.concat([out, pd.DataFrame([{"name": "Ann", "wage": 1.5, "payee": None, "kw": None},
                                                  {"name": None, "wage": None, "payee": None, "kw": None}])],
                            ignore_index=True)
        return out

    written = []
    with mock.patch("shared.auth.require_password", lambda: None), \
            mock.patch.object(ES, "lang_stable_editor", editor), \
            mock.patch.object(pathlib.Path, "mkdir", lambda *a, **k: None), \
            mock.patch.object(pathlib.Path, "write_text", lambda self, s, **k: written.append((self.name, s))):
        at = AppTest.from_file(str(_page("33")), default_timeout=60)
        at.run()
        at.button(key="save_roster").click().run()
    assert not at.exception, [e.value for e in at.exception]
    saved = json.loads(next(s for n, s in written if n == "influencer_roster.json"))
    ann = next(r for r in saved if r["name"] == "Ann")
    assert ann["payee"] == "" and ann["keywords"] == []
    assert "None" not in [r["name"] for r in saved]          # 名前が空の行は保存しない
