"""page47（🦓 斑马・NST 整合检查）を Streamlit AppTest で無頭実行 — NST / 斑马 / 画像検索はモック。

page15 の「🔎 SKUID 空チェック」tab から独立（Boss 2026-11-12）。fixture page15_offline は外への通信を止める共用品。
"""
from __future__ import annotations

import pathlib
from unittest import mock

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from shared import nst_choices as NC

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "47_🦓_斑马NST整合检查.py"


@pytest.fixture(autouse=True)
def _ui_ja():
    """文言は日文原文で検査する。session_state["lang"] ではなく get_lang を固定するのは、AppTest が radio / selectbox の
    format_func（= t）をスクリプトの外で呼び、そこでは t() が既定 zh になるため（中文は test_page47_zh_ja_ui_text）。"""
    with mock.patch("shared.i18n.get_lang", return_value="ja"):
        yield


def test_page47_renders_and_nav_group():
    from shared import i18n
    st.cache_data.clear()
    with mock.patch("shared.db.get_connection", mock.MagicMock()):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [b.label for b in at.button]
    assert any("NST で SKUID が空の品をチェック" in x for x in labels) and any("登録内容をチェック" in x for x in labels)
    groups = dict(i18n._NAV_GROUPS)
    assert ("pages/47_🦓_斑马NST整合检查.py", "🦓 斑马・NST 整合检查") in groups["🦓 斑马・NST"]
    assert not any("47_" in path for path, _ in groups["🛠️ 工具"])


def test_page47_skuid_check_and_csv_20261112(page15_offline):
    """🔎 SKUID 空チェック: 既定条件で絞り（取扱中止・非アクティブは外す）→ 斑马照会 → 有りは CSV・重複は保留・無しは一覧。"""
    from shared import banma_client, nst_suiteql
    from shared import banma_register as BMR
    st.cache_data.clear()
    blank = [{"id": "1", "code": "X1", "jan": "4900000000011", "name": "名1", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "F", "item_rank": "NEW"},
             {"id": "2", "code": "X2", "jan": "4900000000028", "name": "名2", "dept": "輸出事業", "handling": "取扱中止",
              "inactive": "F", "item_rank": None},
             {"id": "3", "code": "X3", "jan": "4900000000035", "name": "名3", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "T", "item_rank": None},
             {"id": "4", "code": "X4", "jan": "4900000000042", "name": "名4", "dept": "輸出（中国）", "handling": "取扱中",
              "inactive": "F", "item_rank": None},
             {"id": "5", "code": "X5", "jan": "4900000000059", "name": "名5", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "F", "item_rank": None}]
    queries = []

    def fake_all(q):
        queries.append(q)
        return blank if "IS NULL" in q else [{"code": "OLD", "sku_id": "105"}]
    asked = []
    with mock.patch("shared.db.get_connection", mock.MagicMock()), \
            mock.patch.object(NC, "load_choices", return_value=NC.Choices(suppliers=(), middle_by_large={})), \
            mock.patch.object(nst_suiteql, "suiteql_all", side_effect=fake_all), \
            mock.patch.object(banma_client.BanmaClient, "from_env", return_value=object()), \
            mock.patch.object(banma_client, "ensure_token"), \
            mock.patch.object(BMR, "find_skus", side_effect=lambda c, keys: asked.append(keys)
                              or {"X1": "101", "4900000000042": "104", "X5": "105"}):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button(key="p47s_run").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert asked == [["4900000000011", "4900000000042", "4900000000059", "X1", "X4", "X5"]]   # 条件外の X2・X3 は照会しない
    assert "TO_DATE(" in queries[0]                                    # 既定 = 直近 30 日
    info = " ".join(i.value for i in at.info)
    assert "条件に合う 3 件" in info and "斑马に有り 3 件" in info and "重複 1 件" in info
    assert any("斑马に無し" in h.value for h in at.subheader) is False   # 3 件とも斑马に有り
    dl = [d for d in at.get("download_button") if "SKUID 更新 CSV" in d.proto.label]
    assert dl and "（2 件）" in dl[0].proto.label                       # X5 は NST の OLD が同じ SKUID → 保留


def test_page47_skuid_register_misses_20261112(page15_offline):
    """斑马に無い品: 準備（台帳 → NST）→ 斑马登録 → 取れた SKUID が CSV に加わる。斑马へ渡す行の形も確認。"""
    from shared import banma_client, hs_ai, nst_suiteql
    from shared import banma_register as BMR
    from shared import item_register as REG
    from shared import skuid_fill as SF
    st.cache_data.clear()
    blank = [{"id": "1", "code": "X1", "jan": "4900000000011", "name": "NST の名前", "dept": "輸出事業",
              "handling": "取扱中", "inactive": "F", "item_rank": None, "cost": "120", "pkg_weight": "45"},
             {"id": "2", "code": "X2", "jan": "4900000000028", "name": "名2", "dept": "輸出事業",
              "handling": "取扱中", "inactive": "F", "item_rank": None, "cost": "300"},
             {"id": "3", "code": "X3", "jan": "4900000000035", "name": "名3", "dept": "輸出事業",
              "handling": "取扱中", "inactive": "F", "item_rank": None}]
    ledger = {"X2": {"payload": {"アイテム名": "台帳の名前", "商品原価": "377"},
                     "bm": {"海关编码": "950300", "英文名称": "Toy", "图片URL": "https://img/x2.jpg"}},
              "X3": {"payload": {"アイテム名": "台帳 HS なし"}, "bm": {"海关编码": ""}}}
    sent, ai = [], []

    def fake_register(client, rows, record=None, on_progress=None):
        sent.extend(rows)
        return [(r["型番"], {"status": "ok", "sku_id": f"9{i}", "message": ""}) for i, r in enumerate(rows)]
    with mock.patch("shared.db.get_connection", mock.MagicMock()), \
            mock.patch.object(NC, "load_choices", return_value=NC.Choices(suppliers=(), middle_by_large={})), \
            mock.patch.object(NC, "load_prefix_makers", return_value={}), \
            mock.patch.object(nst_suiteql, "suiteql_all", side_effect=lambda q: blank if "IS NULL" in q else []), \
            mock.patch.object(banma_client.BanmaClient, "from_env", return_value=object()), \
            mock.patch.object(banma_client, "ensure_token"), \
            mock.patch.object(BMR, "find_skus", return_value={}), \
            mock.patch.object(SF, "load_ledger", return_value=ledger), \
            mock.patch.object(REG, "fill_images", return_value={"待上图": 1}), \
            mock.patch.object(hs_ai, "is_configured", return_value=True), \
            mock.patch.object(REG, "cross_check_hs", side_effect=lambda rows, *a: ai.extend(REG.item_key(r) for r in rows)
                              or []), \
            mock.patch.object(BMR, "register_all", side_effect=fake_register):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button(key="p47s_run").click().run()
        assert not any("SKUID 更新 CSV" in d.proto.label for d in at.get("download_button"))
        at.button(key="p47s_prep_btn").click().run()
        at.button(key="p47s_reg_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]
    by = {r["型番"]: r for r in sent}
    assert by["X2"]["アイテム名"] == "台帳の名前" and by["X2"]["_hs"] == "950300" and by["X2"]["_image_url"] == "https://img/x2.jpg"
    assert by["X1"]["アイテム名"] == "NST の名前" and by["X1"]["商品原価"] == "120" and by["X1"]["パッケージ重量(g)"] == "45"
    assert sorted(ai) == ["X1", "X3"]                 # AI = NST の品 + 台帳でも HS 空の品（台帳の HS は触らない）
    assert any("1 g で登録" in w.value for w in at.warning)        # X2・X3 は重量なし
    dl = [d for d in at.get("download_button") if "SKUID 更新 CSV" in d.proto.label]
    assert dl and "（3 件）" in dl[0].proto.label


def test_page47_content_check_and_write_20261112(page15_offline):
    """🩺 斑马登録内容: SKUID ありの品 → 斑马 SKU の空（重量 1g・画像なし・報関なし）→ 提案を入れて「反映」→ 書込関数へ。"""
    from shared import banma_client, banma_fix, hs_ai, image_search, nst_suiteql
    from shared import banma_register as BMR
    st.cache_data.clear()
    items = [{"id": "1", "code": "X1", "jan": "4900000000011", "name": "ライオン クリニカ ハミガキ 30g", "maker": "ライオン",
              "dept": "輸出事業", "handling": "取扱中", "inactive": "F", "item_rank": "NEW", "sku_id": "K1",
              "pkg_weight": "45", "pkg_height": "15", "pkg_width": "4", "pkg_depth": "3"},
             {"id": "2", "code": "X2", "jan": "4900000000028", "name": "完全な品", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "F", "item_rank": "NEW", "sku_id": "K2"},
             {"id": "3", "code": "X3", "jan": "", "name": "", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "F", "item_rank": "NEW", "sku_id": "K3"}]
    skus = {"X1": {"SKUID": "K1", "Code": "X1", "Weight": 1, "Length": 1, "Width": 1, "Height": 1, "Image": "", "Type": "普通"},
            "X2": {"SKUID": "K2", "Code": "X2", "Weight": 80, "Length": 9, "Width": 5, "Height": 3,
                   "Image": "https://a/2.jpg", "Type": "普通"},
            "X3": {"SKUID": "K3", "Code": "X3", "Weight": 0, "Length": 9, "Width": 5, "Height": 3,
                   "Image": "https://a/3.jpg", "Type": "普通"}}
    customs = {"K1": None, "K2": {"Code": "330610", "NameEn": "Toothpaste"}, "K3": {"Code": "330610", "NameEn": "Toothpaste"}}
    prod, cus, queries = [], [], []
    with mock.patch("shared.db.get_connection", mock.MagicMock()), \
            mock.patch.object(NC, "load_choices", return_value=NC.Choices(suppliers=(), middle_by_large={})), \
            mock.patch.object(NC, "load_prefix_makers", return_value={}), \
            mock.patch.object(nst_suiteql, "suiteql_all", side_effect=lambda q: queries.append(q) or items), \
            mock.patch.object(banma_client.BanmaClient, "from_env", return_value=object()), \
            mock.patch.object(banma_client, "ensure_token"), \
            mock.patch.object(BMR, "get_skus", return_value=skus), \
            mock.patch.object(banma_fix, "get_customs", side_effect=lambda c, sid: customs[sid]), \
            mock.patch.object(banma_fix, "spu_ids", return_value={"K1": "S1"}), \
            mock.patch.object(image_search, "_load_cache",
                              return_value={"4900000000011": {"status": "ok", "image_url": "https://img/x1.jpg"}}), \
            mock.patch.object(hs_ai, "is_configured", return_value=False), \
            mock.patch.object(banma_fix, "write_product",
                              side_effect=lambda c, conn, spu, ch, codes, **kw: prod.append((spu, ch, kw)) or {"status": "ok"}), \
            mock.patch.object(banma_fix, "write_customs",
                              side_effect=lambda c, conn, sid, code, new, **kw: cus.append((sid, new, kw))
                              or {"status": "created"}):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button(key="p47c_run").click().run()
        assert not at.exception, [e.value for e in at.exception]
        info = " ".join(i.value for i in at.info)
        assert "条件に合う 3 件" in info and "空あり 2 件" in info and "重量 2" in info and "画像 1" in info
        assert not at.error                                          # 重量 0（提案なし）は入力エラーにしない
        assert "IS NOT NULL" in queries[0]
        at.button(key="p47c_write").click().run()
    assert not at.exception, [e.value for e in at.exception]
    # 重量 = NST 包装 45g（JDL なし）· 三辺 = NST 15×4×3 · 画像 = 画像検索 · HS・英文名 = 規則（歯磨き）
    assert [(spu, ch) for spu, ch, _ in prod] == [
        ("S1", {"K1": {"Weight": 45, "Length": 15, "Width": 4, "Height": 3, "Image": "https://img/x1.jpg"}})]
    assert prod[0][2]["expect"]["K1"]["Weight"] == 1 and cus[0][2]["expect"]["Code"] == ""   # 書く直前の照合用
    assert cus and cus[0][0] == "K1" and cus[0][1]["Code"] and cus[0][1]["NameEn"] and cus[0][2]["weight"] == 45
    assert any("ok=2" in s.value for s in at.success)


def _ui_texts(at) -> list[str]:
    """本文の表示文言（title・ボタン・caption・info 等・入力欄のラベル）。表の中身（商品名などのデータ）は見ない。"""
    m = at.main
    out = [x.value for kind in ("title", "header", "subheader", "caption", "info", "warning", "error", "success")
           for x in getattr(m, kind)]
    out += [b.label for b in m.button] + [d.proto.label for d in m.get("download_button")]
    out += [w.label for kind in ("selectbox", "multiselect", "radio", "checkbox") for w in getattr(m, kind)]
    return out


def test_page47_zh_ja_ui_text_20261112(page15_offline):
    """中文モードは仮名なし・日文モードは簡体字なし（Boss 2026-11-12「中文全部中文、日文全部日文」）。
    初期画面 + 🔎 SKUID 空チェックの結果画面まで。斑马 / 待上图 は固有名詞なので字の判定から外す。"""
    import re
    from shared import banma_client, nst_suiteql
    from shared import banma_register as BMR
    kana = re.compile(r"[ぁ-ゖァ-ヺ]")
    simplified = re.compile("[这们为对说时过还个么请询选择从结务设录删题无现击产员门间开关长检认让给级钮显库仓找须该败导]")
    blank = [{"id": "1", "code": "X1", "jan": "4900000000011", "name": "名1", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "F", "item_rank": "NEW"},
             {"id": "5", "code": "X5", "jan": "4900000000059", "name": "名5", "dept": "輸出事業", "handling": "取扱中",
              "inactive": "F", "item_rank": None}]
    texts = {}
    for lang in ("zh", "ja"):
        st.cache_data.clear()
        # get_lang も固定: AppTest は format_func（= t）をスクリプト外で呼ぶ（_ui_ja の docstring）
        with mock.patch("shared.i18n.get_lang", return_value=lang), \
                mock.patch("shared.db.get_connection", mock.MagicMock()), \
                mock.patch.object(nst_suiteql, "suiteql_all",
                                  side_effect=lambda q: blank if "IS NULL" in q else [{"code": "OLD", "sku_id": "105"}]), \
                mock.patch.object(banma_client.BanmaClient, "from_env", return_value=object()), \
                mock.patch.object(banma_client, "ensure_token"), \
                mock.patch.object(BMR, "find_skus", return_value={"X1": "101", "X5": "105"}):
            at = AppTest.from_file(str(PAGE), default_timeout=60)
            at.session_state["__auth_ok"] = True
            at.session_state["lang"] = lang
            at.run()
            first = _ui_texts(at)
            at.button(key="p47s_run").click().run()
        assert not at.exception, [e.value for e in at.exception]
        texts[lang] = first + _ui_texts(at)
    assert any("NST 中 SKUID 为空" in x for x in texts["zh"]) and any("NST で SKUID が空の品をチェック" in x for x in texts["ja"])
    assert not [x for x in texts["zh"] if kana.search(x)]
    assert not [x for x in texts["ja"] if simplified.search(x.replace("斑马", "").replace("待上图", ""))]


def test_page47_parallel_workers_see_session_language():
    """_parallel の工作スレッドでも get_lang / t() がこのセッションの言語を読む（ScriptRunContext を渡す）。
    渡さないと固定 zh になり、1 件ごとに missing ScriptRunContext の警告がログに出る。"""
    import ast

    src = PAGE.read_text(encoding="utf-8")
    code = next(ast.get_source_segment(src, n) for n in ast.parse(src).body
                if isinstance(n, ast.FunctionDef) and n.name == "_parallel")

    def app():
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        import streamlit as st
        from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

        from shared import i18n
        ns = {"threading": threading, "time": time, "ThreadPoolExecutor": ThreadPoolExecutor,
              "as_completed": as_completed, "st": st, "add_script_run_ctx": add_script_run_ctx,
              "get_script_run_ctx": get_script_run_ctx}
        exec(st.session_state["code"], ns)
        st.session_state["res"] = ns["_parallel"](lambda k: i18n.get_lang(), [1, 2, 3], "x", workers=3)

    with mock.patch("shared.i18n.get_lang", side_effect=lambda: st.session_state.get("lang", "zh")):
        at = AppTest.from_function(app)
        at.session_state["code"], at.session_state["lang"] = code, "ja"
        at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["res"] == {1: "ja", 2: "ja", 3: "ja"}
