from shared import item_register as R
from tests.test_item_register import _enrich, _row


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
    assert out == nst
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
