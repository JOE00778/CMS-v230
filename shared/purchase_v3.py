"""発注AI v3 · 可售天数基準（デスクトップ「订货助手」の規則を CMS へ移植）.

出典: Boss 提供『订货规则与数据说明』2026-09-15（~/Desktop）。
デスクトップ版は毎日 SKU CSV を手で取り込む方式だったが、v3 は SKU 側を全部
PG から引く（`nst.item_master_raw` / `nst.sales_daily` / `nst.inventory_snapshot`）。
固定仕入規則だけは CSV アップロード（Boss 2026-09-28 拍板：飞书同期はしない）。

既存の v2（`shared/purchase_engine.py`）とは**別物**なので共有しない:
  v2 = 月販トレンド × zone 優先 × 単価最安 の多仕入先決定版
  v3 = 可售天数 70/35 日 × 固定仕入先 1 社 × 箱規整数倍

このモジュールは**純関数のみ**（DB / Streamlit に依存しない）。
検証は tests/test_purchase_v3.py。原典 §18 の算例をそのままテストにしてある。

⚠️ デスクトップ版との既知の差 2 点:
  1. `可售天数` は原典では CSV の列をそのまま使うが、v3 は在庫 ÷ 日均で自前計算する。
     CSV 側の口径が違えば数字がずれる。
  2. 取扱中止の判定を**2 列**（item_rank + handling_cd）で見る（Boss 2026-09-28 拍板）。
     原典は「商品等级に取扱中止を含む」だけ。NST では item_rank（A/B/C/D 等級）と
     handling_cd（経営状態）は独立した列で、片方だけ見ると漏れる
     （2026-08 に 34 件の取扱中止品を誤って上架した実績あり）。v3 の方が厳しい。
"""
from __future__ import annotations

import math
import re

# 目標可售天数（原典 §3 / §4）
DEFAULT_TARGET_DAYS = 70
C_RANK_TARGET_DAYS = 35
# 1 回の補充は最大この日数分まで（原典 §5）
MAX_REFILL_DAYS = 50
# 発注後この日数を超えたら注意表示（原典 §13）
OVERSTOCK_WARN_DAYS = 75
# 日均販売の算出期間
SALES_WINDOW_DAYS = 30

# 取扱中止と見なす語。item_rank / handling_cd のどちらに入っていても弾く。
# purchase_engine.DISCONTINUED_VALUES と同じ語彙 + 原典の「取扱中止」。
DISCONTINUED_TOKENS = ("取扱中止", "取り扱い中止", "メーカー取扱中止", "取扱停止",
                       "廃盤", "廃番", "終売", "生産終了")

# C ランク判定（原典 §4: `C` / `Cランク` / `C等级`）。
# 「C」単体・接頭辞としての C のみ拾う。CC や Cancel 等を誤爆しないよう境界を付ける。
_C_RANK_RE = re.compile(r"^\s*C\s*(ランク|等级|等級|ランク評価)?\s*$", re.IGNORECASE)

STATUS_SUGGEST = "建议订货"
STATUS_PACK_EXCEPTION = "箱规例外"
STATUS_OVER_75 = "订货后超过75天"
STATUS_NO_PRICE = "缺单价"


def _num(v, default: float = 0.0) -> float:
    """数値化。空欄・変換不能は default。`¥` `￥` と桁区切りカンマを落とす（原典 §9.1）。"""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return default if isinstance(v, float) and math.isnan(v) else float(v)
    s = str(v).strip().replace("¥", "").replace("￥", "").replace(",", "")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def normalize_jan(v) -> str:
    """JAN は文字列として扱う（先頭 0 を消さない）。前後空白を落とすだけ。"""
    if v is None:
        return ""
    s = str(v).strip()
    # Excel 経由で 4901234567890.0 になっている場合の救済
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def is_discontinued(item_rank, handling_cd) -> bool:
    """取扱中止か。**2 列とも**見る（Boss 2026-09-28）。どちらかに該当語があれば真。"""
    blob = f"{item_rank or ''} {handling_cd or ''}"
    return any(tok in blob for tok in DISCONTINUED_TOKENS)


def target_days(item_rank) -> int:
    """目標可售天数。C ランクは 35 日、それ以外（A/B/NEW 等）は 70 日（原典 §11）。"""
    return C_RANK_TARGET_DAYS if _C_RANK_RE.match(str(item_rank or "")) \
        else DEFAULT_TARGET_DAYS


def decide_status(order_units: float, daily: float, order_days: float) -> str:
    """状態判定。原典 §13 の優先順位をそのまま写す（箱規例外 > 75 日超 > 通常）。"""
    if daily > 0 and order_units / daily > MAX_REFILL_DAYS:
        return STATUS_PACK_EXCEPTION
    if order_days > OVERSTOCK_WARN_DAYS:
        return STATUS_OVER_75
    return STATUS_SUGGEST


def compute_order(*, sold_30d, stock, transit, pack, price, item_rank) -> dict | None:
    """1 SKU 分の発注量計算（原典 §10 の式をそのまま）。発注不要なら None。

    Args:
        sold_30d: 前 30 日販売数（S30）
        stock:    現在庫（JDL 在庫）
        transit:  在途残（注文済・未入荷）
        pack:     起訂量 / 箱規。0 以下・空は呼び出し側で弾く前提（ここでは None を返す）
        price:    仕入単価。空でも数量は出す（金額だけ None · 原典 §16）
        item_rank: 商品等級（C 判定用）

    Returns:
        dict（daily / current_days / after_transit_days / boxes / order_units /
              order_days / amount / status）or None（発注不要 or 計算不能）
    """
    s30 = _num(sold_30d)
    if s30 <= 0:                      # 日均が出せない（原典 §3: 30 日販売 0 はスキップ）
        return None
    pack_n = _num(pack)
    if pack_n <= 0:                   # 箱規不明では整数倍にできない（原典 §16）
        return None

    daily = s30 / SALES_WINDOW_DAYS
    stock_n = _num(stock)
    transit_n = _num(transit)
    current_days = stock_n / daily
    after_transit_days = current_days + transit_n / daily

    tgt = target_days(item_rank)
    if after_transit_days >= tgt:     # 在途込みで足りている → 発注しない
        return None

    need_units = (tgt - after_transit_days) * daily
    max_units = MAX_REFILL_DAYS * daily
    raw_units = min(need_units, max_units)
    boxes = math.ceil(raw_units / pack_n)
    order_units = boxes * pack_n
    order_days = after_transit_days + order_units / daily

    price_n = _num(price, default=float("nan"))
    has_price = not math.isnan(price_n) and price_n > 0
    status = decide_status(order_units, daily, order_days)
    if not has_price:
        status = STATUS_NO_PRICE

    return {
        "daily": daily,
        "current_days": current_days,
        "after_transit_days": after_transit_days,
        "target_days": tgt,
        "need_units": need_units,
        "boxes": int(boxes),
        "order_units": order_units,
        "order_days": order_days,
        "amount": order_units * price_n if has_price else None,
        "status": status,
    }


def load_rules(records) -> tuple[dict, list[dict]]:
    """固定仕入規則（アップロード CSV の行）を JAN 辞書にする。

    Args:
        records: dict の列（vendor_name / jan / display_name / 起订量 / 价格）

    Returns:
        (rules, issues)
        rules  = {jan: {vendor_name, display_name, pack, price}}（pack/price は float|None）
        issues = 規則として使えない行（JAN 空 / JAN 重複 / 起訂量欠落）。
                 **黙って捨てない**（起訂量欠落は発注判定に届かず消えるため · 実データで 5 件）
    """
    rules: dict[str, dict] = {}
    issues: list[dict] = []
    for r in records:
        jan = normalize_jan(r.get("jan"))
        if not jan:
            issues.append({**r, "_issue": "JAN 空"})
            continue
        pack = _num(r.get("起订量"), default=0.0)
        price = _num(r.get("价格"), default=0.0)
        row = {
            "vendor_name": (r.get("vendor_name") or "").strip(),
            "display_name": (r.get("display_name") or "").strip(),
            "pack": pack if pack > 0 else None,
            "price": price if price > 0 else None,
        }
        if jan in rules:
            issues.append({**r, "_issue": "JAN 重複（先勝ち · 原典 §16 で一意が前提）"})
            continue
        rules[jan] = row
        if row["pack"] is None:
            issues.append({**r, "_issue": "起订量 欠落 → 整箱数が出せず発注建议に出ない"})
    return rules, issues


def build_recommendations(skus, rules) -> dict:
    """SKU 一覧 × 固定規則 → 3 つのバケツに振り分ける（原典 §9 のフロー順）。

    Args:
        skus: dict の列。必須キー: jan / display_name / maker / item_rank /
              handling_cd / sold_30d / stock / transit
        rules: load_rules() の第 1 戻り値

    Returns:
        {"orders": [...], "unmatched": [...], "rule_incomplete": [...],
         "skipped_discontinued": int, "no_need": int}

        orders           第 1 頁「订货建议」
        unmatched        第 2 頁「未匹配供应商」（規則に JAN が無い · 原典 §5.2）
        rule_incomplete  規則はあるが起訂量が無く計算不能（原典 §16 · 黙って消さない）
    """
    orders: list[dict] = []
    unmatched: list[dict] = []
    rule_incomplete: list[dict] = []
    skipped = 0
    no_need = 0

    for s in skus:
        # ① 取扱中止は仕入先照合より前に弾く（原典 §5.1 / §9.2）
        if is_discontinued(s.get("item_rank"), s.get("handling_cd")):
            skipped += 1
            continue

        jan = normalize_jan(s.get("jan"))
        rule = rules.get(jan)
        base = {
            "jan": jan,
            "display_name": s.get("display_name") or "",
            "maker": s.get("maker") or "",
            "item_rank": s.get("item_rank") or "",
            "handling_cd": s.get("handling_cd") or "",
            "stock": _num(s.get("stock")),
            "sold_30d": _num(s.get("sold_30d")),
            "transit": _num(s.get("transit")),
        }

        # ② 規則に無い → 第 2 頁へ。販売 0 でも捨てない（原典 §9.3 / §16）
        if rule is None:
            unmatched.append(base)
            continue

        # ③ 規則はあるが箱規が無い → 計算不能。第 1 頁にも第 2 頁にも出ないので別立て
        if rule["pack"] is None:
            rule_incomplete.append({**base, "vendor_name": rule["vendor_name"],
                                    "_issue": "起订量 欠落"})
            continue

        calc = compute_order(
            sold_30d=base["sold_30d"], stock=base["stock"], transit=base["transit"],
            pack=rule["pack"], price=rule["price"], item_rank=base["item_rank"])
        if calc is None:
            no_need += 1
            continue

        orders.append({**base,
                       "vendor_name": rule["vendor_name"],
                       "pack": rule["pack"],
                       "price": rule["price"],
                       **calc})

    orders.sort(key=lambda r: (r["vendor_name"], -(r["amount"] or 0)))
    return {"orders": orders, "unmatched": unmatched,
            "rule_incomplete": rule_incomplete,
            "skipped_discontinued": skipped, "no_need": no_need}
