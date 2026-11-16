"""page49 采购处理 · 待确认の「📥 下载商品登录 Excel」— 勾选した画面でそのまま 1 回のクリックでダウンロード
（Boss 2026-11-16「生成商品登录EXCEL 下载不了」= 生成 → 下に出る 2 つ目のボタンが画面の外だった）。DB・網なし。"""
from __future__ import annotations

import contextlib
import pathlib
from datetime import datetime
from decimal import Decimal
from unittest import mock

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from shared import nst_choices as NC

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "49_🧾_采购处理.py"
SUP = "0457 カネイシ株式会社"
ITEMS = pd.DataFrame({"jan": ["4900000000011"], "item_code": ["A"], "display_name": ["商品A"], "tax_schedule": ["消費税10%"],
                      "order_lot": [12], "cost": [Decimal("900")], "last_purchase_cost": [None]})


def _req(i: int, jan: str, name: str | None) -> dict:
    return {"id": i, "jan": jan, "item_name": name, "qty": 12, "note": None, "requested_by": "刘颖",
            "requested_at": datetime(2026, 11, 16, 10), "status": "requested", "supplier": None,
            "unit_price": Decimal("130"), "confirmed_qty": None, "confirmed_by": None, "confirmed_at": None,
            "ordered_at": None, "ordered_by": None, "po_ext_id": None, "ref_no": None, "kind": "new"}


REQS = [_req(1, "4900000000011", None), _req(2, "4901301452061", "THE ANSWER シャンプー"),
        _req(3, "4977513075862", "マスターボール")]


@contextlib.contextmanager
def _conn():
    yield object()


def _run(select_all: bool):
    import shared.order_request_ui as UI
    from shared import new_item_request as NR
    real = UI.lang_stable_editor

    def editor(data, *, key, reset=None, **kw):     # 人が「选择」を付けた / 付けていない状態
        out = real(data, key=key, reset=reset, **kw)
        if key.endswith("_conf_editor"):
            out = out.copy()
            out["选择"] = select_all
        return out

    st.cache_data.clear()
    choices = NC.Choices(suppliers=(SUP,), middle_by_large=dict(NC.MIDDLE_BY_LARGE))
    with mock.patch("shared.auth.require_password", lambda: None), \
            mock.patch.object(UI, "get_connection", _conn), \
            mock.patch.object(UI, "load_suppliers", lambda c: [SUP]), \
            mock.patch.object(UI, "load_choices", lambda c: choices), \
            mock.patch.object(UI, "lang_stable_editor", editor), \
            mock.patch("shared.po_csv.load_item_master", lambda c: ITEMS), \
            mock.patch("shared.image_search.search_jan", lambda j: ""), \
            mock.patch.object(NR, "ensure_table", lambda c: None), \
            mock.patch.object(NR, "load_requests", lambda c: [dict(r) for r in REQS]), \
            mock.patch.object(NR, "load_po_lines", lambda c, j: []), \
            mock.patch.object(NR, "load_supplier_sources", lambda c, j: ({}, {})), \
            mock.patch.object(NR, "register_template", wraps=NR.register_template) as reg:
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__lark_user"] = {"name": "徐 越"}
        at.run()
    assert not at.exception, [e.value for e in at.exception]
    dl = {d.proto.id.rsplit("-", 1)[-1]: d.proto for d in at.get("download_button")}
    return at, dl, reg


def test_register_excel_downloads_in_one_click_20261116():
    at, dl, reg = _run(select_all=True)
    assert not [b for b in at.button if b.key and b.key.endswith("_reg_make")]     # 「生成」→ 2 つ目のボタン、の 2 段は無い
    b = dl["p49new_reg_dl"]                                                          # 勾选した同じ画面にダウンロードがある
    assert b.label == "📥 下载商品登录 Excel（2 件）" and not b.disabled and b.ignore_rerun
    rows, _, owner = reg.call_args.args                                             # 未登録の 2 件だけ・担当者は操作者
    assert [r["jan"] for r in rows] == ["4901301452061", "4977513075862"] and owner == "043 徐越"
    assert [r["name"] for r in rows] == ["THE ANSWER シャンプー", "マスターボール"]
    assert "p49restock_reg_dl" not in dl                                             # 日常补货は NST 登録済みだけ


def test_register_excel_disabled_without_unregistered_selection_20261116():
    _, dl, reg = _run(select_all=False)
    b = dl["p49new_reg_dl"]
    assert b.disabled and b.label == "📥 下载商品登录 Excel（0 件）" and reg.call_count == 0
