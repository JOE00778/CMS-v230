"""tsca_cert.py — FedEx の TSCA Certification（Rev 1/17 書式）を PDF で作る.

Boss 2026-11-24：自建站の US 向け出荷で FedEx から求められる書式。
- 商品は注文の全行を載せる（「品名; qty N; JAN xxx」）
- 既定は Negative Certification に✓（画面で Positive に切替可）
- 証明人 XU YUE、署名は shared/assets/signature_xuyue.png
10 行を超えたら続紙を足し、表頭から繰り返す。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import fitz

COMPANY = "MITSUKIN SHOJI Co., Ltd."
ADDRESS = "3-1-35 Sekiden-machi, Oita-shi, Oita 870-0048, Japan"
CERTIFIER = "XU YUE"
TITLE = "Export Department Manager"
PHONE = "+81-70-9295-9651"
EMAIL = "jo@mitsukin.info"
SIGNATURE = Path(__file__).parent / "assets" / "signature_xuyue.png"
ROWS = 10
H, B = "helv", "hebo"
POS = ("I certify that all chemical substances in this shipment comply with all applicable rules or orders under "
       "TSCA and that I am not offering a chemical substance for entry in violation of TSCA or any applicable "
       "rule or order thereunder.")
NEG = "I certify that all chemicals in this shipment are not subject to TSCA."
FOOT = ("If the certifier is unsure if their chemical substance is subject to TSCA compliance, contact the "
        "Environmental Protection Agency, TSCA Assistance Office at 1.202.554.1404 between 8:30 a.m. and 5:00 p.m.")


def item_text(name: str, qty: int | float, jan: str) -> str:
    return f"{name}; qty {int(qty)}; JAN {jan}"


def _box(p, x, y, checked):
    p.draw_rect(fitz.Rect(x, y, x + 14, y + 14), width=0.8)
    if checked:
        p.draw_polyline([(x + 3, y + 7.5), (x + 6, y + 11), (x + 11.5, y + 3)], width=1.6)


def _page(doc, ship_date: date, awb: str, negative: bool, items: list[str], start: int, sign: bool):
    p = doc.new_page(width=595.28, height=841.89)
    p.insert_text((108, 47), "Toxic Substance Control Act (TSCA) Certification", fontname=H, fontsize=16)
    p.insert_text((47, 87), f"Date:  {ship_date.strftime('%B %d, %Y').replace(' 0', ' ')}", fontname=H, fontsize=10)
    p.insert_text((47, 112), f"Waybill or reference number:  {awb}", fontname=H, fontsize=10)
    p.insert_text((47, 155), "Check only one", fontname=B, fontsize=10)
    p.insert_text((47, 182), "Positive Certification", fontname=B, fontsize=10)
    _box(p, 55, 201, not negative)
    p.insert_textbox(fitz.Rect(82, 195, 548, 240), POS, fontname=H, fontsize=10)
    p.insert_text((82, 262), "or", fontname=H, fontsize=10)
    p.insert_text((47, 289), "Negative Certification", fontname=B, fontsize=10)
    _box(p, 55, 309, negative)
    p.insert_text((82, 318), NEG, fontname=H, fontsize=10)
    y = 358
    for k, v in (("Company name:", COMPANY), ("Company address:", ADDRESS), ("Certifier name:", CERTIFIER),
                 ("Certifier title:", TITLE), ("Certifier phone number:", PHONE),
                 ("Certifier email address:", EMAIL), ("Certifier signature:", "")):
        p.insert_text((47, y), k, fontname=H, fontsize=10)
        if v:
            p.insert_text((180, y), v, fontname=H, fontsize=10)
        y += 23
    if sign and SIGNATURE.exists():
        p.insert_image(fitz.Rect(150, y - 36, 260, y - 6), filename=str(SIGNATURE), keep_proportion=True)
    p.insert_text((47, y + 6), "Product description", fontname=H, fontsize=10)
    top, rh = y + 12, 22
    p.draw_rect(fitz.Rect(47, top, 548, top + rh * ROWS), width=0.8)
    for i in range(ROWS):
        ry = top + rh * i
        if i:
            p.draw_line((47, ry), (548, ry), width=0.8)
        n = start + i
        p.insert_text((52, ry + 14), f"{n + 1}.", fontname=H, fontsize=9)
        if n < len(items):
            t = items[n]
            fs = 8.5 if fitz.get_text_length(t, H, 8.5) <= 470 else max(5.5, 8.5 * 470 / fitz.get_text_length(t, H, 8.5))
            p.insert_text((70, ry + 14), t, fontname=H, fontsize=fs)
    p.insert_textbox(fitz.Rect(47, top + rh * ROWS + 12, 548, top + rh * ROWS + 50), FOOT, fontname=H, fontsize=9)
    p.insert_text((515, top + rh * ROWS + 62), "Rev 1/17", fontname=H, fontsize=7)


def build(ship_date: date, awb: str, items: list[str], negative: bool = True, sign: bool = True) -> bytes:
    doc = fitz.open()
    for start in range(0, max(len(items), 1), ROWS):
        _page(doc, ship_date, awb, negative, items, start, sign)
    return doc.tobytes()
