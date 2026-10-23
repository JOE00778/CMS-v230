"""invoice_ci.py — FedEx コマーシャル・インボイス（CI）の PDF を作る.

雛形は `shared/assets/fedex_ci_template.pdf`。FedEx 公式フォーム
（REV. 08-23-22 / clsd 2340）から注文ごとに変わる値だけを抜いたもので、
罫線・英語ラベル・日本語注記・輸出者ブロック・SOLD・USD・チェック欄の X は
そのまま焼き込んである。雛形の作り方は
database/data_warehouse/customs/make_ci_template.py。

座標・字号は全部、Boss が 2026-10-23 に送ってきた実物（注文 #1074）から実測した。
記入欄は 6pt、合計行は 7pt、明細行は 8pt と**元のフォームが混在している**ので
欄ごとに字号を持たせる。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import fitz

TEMPLATE = Path(__file__).with_name("assets") / "fedex_ci_template.pdf"
FONT = "helv"
SIZE_ITEM = 8.0

# 明細表の列境界（元 PDF のセル矩形から実測）
COL = [36, 67, 99, 134, 173, 334, 388, 435, 506, 576]
# 明細の値の揃え位置（実測。列境界からの逃げが列ごとに違う）
X_UNITS, X_NETW, X_UNITVAL, X_TOTVAL = 90.80, 132.00, 503.50, 574.00
TABLE = {0: (442.8, 559.5),    # 本紙: 表の上端 y, 下端 y
         1: (266.8, 744.0)}    # 続紙
LINE_H = 8.0                   # 説明が折り返したときの 2 行目以降の送り
ROW_PAD = 4.0                  # 行高 = LINE_H * 行数 + ROW_PAD（1 行なら 12pt）
BASE_OFF = 10.2                # 行の上端からベースラインまで
ADDR_STEP = 8.74               # 住所の 2 行目以降の送り

# 注文ごとに書き込む欄。(x, baseline_y, 揃え l/r/c, 字号)
P1 = {
    "ship_date":         (303.60, 105.77, "l", 6.0),
    "awb":               (299.05, 124.66, "l", 6.0),
    "consignee_contact": (202.23, 250.38, "l", 6.0),
    "consignee_tel":     (194.12, 260.10, "l", 6.0),
    "consignee_name":    (39.22, 289.67, "l", 6.0),
    "consignee_addr":    (39.22, 298.41, "l", 6.0),
    "consignee_city":    (39.30, 325.60, "l", 6.0),
    "consignee_country": (181.48, 336.92, "l", 6.0),
    "soldto_country":    (417.17, 327.68, "l", 6.0),
    "total_pkgs":        (53.70, 611.47, "r", 7.0),
    "total_units":       (91.60, 611.41, "r", 7.0),
    "total_net":         (136.90, 611.41, "r", 7.0),
    "total_gross":       (178.46, 612.00, "l", 6.0),   # 「2.60 KG」単位込み
    "subtotal":          (573.66, 596.21, "r", 6.0),
    "invoice_total":     (573.66, 699.83, "r", 6.0),
    "sign_date":         (538.46, 734.57, "l", 6.0),
    "page_cur":          (518.00, 69.00, "l", 8.0),
    "page_tot":          (557.00, 69.00, "l", 8.0),
}
P2 = {
    "awb":               (299.11, 108.85, "l", 6.0),
    "consignee_name":    (37.47, 174.24, "l", 6.0),
    "consignee_addr":    (38.11, 184.59, "l", 6.0),
    "consignee_city":    (38.11, 211.36, "l", 6.0),
    "consignee_country": (156.65, 220.15, "l", 6.0),
    "soldto_country":    (380.23, 220.61, "l", 6.0),
    "page_subtotal":     (571.00, 754.00, "r", 8.0),
    "page_cur":          (517.00, 81.10, "l", 8.0),
    "page_tot":          (552.00, 81.10, "l", 8.0),
}


@dataclass
class CiLine:
    description: str
    units: float
    net_weight_kg: float          # 1 個あたりの正味重量
    hs_code: str
    origin: str                   # ISO2
    unit_value: float

    @property
    def total_value(self) -> float:
        return round(self.unit_value * self.units, 2)

    @property
    def line_net_kg(self) -> float:
        return round(self.net_weight_kg * self.units, 2)


@dataclass
class CiDoc:
    ship_date: date
    consignee_name: str
    consignee_addr: list[str]
    consignee_city: str
    consignee_country: str
    lines: list[CiLine]
    consignee_contact: str = ""
    consignee_tel: str = ""
    awb: str = ""
    total_packages: int = 1
    total_gross_kg: float | None = None   # 無ければ正味重量の合計を使う
    sold_to_country: str = ""             # 空なら荷受人と同じ
    extra: dict = field(default_factory=dict)

    @property
    def total_units(self) -> float:
        return sum(l.units for l in self.lines)

    @property
    def total_net_kg(self) -> float:
        return round(sum(l.line_net_kg for l in self.lines), 2)

    @property
    def invoice_total(self) -> float:
        return round(sum(l.total_value for l in self.lines), 2)


def _wrap(text: str, width: float) -> list[str]:
    """説明欄の折り返し。単語境界で切り、1 語が長すぎるときだけ文字単位."""
    out: list[str] = []
    for word in (text or "").split():
        if out and fitz.get_text_length(f"{out[-1]} {word}", FONT, SIZE_ITEM) <= width:
            out[-1] = f"{out[-1]} {word}"
            continue
        while fitz.get_text_length(word, FONT, SIZE_ITEM) > width:
            cut = len(word)
            while cut > 1 and fitz.get_text_length(word[:cut], FONT, SIZE_ITEM) > width:
                cut -= 1
            out.append(word[:cut])
            word = word[cut:]
        out.append(word)
    return out or [""]


def _put(page, x: float, y: float, text: str, align: str = "l",
         size: float = SIZE_ITEM) -> None:
    if not text:
        return
    if align == "r":
        x -= fitz.get_text_length(text, FONT, size)
    elif align == "c":
        x -= fitz.get_text_length(text, FONT, size) / 2
    page.insert_text((x, y), text, fontname=FONT, fontsize=size)


def _field(page, pos: dict, key: str, text: str) -> None:
    if key not in pos:
        return
    x, y, align, size = pos[key]
    _put(page, x, y, text, align, size)


def _layout(lines: list[CiLine]) -> list[list[tuple[CiLine, list[str]]]]:
    """行を頁に割り振る。説明の折り返し行数で行高が変わる."""
    desc_w = COL[5] - COL[4] - 4
    pages: list[list[tuple[CiLine, list[str]]]] = []
    cur: list[tuple[CiLine, list[str]]] = []
    top, bottom = TABLE[0]
    y = top
    for ln in lines:
        wrapped = _wrap(ln.description, desc_w)
        h = LINE_H * len(wrapped) + ROW_PAD
        if y + h > bottom and cur:
            pages.append(cur)
            cur = []
            top, bottom = TABLE[1]
            y = top
        cur.append((ln, wrapped))
        y += h
    pages.append(cur)
    return pages


def _draw_table(page, rows: list[tuple[CiLine, list[str]]], top: float) -> None:
    y = top
    bounds = [y]
    for ln, wrapped in rows:
        base = y + BASE_OFF
        _put(page, X_UNITS, base, f"{ln.units:.2f}", "r")
        _put(page, X_NETW, base, f"{ln.line_net_kg:.2f}", "r")
        _put(page, (COL[3] + COL[4]) / 2, base, "PCS", "c")
        for i, w in enumerate(wrapped):
            _put(page, COL[4] + 2, base + LINE_H * i, w, "l")
        _put(page, COL[5] + 2, base, ln.hs_code or "", "l")
        _put(page, (COL[6] + COL[7]) / 2, base, ln.origin or "", "c")
        _put(page, X_UNITVAL, base, f"{ln.unit_value:.6f}", "r")
        _put(page, X_TOTVAL, base, f"{ln.total_value:.2f}", "r")
        y += LINE_H * len(wrapped) + ROW_PAD
        bounds.append(y)
    for by in bounds:
        page.draw_line((COL[0], by), (COL[-1], by), width=0.5)
    for cx in COL:
        page.draw_line((cx, bounds[0]), (cx, bounds[-1]), width=0.5)


def build(doc: CiDoc, template: Path = TEMPLATE) -> bytes:
    pages = _layout(doc.lines)
    tpl = fitz.open(template)
    out = fitz.open()
    out.insert_pdf(tpl, from_page=0, to_page=0)
    for _ in range(len(pages) - 1):
        out.insert_pdf(tpl, from_page=1, to_page=1)
    n = out.page_count
    sold_to = doc.sold_to_country or doc.consignee_country
    ship = doc.ship_date.strftime("%d %b, %Y").lstrip("0")

    for idx in range(n):
        page = out[idx]
        pos = P1 if idx == 0 else P2
        _field(page, pos, "awb", doc.awb)
        _field(page, pos, "consignee_name", doc.consignee_name)
        _field(page, pos, "consignee_city", doc.consignee_city)
        _field(page, pos, "consignee_country", doc.consignee_country)
        _field(page, pos, "soldto_country", sold_to)
        _field(page, pos, "page_cur", str(idx + 1))
        _field(page, pos, "page_tot", str(n))
        ax, ay, _al, asz = pos["consignee_addr"]
        for i, a in enumerate(doc.consignee_addr):
            _put(page, ax, ay + ADDR_STEP * i, a, "l", asz)
        if idx == 0:
            _field(page, P1, "ship_date", ship)
            _field(page, P1, "sign_date", ship)
            _field(page, P1, "consignee_contact", doc.consignee_contact)
            _field(page, P1, "consignee_tel", doc.consignee_tel)
            _field(page, P1, "total_pkgs", str(doc.total_packages))
            _field(page, P1, "total_units", f"{doc.total_units:.2f}")
            _field(page, P1, "total_net", f"{doc.total_net_kg:.2f}")
            gross = doc.total_gross_kg or doc.total_net_kg
            _field(page, P1, "total_gross", f"{gross:.2f} KG")
            _field(page, P1, "subtotal", f"{doc.invoice_total:.2f}")
            _field(page, P1, "invoice_total", f"{doc.invoice_total:.2f}")
        else:
            sub = round(sum(l.total_value for l, _ in pages[idx]), 2)
            _field(page, P2, "page_subtotal", f"{sub:.2f}")
        _draw_table(page, pages[idx], TABLE[0 if idx == 0 else 1][0])

    buf = io.BytesIO()
    out.save(buf, garbage=3, deflate=True)
    return buf.getvalue()
