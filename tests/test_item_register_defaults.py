from shared import item_register as R
from tests.test_item_register import _enrich, _row
import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def _ui_ja():
    """メッセージは日文原文で検査する（中文モードの訳は shared/i18n.py の TRANSLATIONS_ZH。t() は既定 zh なので ja に固定）。"""
    with mock.patch("shared.i18n.get_lang", return_value="ja"):
        yield


def test_missing_defaults_preserve_known_values_and_optional_maker():
    rows = [_row('4512345678906', **{R.COL_PKG_DIMS[0]: '8', R.COL_PKG_WEIGHT: '265'})]
    nst, extra, issues, _ = _enrich(rows)
    row = nst[0]
    assert [row[c] for c in (*R.COL_PKG_DIMS, R.COL_PKG_WEIGHT)] == ['8', '1', '1', '265']
    assert not row.get(R.COL_MAKER)
    assert not any('メーカー名が決まりません' in i.message for i in issues)
    frame = R.to_frame(nst, extra)
    assert '非実測' in frame[0][R.COL_WEIGHT_NOTE]
    assert R.COL_PKG_DIMS[1] in frame[0][R.COL_WEIGHT_NOTE]
    out, _, sources, _ = R.from_frame(frame, nst, extra)
    carton = (*R.COL_CARTON_DIMS, 'カートン入数')
    assert {k: v for k, v in out[0].items() if k not in carton} == {k: v for k, v in nst[0].items() if k not in carton}
    assert [out[0][c] for c in R.COL_CARTON_DIMS] == ['1', '1', '1']     # NST 必須 → 空は 1（Boss 2026-11-10）
    assert 'default-placeholder' in sources[row['型番']]
    frame[0][R.COL_PKG_DIMS[1]] = '12'
    frame[0][R.COL_PKG_DIMS[2]] = '13'
    _, _, sources, _ = R.from_frame(frame, nst, extra)
    assert 'default-placeholder' not in sources[row['型番']]


def test_cleared_or_invalid_values_default_on_export():
    nst, extra, _, _ = _enrich([_row('4901234567894')], want_weight=True)
    frame = R.to_frame(nst, extra)
    frame[0][R.COL_PKG_DIMS[0]] = ''
    frame[0][R.COL_PKG_WEIGHT] = 'bad'
    out, _, _, issues = R.from_frame(frame, nst, extra)
    assert out == [] and any(i.message == R.WEIGHT_REQUIRED_MSG for i in issues)   # 重量は仮値にしない
    frame[0][R.COL_PKG_WEIGHT] = '1'
    out, bm, sources, issues = R.from_frame(frame, nst, extra)
    assert out[0][R.COL_PKG_DIMS[0]] == bm[0][R.COL_PKG_WEIGHT] == '1'
    assert out[0][R.COL_PKG_DIMS[1]] == '7.6'
    assert 'default-placeholder' in sources[out[0]['型番']]
    assert any('非実測' in i.message for i in issues)


def test_user_supplied_one_is_not_a_placeholder():
    nst, extra, _, _ = _enrich([_row('4901234567894', **{c: '1' for c in R._WEIGHT_FIELDS})])
    _, _, sources, _ = R.from_frame(R.to_frame(nst, extra), nst, extra)
    assert sources['4901234567894'] == 'manual'


def test_no_jan_checklist_does_not_require_optional_or_default_fields():
    needs = R.manual_input_needs([{'型番': 'SKU', 'アイテム名': '盆栽'}])
    assert needs[0]['待手入力'] == 'HS / 通関英文名'


def test_auto_and_weight_note_rendered_in_display_language():
    """🤖 自動入力の重量条目・重量・寸法の取得メモは extra に原文で持ち、to_frame（描画）時の言語で出す。"""
    import re
    from shared.jan_web import WeightHit
    hit = WeightHit("4901234567894", 120.0, 10.0, 5.0, None, "netde", "https://x",
                    "卸の単品出荷単位（本体・ケース・内容量は不採用）")
    extras = {}
    for lang in ("zh", "ja"):                                    # enrich 時の言語
        with mock.patch("shared.i18n.get_lang", return_value=lang):
            nst, extra, _, _ = _enrich([_row("4901234567894")], want_weight=True, fw=lambda jan, **k: hit)
        extras[lang] = extra
        rec = R.to_frame(nst, extra)[0]                          # 描画は ja（autouse）
        assert rec[R.COL_AUTO] == ("アイテム名(jancode) · メーカー名(nst-prefix) · "
                                   "パッケージ高さ(cm)/パッケージ幅(cm)(netde) · 包装の既定仮値（非実測）")
        assert rec[R.COL_WEIGHT_NOTE] == ("卸の単品出荷単位（本体・ケース・内容量は不採用） · "
                                          "パッケージ奥行(cm) = 1 cm（既定の仮値・非実測）")
        with mock.patch("shared.i18n.get_lang", return_value="zh"):
            rec = R.to_frame(nst, extra)[0]
        assert not re.search(r"[ぁ-ゖァ-ヺ]", rec[R.COL_AUTO] + rec[R.COL_WEIGHT_NOTE]), rec
    k = "4901234567894"
    assert extras["zh"][k]["auto"] == extras["ja"][k]["auto"]
    assert extras["zh"][k]["weight_note"] == extras["ja"][k]["weight_note"]
