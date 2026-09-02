"""Coupang の受注 Excel → ECMS アップロード用 Excel。

Boss 2026-09-02「在 CMS 上做一个可转化的先，等 ECMS 的 API 对接环境准备好后再用 API 对接」。

**規則は推測ゼロ**。運営の実物 2 ファイル（`0902新订单.xlsx` → `0902ecms上传-新订单.xlsx`、
37 行）を突き合わせて確定させ、`tests/test_coupang_to_ecms_xlsx.py` で 37 行全部を
回帰テストにしてある。列の意味を変えるときはあのテストが落ちる。

固定値（37/37 一致を確認）:
    Client Code=LBF · Warehouse Code=NRT · Shipper Code=LBFVO
    Consignee Info Language=EN · Item Info Language=EN · Country=KR · Origin=JP
    ID Type=ID · Weight Unit=KG · Dangerous Type=N · **Currency=KRW**

⚠️ 通貨は **KRW**。運営の実ファイルが 37/37 すべて KRW で、単価も韓国ウォンのまま
（`paid amount ÷ 数量`）。USD 換算（× 0.00068）を使っているのは **JD 向けの別ライン**
であって、ECMS のアップロードではない。混同しないこと。

重量と品牌の出所（NST 商品マスタ = Excel の `cms0811` シート · CMS では PG から直接引く）:
    Item_Grossweight = round(毛重(g) ÷ 1000, 2)   ← **1 個あたり**。個数は掛けない
    Item_Brand       = 厂商
  実測で確認（`0902` の 37 行）: 重量 37/37 一致。品牌は 34/37 —— 残り 3 件は運営が
  メーカー名ではなくブランド名で出している（例: NST「コーセーコスメポート」→「CoenRich」）。
  自動では NST の厂商を入れ、画面で直せるようにしてある。
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from shared import kr_address

TEMPLATE = json.loads((Path(__file__).parent / "ecms_upload_template.json").read_text("utf-8"))
COLUMNS = [t["col"] for t in TEMPLATE]
HEADERS = [t["header"] for t in TEMPLATE]

CLIENT_CODE = "LBF"
WAREHOUSE_CODE = "NRT"
SHIPPER_CODE = "LBFVO"
CURRENCY = "KRW"          # ← USD ではない。§docstring 参照
REF_PREFIX = "EC" + CLIENT_CODE

# kr_address.ALIAS には「略称→正式名」と「改称前→改称後」が混ざっている。
# 運営は**改称前の名前をそのまま出している**（`전라북도` を `전북특별자치도` に直さない）
# ので、この 4 つは展開対象から外す。
RENAMED_SIDO = {"강원도", "전라북도", "제주도", "세종특별시"}

# Coupang の受注 Excel（Delivery シート）の列
C_ORDER_NO = "C"      # Order number
C_OPTION_NAME = "L"   # Registered option name → 規格型号
C_PRODUCT_ID = "N"    # Displayed product ID
C_OPTION_ID = "O"     # Option ID → Platform Id
C_SKU = "Q"           # Vendor product code = `JAN_入数`
C_PAID = "S"          # paid amount（KRW · 明細合計）
C_QTY = "W"           # Purchased qty
C_NAME = "AA"         # Recipient name
C_ZIP = "AC"          # Zipcode
C_ADDR = "AD"         # Recipient address
C_PCCC = "AJ"         # Personal Customs Clearance Code
C_PHONE = "AK"        # 通関用連絡先（安心番号ではない実番号）


def round_half_up(v: float, digits: int = 2) -> float:
    """Excel の ROUND と同じ丸め。

    Python 組み込みの `round()` は偶数丸めなので `round(0.015, 2)` が 0.01 になる。
    運営の実出力では NST 15g → 0.02（Excel の四捨五入）。ここを組み込み round で
    書くと 15g 級の小物だけ半分の重量で申告することになる。
    """
    return float(Decimal(str(v)).quantize(Decimal("1." + "0" * digits), rounding=ROUND_HALF_UP))


def clean_brand(maker: str) -> str:
    """NST の 厂商 → 申告用の品牌。全角括弧の中身（和名の併記）を落とす。

    `Pelican Soap（ペリカン石鹸）` → `Pelican Soap`
    `IDA Laboratories（井田ラボラトリー）` → `IDA Laboratories`

    ⚠️ これで揃うのは併記形だけ。`ユニリーバ`→`Dove`、`コーセーコスメポート`→`CoenRich`
    のようにブランド名で出しているものは運営の判断なので**自動では寄せない**。
    画面で直せるようにしてある。
    """
    return re.sub(r"[（(][^）)]*[）)]", "", maker or "").strip()


def split_sku(code: str) -> tuple[str, int]:
    """`4901616011007_3` → ("4901616011007", 3)。運営 Excel の J/K 列の数式と同じ。"""
    code = (code or "").strip()
    if "_" not in code:
        return code, 1
    jan, _, tail = code.partition("_")
    return (jan, int(tail)) if tail.isdigit() and len(tail) <= 2 else (jan, 1)


def province_city(addr: str) -> tuple[str, str, str]:
    """住所 → (省, 市, how)。運営の実出力 37 行に合わせた:

    · 省 = 先頭語。**略称は正式名に開く**（`서울` → `서울특별시`）が、
      **改称は原文のまま**（`전라북도` を `전북특별자치도` に直さない — 運営はそのまま出している）
    · 市 = 시군구の先頭語（`강서구` / `수원시`。`수원시 영통구` の구は市に入れない）
    · 住所欄には**原文をそのまま**入れる（分割した残りではない）

    判らなければ ("", "", 理由) を返す。埋めない。
    """
    s = re.sub(r"\s+", " ", (addr or "").strip())
    if not s:
        return "", "", "empty"
    sido, sgg, _detail, how = kr_address.split(s)
    first = s.split(" ")[0]
    if first in kr_address.SIDO or first in RENAMED_SIDO:
        province = first                      # 正式名・改称前ともそのまま
    else:
        province = kr_address.ALIAS.get(first, sido or "")   # 略称だけ開く
    city = (sgg or "").split(" ")[0] if sgg else ""
    if not province or not city:
        return province or "", city or "", how
    return province, city, how


def build_row(order: dict, product: dict | None, master: dict | None,
              ref_number: str) -> dict:
    """Coupang 1 行 → ECMS 1 行（列記号 → 値）。

    product: `coupang_product_info` を **SKU（`JAN_入数`）** で引いたもの
             （英語品名 / HScode / ProductID / OptionID）。同じ JAN でも規格違いは
             別 SKU・別 OptionID なので、JAN で引くと英語品名と URL が混ざる。
    master : NST 商品マスタを **JAN** で引いたもの（`maker` と `weight` g）。
             重量は JAN 単位なので、こちらは JAN で正しい。
    どちらも無ければ該当欄は空のまま返す（埋めない）。
    """
    product = product or {}
    master = master or {}
    jan, pack = split_sku(order.get(C_SKU, ""))
    try:
        bought = int(float(order.get(C_QTY) or 1))
    except ValueError:
        bought = 1
    qty = pack * bought

    try:
        paid = float(order.get(C_PAID) or 0)
    except ValueError:
        paid = 0.0
    # 単価は **整数ウォンに四捨五入**。実測: 34720÷3=11573.33→11573 /
    # 20600÷3=6866.67→**6867**（切り捨てではない）
    unit = int(round_half_up(paid / qty, 0)) if qty else 0

    # Item_Grossweight は **1 個あたり**の kg。内含個数も購入数も掛けない
    # （ECMS 側が Item_Quantity と掛け合わせる）。実測: NST 148g → 0.15 / 83g → 0.08 /
    # 757g → 0.76。運営の実出力 37/37 がこの式で再現できる。
    weight_g = master.get("weight")
    grossweight = round_half_up(float(weight_g) / 1000, 2) if weight_g else ""

    province, city, _how = province_city(order.get(C_ADDR, ""))

    # Product ID は商品マスタ側（注文の Displayed product ID とは別番号のことがある。
    # 実データ: 注文 8026156620 に対し運営の URL は 9544746544）。
    # 옵션 ID は**注文側が正**（マスタが古いことがある。実測 37/37 が注文の O 列と一致）。
    pid = product.get("product_id") or order.get(C_PRODUCT_ID, "")
    oid = order.get(C_OPTION_ID) or product.get("option_id", "")
    url = (f"https://www.coupang.com/vp/products/{pid}?vendorItemId={oid}"
           if pid and oid else "")

    return {
        "A": CLIENT_CODE,
        "B": order.get(C_ORDER_NO, ""),
        "C": ref_number,
        "L": WAREHOUSE_CODE,
        "Q": "EN",
        "R": order.get(C_NAME, ""),
        "S": order.get(C_PHONE, ""),
        "U": "KR",
        "V": province,
        "W": city,
        "X": order.get(C_ZIP, ""),
        "Y": order.get(C_ADDR, ""),
        "Z": "ID",
        "AA": order.get(C_PCCC, ""),
        "AC": "EN",
        # 商品マスタに brand があればそれ。無ければ NST の 厂商 から和名併記を落とす
        "AE": product.get("brand") or clean_brand(master.get("maker", "")),
        "AF": order.get(C_OPTION_NAME, ""),
        "AG": jan,
        "AH": product.get("name_en", ""),
        "AJ": grossweight,
        "AK": "KG",
        "AL": "N",
        # HSCode は運営の実ファイルが 37/37 とも**空**。商品マスタには入っているが
        # アップロードには載せていないので、それに合わせる。載せたくなったらここを
        # product.get("hscode", "") に戻す。
        "AM": "",
        "AN": url,
        "AO": qty,
        "AP": unit,
        "AR": CURRENCY,
        "AS": "JP",
        "AT": SHIPPER_CODE,
        "AU": oid,
    }


def ref_number(seq: int, on: date | None = None) -> str:
    """頭程運単号 `ECLBF` + yymmdd + 5 桁連番（運営の実出力と同じ形）。"""
    d = on or date.today()
    return f"{REF_PREFIX}{d:%y%m%d}{seq:05d}"


REQUIRED = {"B": "订单号", "R": "收货人姓名", "S": "收货人电话", "V": "省", "W": "市",
            "X": "邮编", "Y": "地址", "AA": "PCCC", "AE": "品牌", "AG": "SKU",
            "AH": "英文品名", "AJ": "毛重", "AO": "数量", "AP": "单价"}


def missing(row: dict) -> list[str]:
    """空だと ECMS に弾かれる欄。画面で赤く出して人が直す用。"""
    return [label for col, label in REQUIRED.items()
            if row.get(col) in (None, "", 0)]


def convert(orders: list[dict], products: dict, masters: dict,
            start_seq: int = 1, on: date | None = None) -> list[dict]:
    """Coupang の全行 → ECMS の全行。1 注文 1 行（実データも 37→37 の 1 対 1）。

    products は **SKU** キー、masters は **JAN** キー。
    """
    out = []
    for i, o in enumerate(orders):
        sku = (o.get(C_SKU) or "").strip()
        jan, _ = split_sku(sku)
        out.append(build_row(o, products.get(sku), masters.get(jan),
                             ref_number(start_seq + i, on)))
    return out


def to_xlsx(rows: list[dict], path: str | Path) -> Path:
    """ECMS のテンプレート（57 列・ヘッダ 1 行）で書き出す。

    郵便番号と SKU は**文字列**で入れる（`07531` の前ゼロが消えると住所照合に失敗する）。
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(HEADERS)
    text_cols = {"X", "AG", "B"}
    for r in rows:
        line = []
        for col in COLUMNS:
            v = r.get(col, "")
            line.append(str(v) if col in text_cols and v not in (None, "") else v)
        ws.append(line)
    for idx, col in enumerate(COLUMNS, start=1):
        if col in text_cols:
            for cell in ws.iter_cols(min_col=idx, max_col=idx, min_row=2):
                for c in cell:
                    c.number_format = "@"
    path = Path(path)
    wb.save(path)
    return path
