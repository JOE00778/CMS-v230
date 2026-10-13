"""No-JAN registration: independent item codes survive mixed imports and exports."""
import csv
import io
import pytest
from unittest.mock import MagicMock

import openpyxl
from data_warehouse.templates import item_entry_form as F, jd_bm_item_master as BM, nst_item_master as NST
from shared import item_register as R, nst_choices as NC
from shared.hs_classify import Customs
from shared.jan_web import JanMeta

SKUS = ['ETERNAL-BONSAI-' + c for c in ('REDN', 'NV', 'GR', 'WH', 'BK', 'LGR')]
JAN = '4936201110209'
CH = NC.Choices(suppliers=('0001 TEST',), middle_by_large={'生活雑貨': ('日用品雑貨',)})


def upload(records):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = F.SHEET_INPUT
    columns = [*F.INPUT_COLUMNS, F.COL_NAME_MANUAL, '型番', 'メーカー名（任意）']
    ws.append(columns)
    for values in records:
        r = {'JANコード': '', '商品原価': '100', '仕入先1：仕入先': '0001 TEST',
             '商品担当者': '043 徐越', '大分類': '生活雑貨', '中分類': '日用品雑貨',
             'カートン入数': '1', '発注ロット': '1', F.COL_NAME_MANUAL: '盆栽',
             'メーカー名（任意）': '手入力メーカー', **values}
        ws.append([r.get(c, '') for c in columns])
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def test_upload_accepts_six_no_jan_and_rejects_duplicate_codes_and_bad_jan():
    records = [{'型番': s} for s in SKUS] + [{'JANコード': JAN}, {'型番': SKUS[0]},
              {'型番': 'BAD', 'JANコード': '123'}, {'型番': 'NO-NAME', F.COL_NAME_MANUAL: ''}]
    rows, issues = F.read_upload(upload(records), CH)
    assert len(rows) == 7
    assert [r['型番'] for r in rows] == [*SKUS, JAN]
    assert {i.row for i in issues if i.level == 'error'} == {9, 10, 11}


@pytest.mark.parametrize("jan_code", [JAN, "CREAM-SKU"])
def test_no_jan_mixed_pipeline_preserves_identity_and_skips_barcode_queries(jan_code):
    rows, _ = F.read_upload(upload([{'型番': s} for s in SKUS] + [{'JANコード': JAN, '型番': jan_code}]), CH)
    meta, weight = MagicMock(return_value=JanMeta(JAN, 'クリーム', 'AUTO', '', 'ok')), MagicMock(return_value=None)
    nst, extra, issues, stats = R.enrich(rows, choices=CH, prefix_makers={}, fetch_meta=meta,
        fetch_weight=weight, classify_customs=lambda *a, **k: Customs(None, None, None, 'none'),
        want_weight=True, interval=0)
    assert len(nst) == 7 and set(extra) == {*SKUS, jan_code}
    assert meta.call_count == weight.call_count == 1
    assert meta.call_args.args == weight.call_args.args == (JAN,)
    assert all(r['メーカー名'] == '手入力メーカー' for r in nst)
    frame = R.to_frame(nst, extra)
    for i, r in enumerate(frame): r['アイテム名'] = f'編集 {i}'
    out, bm, sources, _ = R.from_frame(frame, nst, extra)
    assert [r['型番'] for r in out] == [*SKUS, jan_code]
    assert [r['アイテム名'] for r in out] == [f'編集 {i}' for i in range(7)]
    data = list(csv.DictReader(io.StringIO(NST.build_nst_master_csv(out, R.csv_field_columns(out), id_label='型番').decode('utf-8-sig'))))
    assert [r['JANコード'] for r in data] == [''] * 6 + [JAN]
    mapped = [dict(zip(BM.BM_HEADER, BM.nst_to_bm_row(r))) for r in bm]
    assert [r['SKU'] for r in mapped] == [*SKUS, jan_code]
    assert [r['SPU'] for r in mapped] == [*SKUS, jan_code]
    calls = []
    def ai(items):
        calls.extend(items); return {code: 'mock failure' for code, _, _ in items}
    R.cross_check_hs(out, extra, stats, ai)
    assert [x[0] for x in calls] == [*SKUS, jan_code]
    conn = MagicMock()
    assert R.write_register_log(conn, out, None, sources) == (7, '')
    sql, params = conn.executemany.call_args.args
    assert 'item_code' in sql
    assert [p[0] for p in params] == [*SKUS, jan_code]
    assert [p[1] for p in params] == [''] * 6 + [JAN]


def test_existing_item_codes_query_uses_itemid_and_pg_fallback():
    calls = []
    def query(sql, **kw): calls.append(sql); return [{'itemid': SKUS[0]}]
    found, warnings = NC.existing_item_codes(SKUS, suiteql=query)
    assert found == {SKUS[0]} and not warnings and 'itemid IN' in calls[0]
    conn = MagicMock(); conn.execute.return_value.fetchall.return_value = [{'item_code': SKUS[1]}]
    def failed(*a, **kw): raise RuntimeError('offline')
    found, warnings = NC.existing_item_codes(SKUS, suiteql=failed, conn=conn)
    assert found == {SKUS[1]} and warnings


def test_page15_no_jan_generation_and_existing_code_exclusion():
    import pathlib
    import zipfile
    from unittest.mock import patch
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from shared import jan_web, hs_ai
    st.cache_data.clear()
    up = MagicMock(); up.getvalue.return_value = upload([{'型番': s} for s in SKUS])
    conn = MagicMock(); conn.__enter__.return_value = conn
    with patch('shared.db.get_connection', return_value=conn), \
         patch('streamlit.file_uploader', return_value=up), \
         patch.object(NC, 'load_choices', return_value=CH), \
         patch.object(NC, 'load_prefix_makers', return_value={}), \
         patch.object(NC, 'existing_jans', return_value=(set(), [])), \
         patch.object(NC, 'existing_item_codes', return_value=({SKUS[0]}, [])), \
         patch.object(hs_ai, 'is_configured', return_value=False), \
         patch.object(jan_web, 'fetch_meta') as meta:
        page = pathlib.Path(__file__).resolve().parents[1] / 'pages' / '15_📝_商品登录.py'
        at = AppTest.from_file(str(page), default_timeout=30)
        at.session_state['__auth_ok'] = True
        at.run(); at.button(key='page15_run').click().run()
        assert not at.exception, [e.value for e in at.exception]
        assert [m.value for m in at.metric][:2] == ['5', '1']
        at.button(key='page15_gen').click().run()
        assert not at.exception, [e.value for e in at.exception]
        assert at.session_state['page15_zip_n'] == 5
        assert not meta.called
        with zipfile.ZipFile(io.BytesIO(at.session_state['page15_zip_bytes'])) as z:
            nst = next(n for n in z.namelist() if n.endswith('.csv'))
            rows = list(csv.DictReader(io.StringIO(z.read(nst).decode('utf-8-sig'))))
            assert [r['型番'] for r in rows] == SKUS[1:]
            bm_name = next(n for n in z.namelist() if n.endswith('.xlsx'))
            ws = openpyxl.load_workbook(io.BytesIO(z.read(bm_name)))['数据']
            assert [ws.cell(i, 13).value for i in range(3, 8)] == SKUS[1:]
