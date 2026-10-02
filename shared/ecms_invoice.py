"""ECMS 請求書の取り込み（xlsx 明細 + PDF 請求書ヘッダ）.

ECMS からは毎月 2 つ届く:
  · `YYYYMM LBF様輸送費.xlsx`  … sheet「運賃」に 1 行 = 1 発送（= 1 注文）の明細
  · `ご請求書_YYYYMM38LBF.pdf` … 請求書そのもの（費目ごとの合計・消費税・請求総額）

**両方要る**。xlsx だけでは請求総額に届かない:
  2026-09 実測  xlsx 明細合計 ¥774,364 = PDF「航空運賃」¥774,364（一致）
                PDF にはさらに「賠償 **-5,031**」があり、請求総額は ¥769,333。
  賠償（ECMS からの弁済）は xlsx に一切出てこないので、xlsx だけ見ると
  **¥5,031 多く費用計上してしまう**。

⚠️ PDF の落とし穴 2 つ（両方 実ファイルで踏んだ）:
  1. **月によってテキストの並びが変わる**。2026-05 は「1 航空運賃 776,092 課税対象外」が
     1 行、2026-09 は seq / 費目 / 金額 / 課税区分 が **それぞれ別行**。
     → 行で読まず、空白で割った token 列として走査する。
  2. **金額が負になる**（賠償・値引き）。`[\\d,]+` だけの正規表現は符号を取りこぼし、
     賠償行がまるごと消える。→ 先頭の `-` / `△` / `▲` を必ず拾う。

税: 航空運賃は **課税対象外**（国際輸送なので消費税がかからない）。よって xlsx の
`Total（JPY）` はそのまま税抜額として JD 側（不含税）と足し合わせられる。
返送貨物・追加請求のような国内費目は課税対象になることがある（2026-05 実績）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

# 請求書の費目 → cost_type。未知の費目は 'ecms_other' に寄せ、名前を note に残す。
ITEM_TO_COST_TYPE = {
    "航空運賃": "ecms_freight",
    "賠償": "ecms_compensation",
    "返送貨物": "ecms_return",
    "追加請求": "ecms_extra",
}
DEFAULT_COST_TYPE = "ecms_other"

# xlsx「運賃」の必須列。これが無ければ別フォーマットとして弾く。
XLSX_SHEET = "運賃"
XLSX_REQUIRED = ("Tracking No.", "Order No.", "Total（JPY）")
# 金額列 → cost_type。Total は内訳の合計なので取り込まない（二重計上になる）。
XLSX_AMOUNT_COLS = {
    "Freight（JPY）": "ecms_freight",
    "Fuel Surcharge（JPY）": "ecms_fuel",
    "Permit Fee（JPY）": "ecms_permit",
    "ESE care（JPY）": "ecms_ese_care",
    "AU SurCharge（JPY）": "ecms_au_surcharge",
    "Other（JPY）": "ecms_other",
}

_TAX_CLASSES = ("課税対象外", "課税対象")
_SEQ_RE = re.compile(r"^\d{1,2}$")
# 符号付き金額。全角マイナス・△・▲ も負数として扱う（和文請求書の慣習）
_AMOUNT_RE = re.compile(r"^[-−△▲]?[\d,]+$")


def _to_amount(tok: str) -> float:
    """'-5,031' / '△5,031' / '774,364' → float。負号の表記ゆれを吸収する。"""
    s = tok.strip()
    neg = s[:1] in ("-", "−", "△", "▲")
    if neg:
        s = s[1:]
    v = float(s.replace(",", ""))
    return -v if neg else v


@dataclass
class InvoiceLine:
    seq: int
    item_name: str
    amount: float
    tax_class: str

    @property
    def cost_type(self) -> str:
        return ITEM_TO_COST_TYPE.get(self.item_name, DEFAULT_COST_TYPE)


@dataclass
class InvoiceHeader:
    invoice_no: str = ""
    issue_date: dt.date | None = None
    lines: list[InvoiceLine] = field(default_factory=list)
    tax: float = 0.0
    total: float = 0.0           # 請求金額合計（税込・実際に払う額）
    warnings: list[str] = field(default_factory=list)

    @property
    def lines_total(self) -> float:
        return sum(ln.amount for ln in self.lines)


def _parse_jp_date(s: str) -> dt.date | None:
    m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_invoice_text(txt: str) -> InvoiceHeader:
    """請求書 PDF のテキスト → InvoiceHeader（純関数・PDF ライブラリ非依存）。

    行単位ではなく token 列として走査するので、月によって改行位置が変わっても拾える。
    明細は `<連番> <費目> <金額> <課税区分>` の並びを探す。
    """
    h = InvoiceHeader()
    m = re.search(r"請求書番号[:：]\s*([A-Za-z0-9]+)", txt)
    if m:
        h.invoice_no = m.group(1)
    m = re.search(r"発行日[:：]\s*(\S+?年\s*\d{1,2}月\s*\d{1,2}日)", txt)
    h.issue_date = _parse_jp_date(m.group(1)) if m else _parse_jp_date(txt)

    toks = [t for line in txt.splitlines() for t in line.split() if t.strip()]
    i = 0
    while i < len(toks) - 3:
        if _SEQ_RE.match(toks[i]):
            name, amt, tax = toks[i + 1], toks[i + 2], toks[i + 3]
            if (tax in _TAX_CLASSES and _AMOUNT_RE.match(amt)
                    and not _SEQ_RE.match(name) and not _AMOUNT_RE.match(name)):
                h.lines.append(InvoiceLine(int(toks[i]), name, _to_amount(amt), tax))
                i += 4
                continue
        i += 1

    m = re.search(r"消費税等[（(]10[％%][）)]\s*([-−△▲]?[\d,]+)", txt)
    if m:
        h.tax = _to_amount(m.group(1))
    # 請求金額合計 ¥対象外 ¥課税 ¥総額 … 最後の値が実際に払う額
    m = re.search(r"請求金額合計\s*¥\s*([-−△▲]?[\d,]+)\s*¥\s*([-−△▲]?[\d,]+)\s*¥\s*([-−△▲]?[\d,]+)", txt)
    if m:
        h.total = _to_amount(m.group(3))
    else:
        m = re.search(r"請求金額合計\s*¥\s*([-−△▲]?[\d,]+)", txt)
        if m:
            h.total = _to_amount(m.group(1))

    if not h.lines:
        h.warnings.append("明細行を 1 行も取れませんでした（PDF の書式が変わった可能性）")
    if h.total and abs(h.lines_total + h.tax - h.total) > 1:
        h.warnings.append(
            f"明細合計 {h.lines_total:,.0f} + 消費税 {h.tax:,.0f} が "
            f"請求金額合計 {h.total:,.0f} と一致しません")
    return h


def parse_invoice_pdf(data: bytes) -> InvoiceHeader:
    """PDF バイト列 → InvoiceHeader。PyMuPDF（依存に既存）でテキスト化するだけ。"""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        txt = "\n".join(p.get_text() for p in doc)
    return parse_invoice_text(txt)


def normalize_detail(df):
    """xlsx「運賃」の DataFrame → 取り込み用に正規化。

    返り値: (正規化済み DataFrame, warnings)
    列: order_no / tracking_no / n_pkg / <各費目> / total_jpy /
        chargeable_kg / gross_kg / volume_kg / length / width / height /
        destination / ship_date

    Order No. は**文字列のまま**扱う（先頭 0 や 'xxx.0' 化を避ける）。
    Total は内訳の合計なので検算にだけ使い、金額としては内訳側を採る。
    """
    import pandas as pd

    warns: list[str] = []
    missing = [c for c in XLSX_REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"必要な列がありません: {', '.join(missing)}")

    out = pd.DataFrame()
    out["order_no"] = df["Order No."].map(_clean_id)
    out["tracking_no"] = df["Tracking No."].map(_clean_id)
    out["n_pkg"] = pd.to_numeric(df.get("# Of PKG"), errors="coerce").fillna(0).astype(int)
    for col, key in XLSX_AMOUNT_COLS.items():
        out[key] = pd.to_numeric(df.get(col), errors="coerce").fillna(0.0)
    out["total_jpy"] = pd.to_numeric(df.get("Total（JPY）"), errors="coerce").fillna(0.0)

    # 列名に改行が入っている（'Chargeable Weight\n（kg）'）ので正規化して探す
    def _col(*keys):
        for c in df.columns:
            flat = " ".join(str(c).split())
            if all(k in flat for k in keys):
                return df[c]
        return None

    for key, keys in (("chargeable_kg", ("Chargeable", "Weight")),
                      ("gross_kg", ("Gross", "Weight")),
                      ("volume_kg", ("Volume", "Weight"))):
        out[key] = pd.to_numeric(_col(*keys), errors="coerce")
    for key, name in (("length", "Length"), ("width", "Width"), ("height", "Height")):
        out[key] = pd.to_numeric(df.get(name), errors="coerce")
    out["destination"] = df.get("Destination", "").astype(str).str.strip()
    out["ship_date"] = pd.to_datetime(df.get("Ship Date"), errors="coerce").dt.date

    # 内訳の合計が Total と合っているか（ECMS 側の計算ミス検知）
    parts = out[list(XLSX_AMOUNT_COLS.values())].sum(axis=1)
    bad = (parts - out["total_jpy"]).abs() > 0.01
    if bad.any():
        warns.append(f"内訳合計 ≠ Total の行が {int(bad.sum())} 件あります")

    blank = out["order_no"].eq("")
    if blank.any():
        warns.append(f"Order No. が空の行が {int(blank.sum())} 件（店舗に配賦できません）")
    dup = out["tracking_no"].duplicated() & out["tracking_no"].ne("")
    if dup.any():
        warns.append(f"Tracking No. 重複が {int(dup.sum())} 件あります")
    return out, warns


def _clean_id(v) -> str:
    """ID 列を文字列化。Excel 由来の '4901234567890.0' と前後空白を潰す。"""
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "nat", "none"):
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def year_month_from_name(filename: str) -> str:
    """ファイル名から請求対象月を拾う。'202609LBF様輸送費.xlsx' / 'ご請求書_20260938LBF.pdf' → '2026-09'。

    ⚠️ Ship Date からは決めない。2026-09 の請求書は 8/25〜9/28 出荷を含んでおり、
    出荷日で振ると同じ請求書が 2 ヶ月に割れて請求総額と突合できなくなる
    （JD 側も「請求対象月」で揃えている）。
    """
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])", str(filename))
    return f"{m.group(1)}-{m.group(2)}" if m else ""
