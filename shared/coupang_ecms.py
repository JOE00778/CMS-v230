"""Coupang 注文 → ECMS 申告データへの変換（純関数のみ · DB も HTTP も触らない）。

換算・丸めの規則は**運営が現在使っている Excel の数式そのまま**（Boss 2026-08-30 に
「表格中有体现」と指摘され、`coupang通关文件.xlsx` の「JD 用发货文件」から抽出）:

    発票金額   = ROUND(paid amount KRW × 0.00068, 2)   ← USD、小数 2 桁
    通関類型   = IF(発票金額 >= 150, "2", "1")          ← USD 150 が免税枠の線
    重量       = ROUNDUP(単品重量 × 数量, 1)             ← kg、小数 1 桁を**切り上げ**
    数量       = MID(SKU, "_" の後) × Purchased qty     ← SKU は `JAN_入数`
    SKU        = LEFT(SKU, "_" の前) = JAN

⚠️ 0.00068 は固定係数（1 USD ≈ 1,470.6 KRW）で、実勢レートではない。運営の運用に
合わせて既定値にしてあるが、`COUPANG_KRW_USD_RATE` で差し替えられる。使ったレートは
毎回 queue に保存する（後から検証できるように）。

⚠️ 電話は `receiver.safeNumber`（0503-/0502- の安心番号）を**使わない**。通関には
`overseaShippingInfoDto.ordererPhoneNumber`（実番号）を使う——運営の Excel も
「通関用連絡先」列を参照している。
"""
from __future__ import annotations

import math
import os
import re

# 運営 Excel の固定係数。実勢レートではない
DEFAULT_KRW_USD = 0.00068
# 韓国の個人通関免税枠（USD）。これ以上は目録通関ではなく一般申告
DUTY_FREE_USD = 150.0


def fx_rate() -> float:
    """KRW → USD の係数。元川 .env の COUPANG_KRW_USD_RATE で上書き可。"""
    raw = os.environ.get("COUPANG_KRW_USD_RATE", "")
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_KRW_USD
    except ValueError:
        return DEFAULT_KRW_USD


def usd_from_krw(krw: float, rate: float | None = None) -> float:
    """ROUND(krw × rate, 2)。ECMS の Price.amount は Double(8,2)。"""
    return round(float(krw) * (rate if rate is not None else fx_rate()), 2)


def clearance_type(total_usd: float) -> str:
    """1 = 目録通関（$150 未満） / 2 = 一般申告（$150 以上）。"""
    return "2" if float(total_usd) >= DUTY_FREE_USD else "1"


def roundup_1(kg: float) -> float:
    """ROUNDUP(x, 1)。Excel と同じく小数 1 桁で**切り上げ**（切り捨てない）。"""
    return math.ceil(round(float(kg) * 10, 6)) / 10


def split_sku(code: str) -> tuple[str, int]:
    """`4573626220481_2` → ("4573626220481", 2)。"_" が無ければ入数 1。"""
    code = (code or "").strip()
    if "_" not in code:
        return code, 1
    jan, _, tail = code.partition("_")
    try:
        n = int(tail)
    except ValueError:
        return jan, 1
    return jan, max(1, n)


# ------------------------------------------------------------------
# 住所分割
# ------------------------------------------------------------------
# 広域自治体 17。2023-06 に 강원도 → 강원특별자치도、2024-01 に 전라북도 → 전북특별자치도
# へ改称しているが、Coupang のデータには旧称も出るので**両方受ける**。
_METRO = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시",
    "대전광역시", "울산광역시", "세종특별자치시",
)
_PROVINCE = (
    "경기도", "강원특별자치도", "강원도", "충청북도", "충청남도",
    "전북특별자치도", "전라북도", "전라남도", "경상북도", "경상남도",
    "제주특별자치도", "제주도",
)
# 略称 → 正式名。Coupang は「서울 강남구」のような略記も返す
_ALIAS = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시",
    "울산": "울산광역시", "세종": "세종특별자치시", "세종시": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "충북": "충청북도",
    "충남": "충청남도", "전북": "전북특별자치도", "전남": "전라남도",
    "경북": "경상북도", "경남": "경상남도", "제주": "제주특별자치도",
}
_ALL_SIDO = _METRO + _PROVINCE


def split_address(addr: str) -> tuple[str, str, str]:
    """韓国語住所を (시도, 시군구, 詳細) に分ける。

    運営が「地址分析」シートで手作業＋AI でやっている分割の規則版。実データに合わせて:
      · 広域市/特別市     → 시군구 は시도そのもの、残り全部が詳細
        「서울특별시 강동구 둔촌동 555」→ ("서울특별시", "서울특별시", "강동구 둔촌동 555")
      · 도                → 시군구 は「도 + 시/군」、구から先が詳細
        「경기도 수원시 권선구 호매실동」→ ("경기도", "경기도 수원시", "권선구 호매실동")

    先頭が既知の시도でなければ (「", "", 原文) を返す——推測で埋めない。画面で運営が直す。
    """
    s = re.sub(r"\s+", " ", (addr or "").strip())
    if not s:
        return "", "", ""
    parts = s.split(" ")
    head = parts[0]
    sido = _ALIAS.get(head) or (head if head in _ALL_SIDO else "")
    if not sido:
        return "", "", s

    rest = parts[1:]
    if sido in _METRO or _ALIAS.get(head) in _METRO:
        return sido, sido, " ".join(rest)

    # 도：次のトークンが 시 / 군 なら시군구に含める
    if rest and rest[0].endswith(("시", "군")):
        return sido, f"{sido} {rest[0]}", " ".join(rest[1:])
    return sido, sido, " ".join(rest)


# ------------------------------------------------------------------
# Coupang の箱 → queue 行
# ------------------------------------------------------------------
def pccc_of(box: dict) -> tuple[str, str]:
    """(PCCC, 種別)。通常の個人通関固有符号が無ければ一回限りのものを見る。"""
    o = box.get("overseaShippingInfoDto") or {}
    code = (o.get("personalCustomsClearanceCode") or "").strip()
    if code:
        return code, "normal"
    onetime = (o.get("oneTimePccc") or "").strip()
    return (onetime, "onetime") if onetime else ("", "")


def customs_phone(box: dict) -> str:
    """通関用の実番号。安心番号（safeNumber）は返さない。"""
    o = box.get("overseaShippingInfoDto") or {}
    phone = (o.get("ordererPhoneNumber") or "").strip()
    if phone:
        return phone
    # 실번호が開示されている場合のみ receiverNumber を使う（安心番号は使わない）
    r = box.get("receiver") or {}
    return (r.get("receiverNumber") or "").strip()


def build_items(box: dict, lookup) -> list[dict]:
    """orderItems → 申告明細。lookup(jan) は商品マスタ 1 行 or None を返す callable。"""
    rate = fx_rate()
    out = []
    for it in box.get("orderItems") or []:
        jan, pack = split_sku(it.get("externalVendorSkuCode") or "")
        shipped = int(it.get("shippingCount") or 0) - int(it.get("cancelCount") or 0)
        if shipped <= 0:
            continue
        qty = pack * shipped
        m = lookup(jan) or {}
        unit_g = m.get("weight_g")
        krw_total = float(it.get("salesPrice") or 0) * shipped
        out.append({
            "jan": jan,
            "name_en": m.get("name_en") or "",
            "hscode": m.get("hscode") or "",
            "url": m.get("url") or "",
            "pack": pack,
            "shipped": shipped,
            "qty": qty,
            "weight_kg": round(float(unit_g) / 1000 * qty, 4) if unit_g else None,
            "krw": krw_total,
            "price_usd": usd_from_krw(krw_total / qty, rate) if qty else 0.0,
            "total_usd": usd_from_krw(krw_total, rate),
        })
    return out


def to_queue_row(box: dict, lookup, pulled_at: str) -> dict:
    """Coupang の shipmentBox 1 件 → coupang_shipment_queue の 1 行。

    足りない項目（英語品名 / HS / 重量 / PCCC）は空のまま返す。**埋めない**——
    画面で赤く出して運営に直させる方が、勝手に補うより安全。
    """
    rate = fx_rate()
    r = box.get("receiver") or {}
    addr_full = " ".join(x for x in (r.get("addr1"), r.get("addr2")) if x).strip()
    sido, sigungu, detail = split_address(addr_full)
    items = build_items(box, lookup)
    pccc, kind = pccc_of(box)

    total_krw = sum(i["krw"] for i in items)
    weights = [i["weight_kg"] for i in items if i["weight_kg"] is not None]
    weight = roundup_1(sum(weights)) if len(weights) == len(items) and items else None

    return {
        "order_id": str(box.get("orderId") or ""),
        "shipment_box_id": str(box.get("shipmentBoxId") or ""),
        "ordered_at": box.get("orderedAt"),
        "coupang_status": box.get("status"),
        "receiver_name": (r.get("name") or "").strip(),
        "receiver_phone": customs_phone(box),
        "receiver_postcode": (r.get("postCode") or "").strip(),  # 前ゼロ保持のため文字列
        "receiver_addr": addr_full,
        "addr_sido": sido,
        "addr_sigungu": sigungu,
        "addr_detail": detail,
        "pccc": pccc,
        "pccc_kind": kind,
        "items": items,
        "total_krw": total_krw,
        "total_usd": usd_from_krw(total_krw, rate),
        "weight_kg": weight,
        "fx_rate": rate,
        "ecms_status": "pending",
        "pulled_at": pulled_at,
    }


def missing_fields(row: dict) -> list[str]:
    """ECMS へ出す前に必ず埋まっていないといけない項目。画面の赤表示用。"""
    miss = []
    for key, label in (("receiver_name", "収件人姓名"), ("receiver_phone", "通関用電話"),
                       ("receiver_postcode", "邮编"), ("addr_sido", "省/州"),
                       ("addr_sigungu", "城市"), ("addr_detail", "详细地址"),
                       ("pccc", "PCCC")):
        if not row.get(key):
            miss.append(label)
    if not row.get("weight_kg"):
        miss.append("重量（商品マスタ未登録）")
    for i, it in enumerate(row.get("items") or [], start=1):
        if not it.get("name_en"):
            miss.append(f"英語品名#{i}（JAN {it.get('jan')}）")
    if not row.get("items"):
        miss.append("申告明細")
    return miss
