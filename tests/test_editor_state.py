"""shared/editor_state.lang_stable_editor — 言語を切り替えても data_editor の未保存の編集を失わない。

Streamlit 1.57 の data_editor は data と column_config を element id に入れる（key は主キーではない）。表頭・表示値が
言語で変わる = 切替で別の表になり、フロントの編集は消える。AppTest で「フロントが送る編集状態」を再現して確かめる。
"""
from __future__ import annotations

import json

from streamlit.proto.WidgetStates_pb2 import WidgetStates
from streamlit.testing.v1 import AppTest


def _app():
    import pandas as pd
    import streamlit as st

    from shared import i18n
    from shared.editor_state import lang_stable_editor

    i18n._note_page()                  # 各ページ冒頭の lang_selector() が呼ぶ（別ページから入ったら累計編集を捨てる）
    lang = st.session_state.get("lang", "zh")
    src = st.session_state.get("src", 0)
    df = pd.DataFrame({"name": [f"n{src}{i}" for i in range(3)], "qty": [1.0, 2.0, 3.0],
                       "note": [f"{lang}-{i}" for i in range(3)],      # disabled・言語で変わる表示値
                       "memo": [f"{lang}m{i}" for i in range(3)],      # column_config 側で disabled
                       "day": [None, None, None]})                      # 空の列 × DateColumn（編集は ISO 文字列で返る）
    out = lang_stable_editor(
        df, key="ed", disabled=["note"], num_rows=st.session_state.get("rows", "fixed"),
        reset=st.session_state.get("reset"),
        column_config={"name": st.column_config.TextColumn("名前" if lang == "ja" else "名字"),
                       "memo": st.column_config.TextColumn("memo", disabled=True),
                       "day": st.column_config.DateColumn("日付" if lang == "ja" else "日期")})
    st.session_state["out"] = out.to_dict("list")


def _filter_app():
    """page30 在途明細の形：line_id（保存キー・言語で変わらない）+ 言語で変わる表示列 + 空の編集列。"""
    import pandas as pd
    import streamlit as st

    from shared.editor_state import lang_stable_editor

    lang = st.session_state.get("lang", "zh")
    rows = {"A": ["A-1", "A-2", "A-3"], "B": ["B-1", "B-2", "B-3"]}[st.session_state.get("flt", "A")]
    df = pd.DataFrame({"line_id": rows, "vendor": [f"{lang}:{r}" for r in rows], "eta_note": ["", "", ""]})
    kw = dict(hide_index=True, disabled=["line_id", "vendor"],
              column_config={"eta_note": st.column_config.TextColumn("备注" if lang == "zh" else "備考")})
    if st.session_state.get("show_id"):
        kw["reset"] = tuple(rows)                          # 表示する識別列は呼び出し側が reset で渡す
    else:
        kw["column_order"] = ["vendor", "eta_note"]        # 表示しない識別列は既定 token に入る
    out = lang_stable_editor(df, key="ed", **kw)
    st.session_state["out"] = out[["line_id", "eta_note"]].values.tolist()


class _Front:
    """ブラウザ役：今表示している表（element id）の編集状態を毎回送る。表が別物になれば編集はまっさら。"""

    def __init__(self, app=_app, **state):
        self.at = AppTest.from_function(app)
        for k, v in state.items():
            self.at.session_state[k] = v
        self.at.run()
        self.eid, self.edits = self._eid(), None

    def _eid(self) -> str:
        ids = [d.proto.id for d in self.at.dataframe if d.proto.id.endswith("-ed")]
        assert len(ids) == 1, ids
        return ids[0]

    def run(self, **state) -> dict:
        for k, v in state.items():
            self.at.session_state[k] = v
        ws = WidgetStates()
        if self.edits:
            w = ws.widgets.add()
            w.id, w.string_value = self.eid, json.dumps(self.edits)
        self.at._run(ws)
        assert not self.at.exception, [e.value for e in self.at.exception]
        if self._eid() != self.eid:
            self.eid, self.edits = self._eid(), None
        return self.at.session_state["out"]

    def leave_and_return(self) -> dict:
        """別ページへ移って戻る：表は一度消える（ブラウザの編集も消える）。戻った run で lang_selector が前のページを見る。"""
        self.at.session_state["_cms_page"] = "another-page"
        self.edits = None
        return self.run()

    def _state(self) -> dict:
        self.edits = self.edits or {"edited_rows": {}, "added_rows": [], "deleted_rows": []}
        return self.edits

    def edit(self, row: int, col: str, value) -> dict:
        self._state()["edited_rows"].setdefault(str(row), {})[col] = value
        return self.run()

    def add(self, row: dict) -> dict:
        self._state()["added_rows"].append(row)
        return self.run()

    def delete(self, row: int) -> dict:
        self._state()["deleted_rows"].append(row)
        return self.run()


def test_same_language_reruns_keep_edits_and_data_is_stable():
    f = _Front()
    eid = f.eid
    assert f.edit(0, "name", "X")["name"] == ["X", "n01", "n02"]
    for _ in range(3):
        assert f.run()["name"] == ["X", "n01", "n02"]
    assert f.eid == eid                    # 同じ言語では毎回同じ data を渡す（id が変わると編集が消える）


def test_language_switch_keeps_edits_and_accumulates():
    f = _Front()
    f.edit(0, "name", "X")
    f.edit(2, "qty", 9.0)
    out = f.run(lang="ja")
    assert out["name"] == ["X", "n01", "n02"] and out["qty"] == [1.0, 2.0, 9.0]
    assert out["note"] == ["ja-0", "ja-1", "ja-2"] and out["memo"] == ["jam0", "jam1", "jam2"]  # 表示列は新しい言語
    assert f.run()["name"] == ["X", "n01", "n02"]                   # ja のまま rerun
    f.edit(1, "name", "Y")
    out = f.run(lang="zh")
    assert out["name"] == ["X", "Y", "n02"] and out["qty"] == [1.0, 2.0, 9.0]
    assert out["note"] == ["zh-0", "zh-1", "zh-2"]
    assert f.run()["name"] == ["X", "Y", "n02"]
    f.edit(0, "name", "Z")                                         # 重放した値の上をさらに直す
    assert f.run(lang="ja")["name"] == ["Z", "Y", "n02"]


def test_data_source_change_clears_accumulated_edits():
    f = _Front()
    f.edit(0, "name", "X")
    assert f.run(lang="ja")["name"][0] == "X"
    assert f.run(src=1)["name"] == ["n10", "n11", "n12"]             # 再処理・保存後の読み直し
    assert f.run(lang="zh")["name"] == ["n10", "n11", "n12"]          # 前のデータの編集を持ち込まない


def test_dynamic_rows_survive_language_switch():
    f = _Front(rows="dynamic")
    f.delete(1)
    f.add({"name": "NEW", "qty": 7.0})
    f.edit(0, "name", "E")
    out = f.run(lang="ja")
    assert out["name"] == ["E", "n02", "NEW"] and out["qty"] == [1.0, 3.0, 7.0]
    assert out["note"][:2] == ["ja-0", "ja-2"]
    f.delete(2)                                                    # 足した行を消す
    f.add({"name": "NEW2"})
    out = f.run(lang="zh")
    assert out["name"] == ["E", "n02", "NEW2"] and out["note"][:2] == ["zh-0", "zh-2"]
    assert f.run()["name"] == ["E", "n02", "NEW2"]


def test_date_typed_into_empty_column_survives_switch():
    """空の列に DateColumn で入れた日付は文字列で返る → そのまま重ねると「date 列に文字列」で data_editor が例外。"""
    import datetime as dt
    f = _Front(rows="dynamic")
    f.edit(0, "day", "2026-11-01")
    f.add({"name": "NEW", "day": "2026-12-24"})
    out = f.run(lang="ja")
    assert out["day"] == [dt.date(2026, 11, 1), None, None, dt.date(2026, 12, 24)]
    assert f.run(lang="zh")["day"][0] == dt.date(2026, 11, 1)


def test_filter_to_same_size_rows_does_not_carry_edits_to_other_rows():
    """page30 在途明細：言語を切り替えた後、仕入先の絞り込みを A → 行数が同じで編集列も空の B に変える。
    A の備考が位置で B の行に乗ると、保存で B の line_id に書かれる（原生 data_editor は表が変われば編集を捨てる）。"""
    for state in ({}, {"show_id": True}):              # line_id 非表示（既定 token）/ 表示 + reset
        f = _Front(_filter_app, **state)
        f.edit(1, "eta_note", "ETA 11/20")
        assert f.run(lang="ja")[1] == ["A-2", "ETA 11/20"], state
        assert f.run(flt="B") == [["B-1", ""], ["B-2", ""], ["B-3", ""]], state


def test_reset_adds_to_default_token():
    """reset は識別値を足すだけ。同じ reset でも編集列の中身が変われば（保存後の読み直し）古い編集を重ねない。"""
    f = _Front(reset="same-rows")
    f.edit(0, "name", "X")
    assert f.run(lang="ja")["name"][0] == "X"
    assert f.run(src=1)["name"] == ["n10", "n11", "n12"]


def test_leaving_page_drops_kept_edits_like_native_editor():
    """言語を切り替えた後に別ページへ行って戻る → 原生 data_editor と同じく未保存の編集は消える。"""
    f = _Front()
    f.edit(0, "name", "X")
    assert f.run(lang="ja")["name"][0] == "X"
    assert f.leave_and_return()["name"] == ["n00", "n01", "n02"]
    f.edit(1, "name", "Y")                                  # 戻った後の編集・切替は今まで通り保つ
    assert f.run(lang="zh")["name"] == ["n00", "Y", "n02"]
    assert f.run()["name"] == ["n00", "Y", "n02"]


def test_lang_selector_notes_the_page():
    import inspect

    from shared import i18n
    assert "_note_page()" in inspect.getsource(i18n.lang_selector)


def test_pages_pass_row_identity_as_reset():
    """行の集合が変わっても行数が同じことがある表は、言語で変わらない行の識別値を reset に渡す。"""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "pages"
    want = {("30_", "intransit_editor"): "line_id", ("30_", "wl_editor"): "vendor_id",
            ("32_", "bin_cat_editor"): "bin_number", ("32_", "wl_editor"): "vendor_id",
            ("12_", "diff_items_table"): "id", ("29_", "new_dept"): "shop",
            ("15_", "page15_editor"): "型番", ("15_", "page15_weight_fix"): "型番",
            ("21_", "gen_editor_"): "jan", ("21_", "rv_sku_"): "jan"}
    seen = set()
    for page in root.glob("*.py"):
        for node in ast.walk(ast.parse(page.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "lang_stable_editor"):
                continue
            kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
            for (prefix, key), ident in want.items():
                if page.name.startswith(prefix) and key in kw.get("key", ""):
                    seen.add((prefix, key))
                    assert ident in kw.get("reset", ""), (page.name, key, kw.get("reset"))
    assert seen == set(want)
