"""invoice_ci.py — コマーシャル・インボイス（CI）PDF を作る（B 型・美国海关对应型）.

Boss 2026-10-25: FedEx 公式フォームは使わない欄が多すぎる → 3 案から **B** を採用。
19 CFR 141.86 が求める項目（売手・買手・品名・数量・単価・通貨・原産国・付帯費用・
入港地）だけを 1 枚に収める。FedEx は自社書式の CI を受け付ける（ETD ガイド）。

品名（Description of Goods）は**商品名ではなく清関用の英文品名**（Boss 同日）。
呼び出し側が PG logistics.item_customs.customs_name_en を CiLine.description に入れる。

明細が 1 枚に収まらなければ続紙を足し、表頭を繰り返す。合計・宣誓・署名は最終頁。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date

import fitz

EXPORTER = ["MITSUKIN SHOJI CO.,LTD", "1-4-14 Benten", "Oita 870-0017, JAPAN",
            "Tel: +81-97-574-9906", "Contact: JO ETSU"]
DECLARANT = "SHINICHI TANAKA"            # Boss 2026-10-25
TERMS = "DDP"                            # US は関税込み価格（Markets: INCLUDE_DUTIES_IN_PRICE）
DECLARATION = "I declare that all the information contained in this invoice is true and correct."
H, B = "helv", "hebo"
X0, X1 = 36, 576
PAGE_BOTTOM = 740                        # 明細をここまで詰める（最終頁は合計欄の分を引く）
TAIL_H = 190                             # 合計・宣誓・署名に要る高さ
SZ = 7.5
COLS = [("#", 18, "c"), ("Description of Goods", 228, "l"), ("HS Code", 60, "l"),
        ("Origin", 34, "c"), ("Qty", 30, "r"), ("Unit Value", 56, "r"),
        ("Total", 56, "r"), ("Net kg", 58, "r")]


@dataclass
class CiLine:
    description: str              # 清関用英文品名
    units: float
    net_weight_kg: float          # 1 個あたり
    hs_code: str
    origin: str                   # ISO2
    unit_value: float

    @property
    def total_value(self) -> float:
        return round(self.unit_value * self.units, 2)

    @property
    def line_net_kg(self) -> float:
        return self.net_weight_kg * self.units


@dataclass
class CiDoc:
    ship_date: date
    consignee_name: str
    consignee_addr: list[str]
    consignee_city: str
    consignee_country: str
    lines: list[CiLine]
    invoice_no: str = ""
    consignee_contact: str = ""
    consignee_tel: str = ""
    awb: str = ""
    total_packages: int = 1
    total_gross_kg: float | None = None   # 無ければ正味合計
    currency: str = "USD"
    extra: dict = field(default_factory=dict)

    @property
    def total_units(self) -> float:
        return sum(l.units for l in self.lines)

    @property
    def total_net_kg(self) -> float:
        return round(sum(l.line_net_kg for l in self.lines), 3)

    @property
    def invoice_total(self) -> float:
        return round(sum(l.total_value for l in self.lines), 2)


def _t(pg, x, y, s, sz=8.0, f=H, align="l"):
    if not s:
        return
    if align == "r":
        x -= fitz.get_text_length(s, f, sz)
    elif align == "c":
        x -= fitz.get_text_length(s, f, sz) / 2
    pg.insert_text((x, y), s, fontname=f, fontsize=sz)


def _wrap(s: str, width: float) -> list[str]:
    out: list[str] = []
    for w in (s or "").split():
        if out and fitz.get_text_length(f"{out[-1]} {w}", H, SZ) <= width:
            out[-1] += " " + w
        else:
            out.append(w)
    return out or [""]


def _xs():
    xs = [X0]
    for c in COLS:
        xs.append(xs[-1] + c[1])
    return xs


def _cell_x(xs, i, align):
    return xs[i] + 3 if align == "l" else (xs[i + 1] - 3 if align == "r" else (xs[i] + xs[i + 1]) / 2)


def _consignee(d: CiDoc) -> list[str]:
    return [x for x in (d.consignee_name, *d.consignee_addr, d.consignee_city,
                        d.consignee_country, f"Tel: {d.consignee_tel}" if d.consignee_tel else "") if x]


def _header(pg, d: CiDoc, page: int, pages: int) -> float:
    """1 頁目は全部、続紙は表題と番号だけ。戻り値は明細表の開始 y."""
    ship = d.ship_date.strftime("%d %b %Y")
    pg.draw_rect(fitz.Rect(X0, 36, X1, 70), width=0.8)
    _t(pg, 306, 58, "COMMERCIAL INVOICE", 15, B, "c")
    _t(pg, X1 - 6, 64, f"Page {page} of {pages}", 7, H, "r")
    if page > 1:
        _t(pg, X0, 86, f"Invoice No. {d.invoice_no}   ·   Date {ship}   ·   AWB {d.awb or '-'}   (continued)", 8)
        return 108
    y = 78
    box = [("Invoice No.", d.invoice_no), ("Invoice / Ship Date", ship),
           ("Air Waybill No.", d.awb or "-"), ("Terms of Sale", TERMS),
           ("Reason for Export", "Sale (e-commerce)"), ("Currency", d.currency),
           ("Port of Entry", "As designated by carrier (FedEx)"), ("Country of Export", "JAPAN")]
    for i, (k, v) in enumerate(box):
        cx, cy = X0 + (i % 2) * 270, y + (i // 2) * 15
        pg.draw_rect(fitz.Rect(cx, cy, cx + 270, cy + 15), width=0.4)
        _t(pg, cx + 4, cy + 10.5, k, 7, B)
        _t(pg, cx + 100, cy + 10.5, v, 8)
    y += 15 * 4 + 8
    for j, (title, ls) in enumerate([("EXPORTER (Seller)", EXPORTER),
                                     ("CONSIGNEE (Buyer / Sold To)", _consignee(d))]):
        cx = X0 + j * 270
        pg.draw_rect(fitz.Rect(cx, y, cx + 270, y + 92), width=0.4)
        _t(pg, cx + 4, y + 11, title, 7.5, B)
        for i, s in enumerate(ls[:7]):
            _t(pg, cx + 4, y + 24 + i * 10, s, 8.5)
    return y + 112


def _table_head(pg, y: float) -> float:
    xs = _xs()
    pg.draw_rect(fitz.Rect(X0, y - 9, X1, y + 4), color=None, fill=(0.92, 0.92, 0.92))
    for i, c in enumerate(COLS):
        _t(pg, _cell_x(xs, i, c[2]), y, c[0], SZ, B, c[2])
    return y + 6


def _row_h(l: CiLine) -> float:
    return 9 * len(_wrap(l.description, COLS[1][1] - 6)) + 4


def _paginate(d: CiDoc) -> list[list[tuple[int, CiLine]]]:
    pages, cur = [], []
    y = 264.0                                  # 1 頁目の明細開始 y（_header + _table_head）
    for n, l in enumerate(d.lines, 1):
        h = _row_h(l)
        if y + h > PAGE_BOTTOM and cur:
            pages.append(cur); cur = []; y = 114.0
        cur.append((n, l)); y += h
    pages.append(cur)
    # 最終頁に合計欄が入らなければ空の続紙を足す
    last_y = (264.0 if len(pages) == 1 else 114.0) + sum(_row_h(l) for _, l in pages[-1])
    if last_y + TAIL_H > 792 - 36:
        pages.append([])
    return pages


def _rows(pg, y: float, rows) -> float:
    xs = _xs()
    for n, l in rows:
        desc = _wrap(l.description, COLS[1][1] - 6)
        vals = [str(n), "", l.hs_code, l.origin, f"{l.units:g}", f"{l.unit_value:.2f}",
                f"{l.total_value:.2f}", f"{l.line_net_kg:.3f}"]
        for i, c in enumerate(COLS):
            if i == 1:
                for k, s in enumerate(desc):
                    _t(pg, _cell_x(xs, i, "l"), y + 9 + k * 9, s, SZ)
            else:
                _t(pg, _cell_x(xs, i, c[2]), y + 9, vals[i], SZ, H, c[2])
        y += 9 * len(desc) + 4
        pg.draw_line((X0, y), (X1, y), color=(0.75, 0.75, 0.75), width=0.4)
    return y


def _tail(pg, y: float, d: CiDoc) -> None:
    gross = d.total_gross_kg or d.total_net_kg
    y += 12
    _t(pg, X0, y, f"Packages: {d.total_packages}   Total Units: {d.total_units:g}   "
                  f"Net Wt: {d.total_net_kg:.3f} kg   Gross Wt: {gross:.2f} kg", 8)
    for k, v, bold in (("Goods Value", d.invoice_total, False), ("Freight", 0, False),
                       ("Insurance", 0, False), ("Other Charges", 0, False),
                       (f"INVOICE TOTAL {d.currency}", d.invoice_total, True)):
        f = B if bold else H
        _t(pg, X1 - 70, y, k, 8, f, "r"); _t(pg, X1 - 3, y, f"{v:.2f}", 8, f, "r"); y += 11
    _t(pg, X0, y + 6, "No rebates, drawbacks, bounties or assists apply to these goods.", 7.5)
    _t(pg, X0, y + 26, DECLARATION, 7.5)
    y += 50
    pg.draw_line((X0, y), (220, y), width=0.5); pg.draw_line((300, y), (420, y), width=0.5)
    _t(pg, X0, y + 9, f"Signature: {DECLARANT}", 7.5)
    _t(pg, 300, y + 9, f"Date: {d.ship_date.strftime('%d %b %Y')}", 7.5)


def build(d: CiDoc) -> bytes:
    pages = _paginate(d)
    doc = fitz.open()
    for i, rows in enumerate(pages, 1):
        pg = doc.new_page(width=612, height=792)
        y = _header(pg, d, i, len(pages))
        y = _rows(pg, _table_head(pg, y), rows) if rows or i == 1 else y
        if i == len(pages):
            _tail(pg, y, d)
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    return buf.getvalue()
