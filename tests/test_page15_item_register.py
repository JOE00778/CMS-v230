"""page15（商品登录）を Streamlit AppTest で無頭実行 — 例外なく 4 タブとも描画されること。

load_choices / SuiteQL / DB はモック（网络/DB 不要）。
"""
from __future__ import annotations

import pathlib
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from shared import nst_choices as NC

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "15_📝_商品登录.py"


def test_page15_renders_all_tabs_without_exception(page15_offline):
    st.cache_data.clear()
    choices = NC.Choices(suppliers=("0001 ベストアンサ株式会社",),
                         middle_by_large=dict(NC.MIDDLE_BY_LARGE),
                         warnings=("NST 分類の取得に失敗したため 2026-10-02 時点の定数を使用: mock",))
    with mock.patch("shared.db.get_connection", mock.MagicMock()), \
            mock.patch.object(NC, "load_choices", return_value=choices) as lc, \
            mock.patch.object(NC, "load_prefix_makers", return_value={}), \
            mock.patch.object(NC, "existing_jans", return_value=(set(), [])):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) == 4
    assert lc.called
    assert any("定数を使用" in w.value for w in at.warning)          # choices.warnings を表示
    labels = [b.label for b in at.button]
    assert any("処理する" in x for x in labels)                     # 🆕 商品登録
    assert any("查询 PG" in x for x in labels)                      # 📦 セット品登録
    assert any("チェックする" in x for x in labels)                  # 🔎 SKUID 空チェック
    assert not any("JD" in x for x in labels)                       # JD UI は削除済み
    assert not any("ネットで調べる" in c.label for c in at.checkbox)  # 網調べは常に実行（切替なし）


def _filled_upload(choices, weight: bool = True) -> bytes:
    import io

    from openpyxl import load_workbook

    from data_warehouse.templates import item_entry_form as FORM
    wb = load_workbook(io.BytesIO(FORM.build_template(choices)))
    ws = wb[FORM.SHEET_INPUT]
    vals = {"JANコード": "4901234567894", "商品原価": "100",
            "仕入先1：仕入先": "0001 ベストアンサ株式会社", "商品担当者": "043 徐越",
            "大分類": "食品", "中分類": "菓子", "カートン入数": "24", "発注ロット": "1",
            "パッケージ高さ(cm)": "10", "パッケージ幅(cm)": "5", "パッケージ奥行(cm)": "3"}
    for j, cell in enumerate(ws[1], start=1):
        if cell.value in vals:
            ws.cell(row=2, column=j, value=vals[cell.value])
    # 重量はテンプレに無い（ネット調べ→無ければ確認表で手入力）。テストでは旧テンプレ同様に列を足して入れる
    if weight:
        ws.cell(row=1, column=ws.max_column + 1, value="パッケージ重量(g)")
        ws.cell(row=2, column=ws.max_column, value="80")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_page15_process_and_generate_flow(page15_offline):
    """UL → ▶ 処理する → 📦 生成 まで。jancode はモック、台帳 INSERT は MagicMock conn。"""
    from shared import jan_web

    st.cache_data.clear()
    choices = NC.Choices(suppliers=("0001 ベストアンサ株式会社",),
                         middle_by_large=dict(NC.MIDDLE_BY_LARGE))
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(choices)
    meta = jan_web.JanMeta("4901234567894", "テスト クッキー 100g", "テスト製菓株式会社", "", "ok")
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    with mock.patch("shared.db.get_connection", return_value=conn), \
            mock.patch("streamlit.file_uploader", return_value=up), \
            mock.patch.object(NC, "load_choices", return_value=choices), \
            mock.patch.object(NC, "load_prefix_makers", return_value={}), \
            mock.patch.object(NC, "existing_jans", return_value=(set(), [])), \
            mock.patch.object(NC, "existing_item_codes", return_value=(set(), [])), \
            mock.patch.object(jan_web, "fetch_meta", return_value=meta), \
            mock.patch.object(jan_web, "INTERVAL", 0):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button(key="page15_run").click().run()
        assert not at.exception, [e.value for e in at.exception]
        assert [m.value for m in at.metric][:2] == ["1", "0"]       # OK=1 NG=0
        assert at.checkbox(key="page15_to_banma").value is True       # 既定 = 斑马にも登録
        at.button(key="page15_gen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["page15_csv_n"] == 1
    assert at.session_state["page15_skuid_blank"] == 0              # fixture の斑马は SKUID "1" を返す
    assert at.session_state["page15_log_msg"][0] == "ok"
    assert conn.executemany.called                                   # 台帳へ INSERT

    import csv
    import io
    import zipfile

    header, row = list(csv.reader(io.StringIO(at.session_state["page15_csv_bytes"].decode("utf-8-sig"))))[:2]
    assert at.session_state["page15_csv_name"].endswith("_NetSuite【アイテム】マスタ登録-V260326EX_徐越.csv")
    rec = dict(zip(header, row))
    assert header[0] == "型番" and rec["型番"] == "4901234567894"
    assert rec["アイテム名"] == "テスト クッキー 100g" and rec["税率"] == "★"
    assert rec["商品ランク"] == "NEW" and rec["SKUID"] == "1"       # 斑马にも登録 → NEW・斑马 SKUID
    assert rec["仕入先1：仕入先"] == "0001 ベストアンサ株式会社" and rec["仕入区分"] == "輸出専用"
    from shared.item_register import COL_IMAGE, PLACEHOLDER_IMAGE_URL
    (bm_row,), _ = page15_offline.call_args[0][1:], None
    assert page15_offline.call_count == 1
    assert bm_row["アイテム名"] == "テスト クッキー 100g"
    assert bm_row[COL_IMAGE] == PLACEHOLDER_IMAGE_URL                # jancode・画像検索とも無し → 待上图
    res = at.session_state["page15_bm_result"]
    assert res["logged"] and [c for c, _ in res["rows"]] == ["4901234567894"]


def _run_page(choices, up, *, meta, exist=frozenset(), conn=None, bm=None):
    """▶ 処理する まで進めた AppTest と patch の ExitStack を返す（呼び出し側で close）。"""
    import contextlib

    from shared import jan_web
    conn = conn or mock.MagicMock()
    conn.__enter__.return_value = conn
    fm = meta if callable(meta) else mock.MagicMock(return_value=meta)
    stack = contextlib.ExitStack()
    for p in (mock.patch("shared.db.get_connection", return_value=conn),
              mock.patch("streamlit.file_uploader", return_value=up),
              mock.patch.object(NC, "load_choices", return_value=choices),
              mock.patch.object(NC, "load_prefix_makers", return_value={}),
              mock.patch.object(NC, "existing_jans", return_value=(set(exist), [])),
              mock.patch.object(jan_web, "fetch_meta", fm),
              mock.patch.object(jan_web, "INTERVAL", 0),
              mock.patch("shared.banma_register.find_skus", return_value=dict(bm or {}))):
        stack.enter_context(p)
    st.cache_data.clear()
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.session_state["__auth_ok"] = True
    at.run()
    at.button(key="page15_run").click().run()
    return at, stack, fm, conn


def _choices():
    return NC.Choices(suppliers=("0001 ベストアンサ株式会社",),
                      middle_by_large=dict(NC.MIDDLE_BY_LARGE))


def test_page15_nst_only_existing_goes_to_banma_only(page15_offline):
    """NST にだけある → 斑马にだけ登録・NST CSV には出さない（Boss 2026-11-10）。"""
    from shared import jan_web
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(ch)
    meta = jan_web.JanMeta("4901234567894", "テスト クッキー", "", "", "ok")
    at, stack, fm, _ = _run_page(ch, up, meta=meta, exist={"4901234567894"})
    with stack:
        assert not at.exception, [e.value for e in at.exception]
        assert [m.value for m in at.metric][:2] == ["1", "0"]
        assert any("斑马にだけ登録 1 件" in i.value for i in at.info)
        at.button(key="page15_gen").click().run()
        assert not at.exception, [e.value for e in at.exception]
    assert page15_offline.call_count == 1                                  # 斑马には登録
    assert any("SKUID が NST に入っていません" in w.value for w in at.warning)
    assert at.session_state["page15_csv_n"] == 0 and "page15_csv_bytes" not in at.session_state
    assert any("すべて NST に登録済み" in i.value for i in at.info)


def test_page15_both_existing_is_skipped(page15_offline):
    """NST・斑马とも済 → 何もしない（error ではなくスキップ）。"""
    from shared import jan_web
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(ch)
    meta = jan_web.JanMeta("4901234567894", "テスト クッキー", "", "", "ok")
    at, stack, fm, _ = _run_page(ch, up, meta=meta, exist={"4901234567894"}, bm={"4901234567894": "777"})
    with stack:
        assert not at.exception, [e.value for e in at.exception]
        assert [m.value for m in at.metric][:2] == ["0", "0"]
        assert not fm.called and page15_offline.call_count == 0
        assert any("スキップ 1 件" in i.value for i in at.info)


def test_page15_banma_only_existing_goes_to_nst_csv_only(page15_offline):
    """斑马にだけある → NST CSV に出し、斑马は登録せず既存 SKUID を入れる。重量の手入力は不要。"""
    import csv
    import io

    from shared import jan_web
    page15_offline.return_value = {"status": "skipped", "sku_id": "777", "spu_id": "", "message": "既存"}
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(ch, weight=False)
    meta = jan_web.JanMeta("4901234567894", "テスト クッキー", "", "", "ok")
    at, stack, fm, _ = _run_page(ch, up, meta=meta, bm={"4901234567894": "777"})
    with stack:
        assert any("NST CSV にだけ出す 1 件" in i.value for i in at.info)
        at.button(key="page15_gen").click().run()
        assert not at.exception, [e.value for e in at.exception]
    rec = next(csv.DictReader(io.StringIO(at.session_state["page15_csv_bytes"].decode("utf-8-sig"))))
    assert rec["SKUID"] == "777" and rec["型番"] == "4901234567894"


def test_page15_rerun_retries_after_jancode_error(page15_offline):
    from shared import jan_web
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(ch)
    err = jan_web.JanMeta("4901234567894", "", "", "", "error", "http=403")
    at, stack, fm, _ = _run_page(ch, up, meta=err)
    with stack:
        assert fm.call_count == 1 and [m.value for m in at.metric][:2] == ["1", "0"]   # 名前なしでも行は残る
        fix = next(d.value for d in at.dataframe if list(d.value.columns) == ["型番", "アイテム名", "パッケージ重量(g)"])
        assert fix["型番"].tolist() == ["4901234567894"] and fix["アイテム名"].fillna("").eq("").all()
        fm.return_value = jan_web.JanMeta("4901234567894", "テスト クッキー", "", "", "ok")
        at.button(key="page15_run").click().run()
        assert fm.call_count == 2 and [m.value for m in at.metric][:2] == ["1", "0"]


def test_page15_broken_xlsx_shows_error_and_other_tabs_render(page15_offline):
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = b"not an xlsx"
    at, stack, _, _ = _run_page(ch, up, meta=None)
    with stack:
        assert not at.exception, [e.value for e in at.exception]
        assert any("処理に失敗" in e.value for e in at.error)
        assert any(b.key == "page15_bundle_query" for b in at.button)


def test_page15_edit_hides_stale_zip_and_log_not_duplicated(page15_offline):
    from shared import jan_web
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(ch)
    meta = jan_web.JanMeta("4901234567894", "テスト クッキー", "", "", "ok")
    at, stack, _, conn = _run_page(ch, up, meta=meta)
    with stack:
        at.button(key="page15_gen").click().run()
        at.button(key="page15_gen").click().run()
        assert conn.executemany.call_count == 1                     # 同じ内容は台帳に 1 回だけ
        assert any("page15_csv_dl" in b.proto.id for b in at.get("download_button"))
        # 生成後に表を編集 → 古い ZIP は伏せる
        at.session_state["page15_csv_src"] = "stale"
        at.run()
        assert not any("page15_csv_dl" in b.proto.id for b in at.get("download_button"))
        assert any("表を編集しました" in w.value for w in at.warning)


def test_page15_unchecked_banma_makes_nst_csv_only(page15_offline):
    """チェックを外すと斑马に触らず NST CSV だけ・SKUID は空（Boss 2026-11-10）。"""
    import csv
    import io
    import zipfile

    from shared import jan_web
    st.cache_data.clear()
    choices = NC.Choices(suppliers=("0001 ベストアンサ株式会社",), middle_by_large=dict(NC.MIDDLE_BY_LARGE))
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(choices)
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    with mock.patch("shared.db.get_connection", return_value=conn), \
            mock.patch("streamlit.file_uploader", return_value=up), \
            mock.patch.object(NC, "load_choices", return_value=choices), \
            mock.patch.object(NC, "load_prefix_makers", return_value={}), \
            mock.patch.object(NC, "existing_jans", return_value=(set(), [])), \
            mock.patch.object(NC, "existing_item_codes", return_value=(set(), [])), \
            mock.patch.object(jan_web, "fetch_meta",
                              return_value=jan_web.JanMeta("4901234567894", "テスト クッキー 100g", "テスト製菓", "", "ok")), \
            mock.patch.object(jan_web, "INTERVAL", 0):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button(key="page15_run").click().run()
        at.checkbox(key="page15_to_banma").uncheck().run()
        at.button(key="page15_gen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert page15_offline.call_count == 0                            # 斑马 API は呼ばない
    assert "page15_bm_result" not in at.session_state and at.session_state["page15_skuid_blank"] == 0
    rec = next(csv.DictReader(io.StringIO(at.session_state["page15_csv_bytes"].decode("utf-8-sig"))))
    assert rec["SKUID"] == "" and rec["商品ランク"] == "" and rec["パッケージ重量(g)"] == "80"


def test_page15_missing_weight_can_be_typed_in_cms(page15_offline):
    """重量がネットで取れない品 → 確認画面の小表に入れるだけで生成できる（再アップロード不要・Boss 2026-11-10）。"""
    import csv
    import io
    import zipfile

    from openpyxl import load_workbook

    from data_warehouse.templates import item_entry_form as FORM
    from shared import jan_web
    st.cache_data.clear()
    choices = NC.Choices(suppliers=("0001 ベストアンサ株式会社",), middle_by_large=dict(NC.MIDDLE_BY_LARGE))
    wb = load_workbook(io.BytesIO(FORM.build_template(choices)))
    ws = wb[FORM.SHEET_INPUT]
    vals = {"JANコード": "4901234567894", "商品原価": "100", "仕入先1：仕入先": "0001 ベストアンサ株式会社",
            "商品担当者": "043 徐越", "大分類": "食品", "中分類": "菓子", "カートン入数": "24"}
    for j, cell in enumerate(ws[1], start=1):
        if cell.value in vals:
            ws.cell(row=2, column=j, value=vals[cell.value])
    buf = io.BytesIO()
    wb.save(buf)
    up = mock.MagicMock()
    up.getvalue.return_value = buf.getvalue()
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    with mock.patch("shared.db.get_connection", return_value=conn), \
            mock.patch("streamlit.file_uploader", return_value=up), \
            mock.patch.object(NC, "load_choices", return_value=choices), \
            mock.patch.object(NC, "load_prefix_makers", return_value={}), \
            mock.patch.object(NC, "existing_jans", return_value=(set(), [])), \
            mock.patch.object(NC, "existing_item_codes", return_value=(set(), [])), \
            mock.patch.object(jan_web, "fetch_meta",
                              return_value=jan_web.JanMeta("4901234567894", "テスト クッキー 100g", "テスト製菓", "", "ok")), \
            mock.patch.object(jan_web, "INTERVAL", 0):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button(key="page15_run").click().run()
        at.button(key="page15_gen").click().run()                      # 重量なし → 生成されない
        assert "page15_csv_bytes" not in at.session_state and page15_offline.call_count == 0
        fix = next(d.value for d in at.dataframe if list(d.value.columns) == ["型番", "アイテム名", "パッケージ重量(g)"])
        assert fix["型番"].tolist() == ["4901234567894"]                 # 重量が空の品だけ出る
        real = st.data_editor

        def typed(data, *a, **k):                                        # 人が小表に 55 と入れた状態
            out = real(data, *a, **k)
            if k.get("key") == "page15_weight_fix":
                out = out.copy()
                out["パッケージ重量(g)"] = "55"
            return out
        with mock.patch("streamlit.data_editor", side_effect=typed):
            at.run()
            at.button(key="page15_gen").click().run()
    assert not at.exception, [e.value for e in at.exception]
    rec = next(csv.DictReader(io.StringIO(at.session_state["page15_csv_bytes"].decode("utf-8-sig"))))
    assert rec["パッケージ重量(g)"] == "55"
    assert page15_offline.call_args[0][1]["パッケージ重量(g)"] == "55"   # 斑马にも同じ重量


def test_page15_hs_retry_button_fills_blank_hs(page15_offline):
    """1 回目の AI が 429 で HS 空 → 「HS を AI で再判定」で空の品だけ埋まる（Boss 2026-11-10）。"""
    from shared import hs_ai, jan_web
    ch = _choices()
    up = mock.MagicMock()
    up.getvalue.return_value = _filled_upload(ch)
    meta = jan_web.JanMeta("4901234567894", "テスト クッキー 100g", "", "", "ok")
    calls = []

    def judge(items):
        calls.append([k for k, *_ in items])
        if len(calls) == 1:
            return {k: "HTTPError: HTTP Error 429: Too Many Requests" for k, *_ in items}
        return {k: hs_ai.AiHs("190590", "Biscuits", "菓子", 0.9) for k, *_ in items}
    with mock.patch.object(hs_ai, "is_configured", return_value=True), mock.patch.object(hs_ai, "judge", judge):
        at, stack, fm, _ = _run_page(ch, up, meta=meta)
        with stack:
            assert not at.exception, [e.value for e in at.exception]
            at.button(key="page15_hs_retry").click().run()
            assert not at.exception, [e.value for e in at.exception]
    assert calls == [["4901234567894"], ["4901234567894"]]           # 食品は規則を使わず AI（1 回目 429）→ 再判定
    assert at.session_state["page15_result"]["extra"]["4901234567894"]["_hs"] == "190590"
    assert any("1 件中 1 件に HS が入りました" in i.value for i in at.info)


def test_page15_skuid_tab_check_and_csv_20261112(page15_offline):
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
        at.button(key="p15s_run").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert asked == [["4900000000011", "4900000000042", "4900000000059", "X1", "X4", "X5"]]   # 条件外の X2・X3 は照会しない
    assert "TO_DATE(" in queries[0]                                    # 既定 = 直近 30 日
    info = " ".join(i.value for i in at.info)
    assert "条件に合う 3 件" in info and "斑马に有り 3 件" in info and "重複 1 件" in info
    assert any("斑马に無し" in h.value for h in at.subheader) is False   # 3 件とも斑马に有り
    dl = [d for d in at.get("download_button") if "SKUID 更新 CSV" in d.proto.label]
    assert dl and "（2 件）" in dl[0].proto.label                       # X5 は NST の OLD が同じ SKUID → 保留


def test_page15_skuid_tab_register_misses_20261112(page15_offline):
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
        at.button(key="p15s_run").click().run()
        assert not any("SKUID 更新 CSV" in d.proto.label for d in at.get("download_button"))
        at.button(key="p15s_prep_btn").click().run()
        at.button(key="p15s_reg_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]
    by = {r["型番"]: r for r in sent}
    assert by["X2"]["アイテム名"] == "台帳の名前" and by["X2"]["_hs"] == "950300" and by["X2"]["_image_url"] == "https://img/x2.jpg"
    assert by["X1"]["アイテム名"] == "NST の名前" and by["X1"]["商品原価"] == "120" and by["X1"]["パッケージ重量(g)"] == "45"
    assert sorted(ai) == ["X1", "X3"]                 # AI = NST の品 + 台帳でも HS 空の品（台帳の HS は触らない）
    assert any("1 g で登録" in w.value for w in at.warning)        # X2・X3 は重量なし
    dl = [d for d in at.get("download_button") if "SKUID 更新 CSV" in d.proto.label]
    assert dl and "（3 件）" in dl[0].proto.label
