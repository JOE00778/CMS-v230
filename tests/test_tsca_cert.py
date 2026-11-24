"""TSCA 証明書（Boss 2026-11-24）：全行掲載・既定 Negative・10 行超で続紙。"""
from datetime import date

import fitz

from shared import tsca_cert as T


def test_build_pages_and_text():
    items = [T.item_text(f"Item {i}", 1, f"49000000000{i:02d}") for i in range(11)]
    d = fitz.open(stream=T.build(date(2026, 10, 29), "879096857025", items))
    assert len(d) == 2
    p1, p2 = d[0].get_text(), d[1].get_text()
    assert "October 29, 2026" in p1 and "879096857025" in p1 and "XU YUE" in p1
    assert "Item 9; qty 1; JAN 4900000000009" in p1 and "Item 10" in p2
    assert d[0].get_images()                     # 署名画像


def test_empty_still_one_page():
    assert len(fitz.open(stream=T.build(date(2026, 1, 1), "", []))) == 1
