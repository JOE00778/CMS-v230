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


# ── L6: キーワード検索が正則扱い（半角括弧で ArrowInvalid） ─────────────

@pytest.mark.parametrize("prefix", ["34", "20"])
def test_keyword_contains_is_literal_audit_l6(prefix):
    import re
    src = _page(prefix).read_text(encoding="utf-8")
    calls = re.findall(r"\.str\.contains\(([^()]*(?:\([^()]*\)[^()]*)*)\)", src)
    assert calls
    assert all("regex=False" in c for c in calls), [c for c in calls if "regex=False" not in c]


# ── L7: 定義原価 0 が混じると波動率 0 →「无变化」で隠れる ────────────

def test_cost_sql_excludes_zero_audit_l7():
    import ast
    from tests.nstdb import new_conn
    tree = ast.parse(_page("20").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "_SQL_COST")
    sql = ast.literal_eval(node.value)
    c = new_conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS nst.cost_history (internal_id INTEGER, effective_date TEXT, cost_estimate REAL);
        INSERT INTO nst.cost_history VALUES (1, '2026-01-01', 0), (1, '2026-02-01', 980), (1, '2026-03-01', 1000);
    """)
    assert [r["std_cost_new"] for r in c.execute(sql)] == [980, 1000]


# ── L5: 報価 CSV が cp932（日本語 Excel の「CSV (コンマ区切り)」）だと解析失敗 ──

@pytest.mark.parametrize("enc", ["cp932", "utf-8-sig", "utf-8"])
def test_quote_csv_encodings_audit_l5(enc):
    from shared import sourcing as sc
    data = "JAN,単価,仕入先\n4901234567894,120,若竹園\n".encode(enc)
    df = sc.read_quote_upload("見積.CSV", data)
    assert list(df.columns) == ["JAN", "単価", "仕入先"]
    assert df.iloc[0].tolist() == ["4901234567894", "120", "若竹園"]


# ── L4: 供货商列なし/全空の報価表が「缺少必要列」で弾かれる ───────────

@pytest.mark.parametrize("raw", [
    {"JAN": ["4901234567894"], "単価": ["120"]},                        # 列なし
    {"JAN": ["4901234567894"], "単価": ["120"], "仕入先": [""]},          # CSV の空串
    {"JAN": ["4901234567894"], "単価": ["120"], "仕入先": [None]},
])
def test_quote_upload_without_supplier_asks_for_one_audit_l4(raw):
    import pandas as pd
    from shared import sourcing as sc
    norm, _ = sc.normalize_upload(pd.DataFrame(raw))
    assert sc.needs_supplier(norm)
    norm["supplier_name"] = "若竹園"
    assert not sc.needs_supplier(norm)


def test_quote_upload_page_only_requires_jan_and_price_audit_l4():
    src = _page("34").read_text(encoding="utf-8")
    assert '"supplier_name" not in _miss' not in src, "外側の条件が供货商列を必須にしている → 統一指派に入れない"
    assert "sc.needs_supplier(_norm)" in src and "sc.read_quote_upload(" in src
