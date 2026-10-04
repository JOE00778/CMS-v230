"""shared/item_register の単体テスト — fetch 系・conn はモック（网络/DB 不要）。"""
from __future__ import annotations

import csv
import io
import json

from data_warehouse.templates import jd_bm_item_master as JBM
from data_warehouse.templates import nst_item_master as TPL
from shared import item_register as R
from shared.hs_classify import Customs
from shared.jan_web import JanMeta, WeightHit
from shared.nst_choices import Choices, FIXED

CH = Choices(suppliers=("0001 ベストアンサ株式会社",),
             middle_by_large={"生活雑貨": ("日用品雑貨",), "食品": ("菓子",)})


def _row(jan, large="生活雑貨", **kw):
    r = {"JANコード": jan, "商品原価": "100", "仕入先1：仕入先": "0001 ベストアンサ株式会社",
         "商品担当者": "043 徐越", "大分類": large, "中分類": "日用品雑貨" if large == "生活雑貨" else "菓子",
         "カートン入数": "24", "発注ロット": "1", "パッケージ高さ(cm)": "", "パッケージ幅(cm)": "",
         "パッケージ奥行(cm)": "", "パッケージ重量(g)": ""}
    r.update(kw)
    return r


METAS = {
    "4901234567894": JanMeta("4901234567894", "テスト シャンプー 480ml", "テスト株式会社", "", "ok"),
    "4512345678906": JanMeta("4512345678906", "あ" * 70, "", "", "ok"),
    "4900000000003": JanMeta("4900000000003", "", "", "", "not_found"),
    "4911111111113": JanMeta("4911111111113", "", "", "", "error", "http=403"),
}


def _meta(jan):
    if jan == "4922222222223":
        raise RuntimeError("boom")
    return METAS[jan]


def _weight(jan):
    return WeightHit(jan, 195.0, 13.5, 7.6, 4.5, "superdelivery", f"https://superdelivery/{jan}")


def _cus(name, maker, jan, *, prefix_map=None):
    return Customs("330510", "Shampoo, 480ml", "シャンプー", "jp-name") if "シャンプー" in name \
        else Customs(None, None, None, "none")


def _enrich(rows, want_weight=False, **kw):
    return R.enrich(rows, choices=CH, prefix_makers={"4901234": "NSTメーカー"},
                    fetch_meta=_meta, fetch_weight=kw.pop("fw", _weight), classify_customs=_cus,
                    want_weight=want_weight, interval=0, **kw)


def test_enrich_fills_fixed_maker_name_hs_and_counts():
    rows = [_row("4901234567894", **{"パッケージ高さ(cm)": "20"}),
            _row("4512345678906", large="食品"),
            _row("4900000000003"), _row("4911111111113"), _row("4922222222223")]
    prog = []
    nst, extra, issues, st = _enrich(rows, want_weight=True, on_progress=lambda *a: prog.append(a))

    assert [r["JANコード"] for r in nst] == ["4901234567894", "4512345678906"]
    a, b = nst
    assert a["型番"] == "4901234567894" and a["アイテム名"] == "テスト シャンプー 480ml"
    assert a["メーカー名"] == "NSTメーカー" and extra[a["JANコード"]]["maker_source"] == "nst-prefix"
    assert all(a[k] == v for k, v in FIXED.items())
    assert "税率" not in a and b["税率"] == "★"            # 食品だけ ★（空列は持たない）
    assert len(b["アイテム名"]) == 60 and "メーカー名" not in b
    # 人が書いた高さは上書きしない・空欄だけ埋める
    assert a["パッケージ高さ(cm)"] == "20" and a["パッケージ幅(cm)"] == "7.6"
    assert a["パッケージ重量(g)"] == "195"
    assert extra[a["JANコード"]]["weight_source"] == "superdelivery"
    assert extra[a["JANコード"]]["weight_url"].endswith("4901234567894")
    assert extra[a["JANコード"]]["_hs"] == "330510"
    assert extra[b["JANコード"]]["_hs"] == ""

    errs = {i.jan for i in issues if i.level == "error"}
    assert errs == {"4900000000003", "4911111111113", "4922222222223"}
    assert st["ok"] == 2 and st["ng"] == 3 and st["warn"] == 1
    assert (st["jancode_ok"], st["jancode_not_found"], st["jancode_error"]) == (2, 1, 2)
    assert (st["hs_ok"], st["hs_ng"], st["name_cut"]) == (1, 1, 1)
    assert (st["maker_nst"], st["maker_none"]) == (1, 1)
    assert st["weight_hit"] == 2
    assert prog[-1][0] == prog[-1][1]                         # 進捗は total に届く


def test_enrich_weight_off_does_not_fetch_and_full_rows_skip():
    called = []
    fw = lambda j: called.append(j)                           # noqa: E731
    _enrich([_row("4901234567894")], fw=fw)
    assert called == []
    full = {"パッケージ高さ(cm)": "1", "パッケージ幅(cm)": "1", "パッケージ奥行(cm)": "1",
            "パッケージ重量(g)": "1"}
    _, _, _, st = _enrich([_row("4901234567894", **full)], want_weight=True, fw=fw)
    assert called == [] and st["weight_skipped"] == 1


def test_enrich_weight_miss_and_error_are_counted():
    def fw(j):
        if j == "4901234567894":
            return None
        raise OSError("down")
    _, _, issues, st = _enrich([_row("4901234567894"), _row("4512345678906")],
                               want_weight=True, fw=fw)
    assert st["weight_miss"] == 1 and st["weight_error"] == 1
    assert sum("網調べ失敗" in i.message for i in issues) == 1


def test_banned_chars_warn():
    METAS["4901234567894"] = JanMeta("4901234567894", "シャンプー（大）, Ⅱ", "", "", "ok")
    try:
        _, _, issues, _ = _enrich([_row("4901234567894")])
    finally:
        METAS["4901234567894"] = JanMeta("4901234567894", "テスト シャンプー 480ml",
                                         "テスト株式会社", "", "ok")
    assert any("禁止文字" in i.message for i in issues)


def test_frame_roundtrip_edits_and_outputs():
    nst, extra, _, _ = _enrich([_row("4901234567894"), _row("4512345678906", large="食品")],
                               want_weight=True)
    recs = R.to_frame(nst, extra)
    assert "パッケージ" in recs[0][R.COL_AUTO] and recs[0]["HS"] == "330510"
    recs[0]["通関英文名"] = "Shampoo"
    recs[0]["パッケージ重量(g)"] = "abc"
    recs[1]["アイテム名"] = ""
    out_nst, out_bm, src, issues = R.from_frame(recs, nst, extra)
    assert [r["JANコード"] for r in out_nst] == ["4901234567894"]
    assert "パッケージ重量(g)" not in out_nst[0]
    assert out_bm[0]["_name_en"] == "Shampoo" and out_bm[0]["_hs"] == "330510"
    assert src == {"4901234567894": "superdelivery"}
    assert {i.level for i in issues} == {"warn", "error"}

    # NST CSV: 有データ列のみ・先頭は 型番（原本の登録手順どおり Internal ID 列は無い）
    cols = R.csv_field_columns(out_nst)
    csv_bytes = TPL.build_nst_master_csv(out_nst, cols, id_label=TPL.COL_ITEM_CODE)
    header, first = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))[:2]
    assert header[0] == "型番" and header.count("型番") == 1 and "Internal ID" not in header
    assert "商品高さ(cm)" not in header and first[0] == "4901234567894"
    # 斑马: 中文名称 = アイテム名 / 英文名称 = 通関英文名
    bm = JBM.nst_to_bm_row(out_bm[0])
    assert bm[29] == out_nst[0]["アイテム名"] and bm[30] == "Shampoo" and bm[34] == "330510"


class _Conn:
    def __init__(self, fail=False):
        self.fail, self.calls, self.committed, self.rolled = fail, [], False, False

    def executemany(self, sql, params):
        if self.fail:
            raise RuntimeError('relation "nst.item_register_log" does not exist')
        self.calls.append((sql, list(params)))

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled = True


def test_write_register_log_ok_and_failure():
    nst = [{"JANコード": "4901234567894", "商品担当者": "043 徐越", "アイテム名": "x"}]
    bm = [JBM.nst_to_bm_row(nst[0])]
    c = _Conn()
    n, err = R.write_register_log(c, nst, bm, {"4901234567894": "superdelivery"})
    assert (n, err, c.committed) == (1, "", True)
    sql, params = c.calls[0]
    assert "nst.item_register_log" in sql
    jan, payload, bm_payload, src, who = params[0]
    assert jan == "4901234567894" and src == "superdelivery" and who == "043 徐越"
    assert json.loads(payload)["アイテム名"] == "x"
    assert json.loads(bm_payload)["SPU"] == "4901234567894"

    c = _Conn(fail=True)
    n, err = R.write_register_log(c, nst, bm)
    assert n == 0 and "does not exist" in err and c.rolled


def test_nst_csv_has_template_derived_columns_and_payload_matches():
    """原本 BJ〜BS の数式どおり（輸出専用）。CSV と台帳 payload は同じ dict から出る。"""
    nst, extra, _, _ = _enrich([_row("4901234567894"), _row("4512345678906", large="食品")])
    out_nst, out_bm, src, _ = R.from_frame(R.to_frame(nst, extra), nst, extra)
    csv_bytes = TPL.build_nst_master_csv(out_nst, R.csv_field_columns(out_nst),
                                         id_label=TPL.COL_ITEM_CODE)
    rows = list(csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    a, b = (dict(zip(rows[0], r)) for r in rows[1:3])
    for rec in (a, b):
        assert rec["購入価格"] == "100" and rec["仕入先1：優先"] == "T"
        assert rec["優先場所"] == "弁天倉庫" and rec["保管棚を使用"] == "T"
        assert rec["売上原価勘定"] == "501121 仕入 : 仕入 : 仕入高 : 国内仕入-輸出"
        assert rec["サポート提供 "] == "T" and rec["NE_連携対象"] == "F" and rec["原価計算法"] == "平均"
    assert a["納税スケジュール"] == "仕入10%＆販売免税" and b["納税スケジュール"] == "仕入8%＆販売免税"
    assert "適正在庫水準の日数" not in rows[0] and "輸入諸掛をトラッキング" not in rows[0]

    c = _Conn()
    R.write_register_log(c, out_nst, None, src)
    payload = json.loads(c.calls[0][1][0][1])
    assert payload["サポート提供 "] == "T" and payload["原価計算法"] == "平均"


def test_food_large_category_vetoes_non_food_hs():
    def cus(name, maker, jan, *, prefix_map=None):
        if "チップ" in name:
            return Customs("961620", "Powder puff", "化粧小物・ブラシ", "jp-name")
        return Customs("170490", "Biscuits", "菓子", "jp-name")
    METAS["4512345678906"] = JanMeta("4512345678906", "テスト ポテトチップス 60g", "", "", "ok")
    METAS["4901234567894"] = JanMeta("4901234567894", "テスト クッキー", "", "", "ok")
    try:
        nst, extra, issues, st = R.enrich(
            [_row("4512345678906", large="食品"), _row("4901234567894", large="食品")],
            choices=CH, prefix_makers={}, fetch_meta=_meta, fetch_weight=_weight,
            classify_customs=cus, want_weight=False, interval=0)
    finally:
        METAS["4512345678906"] = JanMeta("4512345678906", "あ" * 70, "", "", "ok")
        METAS["4901234567894"] = JanMeta("4901234567894", "テスト シャンプー 480ml",
                                         "テスト株式会社", "", "ok")
    assert extra["4512345678906"]["_hs"] == "" and extra["4512345678906"]["_name_en"] == ""
    assert extra["4901234567894"]["_hs"] == "170490"
    assert (st["hs_ok"], st["hs_ng"]) == (1, 1)
    assert any("大分類=食品" in i.message and i.jan == "4512345678906" for i in issues)


def test_jancode_maker_not_in_nst_makers_warns():
    nst, extra, issues, st = R.enrich(
        [_row("4901234567894")], choices=CH, prefix_makers={"4999999": "KAO"},
        fetch_meta=_meta, fetch_weight=_weight, classify_customs=_cus,
        want_weight=False, interval=0)
    assert nst[0]["メーカー名"] == "テスト株式会社" and st["maker_jancode"] == 1
    assert any("NST 既存メーカーに無い" in i.message for i in issues)


def test_enrich_real_fetch_weight_network_failure_counts_as_error(monkeypatch):
    """実 fetch_weight + 全リクエスト 403 → weight_error（「記載なし」にしない）。"""
    import urllib.error

    from shared import jan_web
    monkeypatch.setattr(jan_web.time, "sleep", lambda s: None)

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "x", {}, None)

    _, _, issues, st = _enrich([_row("4901234567894")], want_weight=True,
                               fw=lambda j: jan_web.fetch_weight(j, opener=opener))
    assert st["weight_error"] == 1 and st["weight_miss"] == 0
    assert any("網調べ失敗" in i.message for i in issues)


def test_manual_name_rescues_jancode_not_found_and_wins_over_jancode():
    rows = [_row("4900000000003", **{"アイテム名（任意）": "手入力 シャンプー 300ml"}),   # jancode に名前なし
            _row("4911111111113", **{"アイテム名（任意）": "手入力B"}),                   # jancode 取得失敗
            _row("4901234567894", **{"アイテム名（任意）": "人が直した名前"}),            # jancode あり → 手入力優先
            _row("4900000000003")]                                                        # 手入力なし → 従来どおり error
    nst, extra, issues, st = _enrich(rows)
    by = {r["JANコード"]: r for r in nst}
    assert by["4900000000003"]["アイテム名"] == "手入力 シャンプー 300ml"
    assert by["4911111111113"]["アイテム名"] == "手入力B"
    assert by["4901234567894"]["アイテム名"] == "人が直した名前"
    assert "アイテム名（任意）" not in by["4901234567894"]                 # NST 行には入れない
    assert "アイテム名(jancode)" not in extra["4901234567894"]["auto"]
    assert extra["4900000000003"]["_hs"] == "330510"                       # 手入力名で HS 判定
    assert st["name_manual"] == 3
    warn = {i.jan: i.message for i in issues if i.level == "warn" and "手入力" in i.message}
    assert set(warn) == {"4900000000003", "4911111111113"}
    assert [i.jan for i in issues if i.level == "error"] == ["4900000000003"]
