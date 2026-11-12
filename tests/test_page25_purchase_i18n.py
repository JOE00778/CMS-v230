"""page25 発注AI v2: 理由 / 単位種別 / 全候補 は描画時に訳す。

zh で計算した結果（session の po_reco）を ja に切り替えて再描画すると、再計算なしで日文になる。
po_reco（= 全 SKU 推奨清单 CSV の元）は日文のまま変えない。
"""
from __future__ import annotations

import pathlib
import re
from unittest import mock

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from shared import purchase_engine as PE
from shared.i18n import TRANSLATIONS_ZH
from tests.unit import test_purchase_engine as TPE

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "25_📦_発注AI_v2.py"
KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
COLS = ("理由", "単位種別", "全候補")


def _conn():
    c = TPE._conn()
    TPE._seed_reason_branches(c)                 # 保留（最低受注未達）+ NEW
    TPE._seed_sales(c, "4900000000903", [10, 10, 10])
    TPE.seed_item(c, "4900000000903", display_name="商品Z", maker="メーカーZ", item_rank="Aランク", handling_cd="取扱中")
    c.execute("INSERT INTO supplier_quote VALUES ('仕入先丁','4900000000903','商品Z',100,NULL,NULL,0,'掛','2週間','EMERGENCY',3,'NST4')")
    c.execute("INSERT INTO supplier_quote VALUES ('仕入先戊','4900000000903','商品Z',120,NULL,NULL,0,'掛','2週間','PREPAID',4,'NST5')")
    return c                                     # ↑ ケース/ロット未設定 → needs_review → 仕入先別の全列表


def _shown(at, lang):
    names = {c: (TRANSLATIONS_ZH.get(c, c) if lang == "zh" else c) for c in COLS}
    out = {c: [] for c in COLS}
    for d in at.dataframe:
        for c, n in names.items():
            if n in d.value.columns:
                out[c] += d.value[n].astype(str).tolist()
    return out


def test_reason_follows_language_switch_without_recompute():
    st.cache_data.clear()
    df = PE.compute_recommendations(_conn(), months=3)
    before = df.copy()
    with mock.patch("shared.db.get_readonly_connection", side_effect=lambda *a, **k: _conn()):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.session_state["__role"] = "admin"
        at.session_state["lang"] = "zh"
        at.session_state["po_reco"] = df
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        zh = _shown(at, "zh")
        assert all(zh.values()), zh                                   # 3 列とも表示されている
        assert not [v for vs in zh.values() for v in vs if KANA.search(v)], zh

        at.query_params["lang"] = "ja"
        at.session_state["lang"] = "ja"
        at.run()                                                      # ボタンは押さない = 再計算なし
        assert not at.exception, [e.value for e in at.exception]
        ja = _shown(at, "ja")
        assert sorted(ja["理由"]) == sorted(df["reason"])            # 日文 = 保存値そのまま
        assert ja["単位種別"] == ["未設定"]
        assert ja["全候補"] == ["仕入先丁(応急)·¥100 / 仕入先戊(前払い)·¥120"]
    pd.testing.assert_frame_equal(at.session_state["po_reco"], before)  # CSV の元は日文のまま
