"""供货商采购决策（仕入先比価）· 純ロジック（DB/Streamlit 不要）.

page 34「🏢 供货商管理」が使う。供货商报价(supplier_quote·append 留历史)から
JAN ごとに「最新报价」を取り、価格/納期/预付/起訂量 を綜合加权して最も合理的な
仕入先を判定する。閾値/权重は呼び出し側で可変。

データモデル（PG · sourcing schema · ページ側で idempotent 建表）:
  sourcing.supplier        供货商主档（起訂金額/納期/预付/启用）
  sourcing.supplier_quote  报价（supplier×jan×price×moq×lot×lead×quote_date·append）
最新报价 = (supplier, jan) ごとに quote_date 最大（同日は id 最大）。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Weights:
    """綜合加权（合計は任意·内部で正規化はしない·各項 0-1 メトリクスへ係数）。"""
    price: float = 0.70    # 価格（低いほど良）
    lead: float = 0.10     # 納期（短いほど良）
    prepay: float = 0.10   # 预付（現金払い=負担→ペナルティ）
    moq: float = 0.10      # 起訂量（多いほど柔軟性低→ペナルティ）


def latest_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """报价明細 → (supplier_name, jan) ごとの最新 1 行。

    必須列: supplier_name, jan, quote_date。id があれば同日 tie-break に使う。
    """
    if df.empty:
        return df
    d = df.copy()
    d["quote_date"] = pd.to_datetime(d["quote_date"], errors="coerce")
    _sort = ["quote_date"] + (["id"] if "id" in d.columns else [])
    d = d.sort_values(_sort)
    return (d.drop_duplicates(subset=["supplier_name", "jan"], keep="last")
            .reset_index(drop=True))


def _norm(series: pd.Series) -> pd.Series:
    """0(最良/最小)〜1(最悪/最大) に正規化。range=0 なら全て 0。欠損は中央 0.5。"""
    s = pd.to_numeric(series, errors="coerce")
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return s.notna().astype(float) * 0.0 + s.isna().astype(float) * 0.5
    out = (s - lo) / (hi - lo)
    return out.fillna(0.5)


def score_group(g: pd.DataFrame, w: Weights) -> pd.DataFrame:
    """1 つの JAN に属する各仕入先の最新报价群にスコア付け（低い=推奨）。

    必須列: price。任意: lead_days, is_prepay(bool), moq。
    戻り値: g + score 列 + is_recommended(最小スコア·tie は price 最小)。
    """
    g = g.copy()
    _price_n = _norm(g["price"])
    _lead_n = _norm(g["lead_days"]) if "lead_days" in g.columns else 0.0
    _moq_n = _norm(g["moq"]) if "moq" in g.columns else 0.0
    if "is_prepay" in g.columns:
        _prepay_n = g["is_prepay"].fillna(False).astype(bool).astype(float)
    else:
        _prepay_n = 0.0
    g["score"] = (w.price * _price_n + w.lead * _lead_n
                  + w.prepay * _prepay_n + w.moq * _moq_n)
    # 推奨 = score 最小（tie は price 最小 → さらに tie は先頭）
    g = g.sort_values(["score", "price"], na_position="last")
    g["is_recommended"] = False
    if not g.empty:
        g.iloc[0, g.columns.get_loc("is_recommended")] = True
    return g


def recommend(latest: pd.DataFrame, w: Weights) -> pd.DataFrame:
    """最新报价(全 JAN) → JAN ごとにスコア付け + 推奨フラグ。"""
    if latest.empty:
        return latest
    parts = [score_group(g, w) for _, g in latest.groupby("jan", sort=False)]
    return pd.concat(parts, ignore_index=True)


# 上传报价表 → 标准列。よくある列名のエイリアスを吸収。
_ALIASES = {
    "supplier_name": ["supplier", "供货商", "仕入先", "メイン仕入先", "供应商", "vendor"],
    "jan": ["jan", "JAN", "本地SKU", "商品コード", "sku", "商品编码"],
    "item_name": ["item_name", "商品名", "display_name", "品名"],
    "price": ["price", "采购价", "仕入金額", "メイン仕入金額", "単価", "最安値", "実績原価"],
    "moq": ["moq", "起订量", "起訂量", "最低订量", "min_qty"],
    "order_lot": ["order_lot", "発注ロット", "発注lot", "ロット", "发注ロット", "lot"],
    "lead_days": ["lead_days", "納期", "纳期", "lead"],
}


def normalize_upload(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """上传 DataFrame の列名を標準スキーマへ寄せる。

    戻り値: (標準列だけの DataFrame, 解決できなかった必須列リスト)。
    必須 = supplier_name, jan, price。
    """
    df = df.rename(columns=lambda c: str(c).strip())
    lower = {str(c).strip().lower(): c for c in df.columns}
    out = pd.DataFrame()
    for std, aliases in _ALIASES.items():
        hit = None
        for a in [std] + aliases:
            if a in df.columns:
                hit = a
                break
            if a.lower() in lower:
                hit = lower[a.lower()]
                break
        if hit is not None:
            out[std] = df[hit]
    missing = [c for c in ("supplier_name", "jan", "price") if c not in out.columns]
    return out, missing


# ============================================================
# 仕入先管理リスト.xlsx 多供货商一括抽取
#   各仕入先 sheet（1 sheet=1 仕入先）: JAN / 商品名 / 見積価格 / ロット / 注文最低金額。
#   data sheet（集計/マスタ）は除外。Boss 2026-06-22。
# ============================================================
import re as _re

# 価格は優先度順（御見積=実報価 > 見積 > 単価 > 卸 > 納価 > 価格 > 希望納価=目標値は最後）
_PRICE_KW = ["御見積", "見積", "単価", "卸", "納価", "価格", "希望納価"]
_LOT_KW = ["発注ロット", "ロット", "ケース入数", "入数"]
_MIN_KW = ["注文最低", "最低金額", "起訂", "最低発注"]
_NAME_KW = ["商品名", "品名", "display"]
_JAN_HDR = ["jan", "本地sku", "商品コード", "商品コ-ド", "sku"]
_JAN_RE = _re.compile(r"^\d{8,13}$")

# 报价ではない集計/マスタ sheet（仕入先扱いしない）
DATA_SHEETS = {
    "仕入先-原価_AB類", "仕入先_原価_C類", "AB商品进货周期", "SUPABASE用",
}
_DATA_PREFIX = ("FB_アイテム別売上", "一元 Data", "NEW WIND ")  # 前方一致で除外するもの


def _find_col(header: list[str], kws: list[str]):
    """header(小写化済み list)から kws 優先度順で最初にヒットした列 index。無→None。"""
    for kw in kws:
        for i, h in enumerate(header):
            if kw in h:
                return i
    return None


def parse_vendor_sheet(supplier_name: str, raw: pd.DataFrame) -> pd.DataFrame:
    """1 仕入先 sheet（header=None で読んだ生 DataFrame）→ 标准报价行。

    戻り値列: supplier_name, jan, item_name, price, order_lot, min_order_amount。
    抽けなければ空 DataFrame。
    """
    _cols = ["supplier_name", "jan", "item_name", "price", "order_lot", "min_order_amount"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_cols)
    # 表头行 = 「商品名」を含む最初の行（無ければ 0 行目）
    hr = 0
    for r in range(min(8, len(raw))):
        if any("商品名" in str(x) for x in raw.iloc[r].tolist()):
            hr = r
            break
    header = [str(x).strip().lower() for x in raw.iloc[hr].tolist()]
    body = raw.iloc[hr + 1:]
    # JAN 列：表头名优先、なければデータが JAN パターンの列
    jan_col = _find_col(header, _JAN_HDR)
    if jan_col is None:
        for ci in range(min(5, raw.shape[1])):
            vals = body.iloc[:, ci].dropna().astype(str).str.strip()
            if len(vals) and vals.str.match(_JAN_RE).mean() > 0.5:
                jan_col = ci
                break
    price_col = _find_col(header, _PRICE_KW)
    if jan_col is None or price_col is None:
        return pd.DataFrame(columns=_cols)
    name_col = _find_col(header, _NAME_KW)
    lot_col = _find_col(header, _LOT_KW)
    min_col = _find_col(header, _MIN_KW)

    rows = []
    for _, r in body.iterrows():
        jan = str(r.iloc[jan_col]).strip() if jan_col < len(r) else ""
        if not _JAN_RE.match(jan):
            continue
        price = parse_peso_like(r.iloc[price_col]) if price_col < len(r) else None
        if price is None:
            continue
        rows.append({
            "supplier_name": supplier_name,
            "jan": jan,
            "item_name": (str(r.iloc[name_col]).strip()
                          if name_col is not None and name_col < len(r) else None),
            "price": price,
            "order_lot": parse_peso_like(r.iloc[lot_col]) if lot_col is not None and lot_col < len(r) else None,
            "min_order_amount": (parse_peso_like(r.iloc[min_col])
                                 if min_col is not None and min_col < len(r) else None),
        })
    out = pd.DataFrame(rows, columns=_cols)
    if not out.empty:
        out = out.drop_duplicates(subset=["jan"], keep="first").reset_index(drop=True)
    return out


def parse_peso_like(v):
    """'1,001' / '￥680' / 680 / '' → float。異常→None。（数値清洗·汎用）"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _re.sub(r"[^\d.\-]", "", str(v))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compare_wide(latest: pd.DataFrame) -> pd.DataFrame:
    """最新报价(jan, supplier_name, price) → 横持ち比価表。

    index リセット済 · 列: jan, min_price(最低価), cheapest_supplier(最安仕入先),
    その後に各 supplier の価格列。報価が無い JAN は含まれない。
    """
    cols0 = ["jan", "min_price", "cheapest_supplier"]
    if latest is None or latest.empty:
        return pd.DataFrame(columns=cols0)
    d = latest.copy()
    d["price"] = pd.to_numeric(d["price"], errors="coerce")
    d = d.dropna(subset=["price"])
    if d.empty:
        return pd.DataFrame(columns=cols0)
    piv = d.pivot_table(index="jan", columns="supplier_name", values="price", aggfunc="min")
    piv = piv.dropna(axis=1, how="all")
    _minp = piv.min(axis=1)
    _cheap = piv.idxmin(axis=1)
    piv.insert(0, "min_price", _minp)
    piv.insert(1, "cheapest_supplier", _cheap)
    return piv.reset_index()


def is_data_sheet(name: str) -> bool:
    return name in DATA_SHEETS or any(name.startswith(p) for p in _DATA_PREFIX)


def apply_supplier_alias(df: pd.DataFrame, mapping: dict,
                         col: str = "supplier_name") -> pd.DataFrame:
    """供货商名 → 別名表で正規化（NST 長名「0474 株式会社　五洲」→ Boss 短名「五洲」）。

    mapping = {alias: canonical}。未登録名はそのまま。df は破壊しない。
    """
    if df is None or df.empty or not mapping or col not in df.columns:
        return df
    d = df.copy()
    d[col] = d[col].map(lambda s: mapping.get(str(s).strip(), s))
    return d


def extract_main_suppliers(raw: pd.DataFrame) -> pd.DataFrame:
    """「仕入先別_系列別_免送料判定_明細付」の ③SKU明細 sheet → 主供货商指定行。

    入力: header=None で読んだ生 DataFrame。表头行 =「仕入先」と「JAN」を含む行。
    「■ 五洲…」の様な区切り行 / JAN 非合规行はスキップ。JAN 重複は先勝ち。
    戻り値列: supplier_name, jan, item_name, price（単価·無→None）。
    """
    _cols = ["supplier_name", "jan", "item_name", "price"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_cols)
    hr = None
    for r in range(min(8, len(raw))):
        vals = [str(x).strip() for x in raw.iloc[r].tolist()]
        if "仕入先" in vals and any(v.upper() == "JAN" for v in vals):
            hr = r
            break
    if hr is None:
        return pd.DataFrame(columns=_cols)
    header = [str(x).strip() for x in raw.iloc[hr].tolist()]
    sup_col = header.index("仕入先")
    jan_col = next(i for i, h in enumerate(header) if h.upper() == "JAN")
    name_col = _find_col([h.lower() for h in header], _NAME_KW)
    price_col = next((i for i, h in enumerate(header) if h == "単価"), None)

    rows = []
    for _, r in raw.iloc[hr + 1:].iterrows():
        sup = str(r.iloc[sup_col]).strip() if sup_col < len(r) else ""
        jan = str(r.iloc[jan_col]).strip() if jan_col < len(r) else ""
        jan = jan[:-2] if jan.endswith(".0") else jan
        if not sup or sup.startswith("■") or sup.lower() == "nan" or not _JAN_RE.match(jan):
            continue
        rows.append({
            "supplier_name": sup,
            "jan": jan,
            "item_name": (str(r.iloc[name_col]).strip()
                          if name_col is not None and name_col < len(r) else None),
            "price": (parse_peso_like(r.iloc[price_col])
                      if price_col is not None and price_col < len(r) else None),
        })
    out = pd.DataFrame(rows, columns=_cols)
    if not out.empty:
        out = out.drop_duplicates(subset=["jan"], keep="first").reset_index(drop=True)
    return out


# ============================================================
# 折扣率（仕入折数）分析 · 供货商管理 v2（Boss 2026-07-15）
#   wari = 価格 ÷ MSRP税抜 × 10（7.0 = 7掛）。MSRP 源 = nst.item_msrp。
#   従来 page34 の tab_opt / tab_brand にインライン重複していた式をここへ集約。
# ============================================================

def msrp_taxex(msrp_jpy: pd.Series, msrp_jpy_taxin: pd.Series) -> pd.Series:
    """税抜MSRP = COALESCE(msrp_jpy_taxin, msrp_jpy) ÷ 1.1（税率10%）.

    nst.item_msrp.msrp_jpy_taxin は「税込換算」列（元が税抜なら ×1.1 済）のため、
    ÷1.1 で元税抜値に往復一致する（見直し必要 tab と同口径 · 97b4df3）。
    """
    a = pd.to_numeric(msrp_jpy_taxin, errors="coerce")
    b = pd.to_numeric(msrp_jpy, errors="coerce")
    return a.fillna(b) / 1.1


def wari(price: pd.Series, msrp_ex: pd.Series) -> pd.Series:
    """仕入折数 = price ÷ msrp_ex × 10。msrp_ex <= 0 / 欠損 → NaN。"""
    p = pd.to_numeric(price, errors="coerce")
    m = pd.to_numeric(msrp_ex, errors="coerce")
    return p / m.where(m > 0) * 10


def monthly_weighted_wari(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """月度金額加重折扣率（グループ×月）。

    入力列: ym, rate(PO 税抜単価), quantity, msrp_taxex（NaN 可）+ group_cols
            （jan 列があれば sku_n を数える）。
    出力列: group_cols + ym, wari_w, amount, amount_msrp, coverage, sku_n。
      wari_w   = Σ(rate×qty) ÷ Σ(msrp_taxex×qty) × 10（MSRP 有り行のみで加重）
      amount   = 全行の Σ(rate×qty)（MSRP 無し行も含む採購実額）
      coverage = amount_msrp ÷ amount（折数の代表性判断用）
    group_cols=[] は全体口径（ym ごと 1 行）。
    """
    out_cols = list(group_cols) + ["ym", "wari_w", "amount",
                                   "amount_msrp", "coverage", "sku_n"]
    if df is None or df.empty:
        return pd.DataFrame(columns=out_cols)
    d = df.copy()
    for c in ("rate", "quantity", "msrp_taxex"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["rate", "quantity"])
    d = d[d["quantity"] > 0]
    if d.empty:
        return pd.DataFrame(columns=out_cols)
    d["_amt"] = d["rate"] * d["quantity"]
    _has = d["msrp_taxex"] > 0
    d["_amt_m"] = d["_amt"].where(_has, 0.0)
    d["_msrp_amt"] = (d["msrp_taxex"] * d["quantity"]).where(_has, 0.0)
    keys = list(group_cols) + ["ym"]
    grouped = d.groupby(keys, dropna=False)
    g = grouped.agg(amount=("_amt", "sum"), amount_msrp=("_amt_m", "sum"),
                    _msrp_amt=("_msrp_amt", "sum")).reset_index()
    if "jan" in d.columns:
        g = g.merge(grouped["jan"].nunique().rename("sku_n").reset_index(), on=keys)
    else:
        g["sku_n"] = pd.NA
    g["wari_w"] = (g["amount_msrp"] / g["_msrp_amt"].where(g["_msrp_amt"] > 0)) * 10
    g["coverage"] = g["amount_msrp"] / g["amount"].where(g["amount"] > 0)
    return g[out_cols]


# ============================================================
# 报价有效性 / 最低有效报价（供货商管理 v2 第一期 · spec 2026-07-15）
#   節省測算に参加できるのは「有効」報価のみ。有効期間未設定の歴史報価は
#   参加させるが「有效性未确认」と明示（spec: 静默按默认值计算は禁止）。
# ============================================================

VALIDITY_LABELS = {
    "valid": "有效", "unconfirmed": "有效性未确认", "expired": "报价过期",
    "not_yet": "未生效", "incomplete": "数据不足", "inactive": "供应商停用",
}


def quote_status(latest: pd.DataFrame, active: dict | None = None,
                 today: "pd.Timestamp | None" = None) -> pd.DataFrame:
    """最新报价へ有効性判定列を付与。

    latest: latest_quotes() 出力（supplier_name, jan, price, quote_date,
            任意列 valid_from / valid_to）。active: {supplier_name: bool}（None=全て啓用）。
    追加列:
      validity ∈ inactive / incomplete / expired / not_yet / valid / unconfirmed
        - incomplete   = price か quote_date 欠損
        - expired      = valid_to < today
        - not_yet      = valid_from > today
        - valid        = 有効期間内（両端 NULL 可の判定後）
        - unconfirmed  = valid_from も valid_to も未設定（歴史報価 · 参加可だが明示）
      eligible = validity ∈ {valid, unconfirmed} かつ供货商啓用 → 節省測算に参加
    """
    cols = ["validity", "eligible"]
    if latest is None or latest.empty:
        out = (latest.copy() if latest is not None else pd.DataFrame())
        for c in cols:
            out[c] = pd.Series(dtype="object" if c == "validity" else "bool")
        return out
    d = latest.copy()
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.today()
    today = today.normalize()
    price = pd.to_numeric(d.get("price"), errors="coerce")
    qdate = pd.to_datetime(d.get("quote_date"), errors="coerce")
    vfrom = pd.to_datetime(d["valid_from"], errors="coerce") if "valid_from" in d.columns \
        else pd.Series(pd.NaT, index=d.index)
    vto = pd.to_datetime(d["valid_to"], errors="coerce") if "valid_to" in d.columns \
        else pd.Series(pd.NaT, index=d.index)
    if active:
        is_active = d["supplier_name"].map(
            lambda s: bool(active.get(str(s).strip(), True)))
    else:
        is_active = pd.Series(True, index=d.index)

    validity = pd.Series("valid", index=d.index, dtype="object")
    validity[vfrom.isna() & vto.isna()] = "unconfirmed"
    validity[vfrom.notna() & (vfrom > today)] = "not_yet"
    validity[vto.notna() & (vto < today)] = "expired"
    validity[price.isna() | qdate.isna()] = "incomplete"
    validity[~is_active] = "inactive"
    d["validity"] = validity
    d["eligible"] = validity.isin(["valid", "unconfirmed"])
    return d


def best_effective_quote(status: pd.DataFrame) -> pd.DataFrame:
    """quote_status 出力 → JAN ごとの最低有效报价。

    出力列: jan, best_supplier, best_price, best_validity,
            n_eligible(参加供货商数)。eligible 行の無い JAN は含まれない。
    tie-break: price → supplier_name（決定的）。
    """
    cols = ["jan", "best_supplier", "best_price", "best_validity", "n_eligible"]
    if status is None or status.empty or "eligible" not in status.columns:
        return pd.DataFrame(columns=cols)
    d = status[status["eligible"]].copy()
    d["price"] = pd.to_numeric(d["price"], errors="coerce")
    d = d.dropna(subset=["price"])
    if d.empty:
        return pd.DataFrame(columns=cols)
    d = d.sort_values(["jan", "price", "supplier_name"])
    n = d.groupby("jan")["supplier_name"].nunique().rename("n_eligible")
    best = d.drop_duplicates("jan", keep="first")
    best = best.merge(n, on="jan")
    best = best.rename(columns={"supplier_name": "best_supplier",
                                "price": "best_price",
                                "validity": "best_validity"})
    return best[cols].reset_index(drop=True)


def extract_vendor_quotes(sheets: dict) -> tuple[pd.DataFrame, dict]:
    """{sheet_name: 生DataFrame(header=None)} → (全报价 DataFrame, {sheet:件数})。

    data sheet は自動除外。各仕入先 sheet を parse_vendor_sheet で処理。
    """
    parts, counts = [], {}
    for name, raw in sheets.items():
        if is_data_sheet(name):
            continue
        q = parse_vendor_sheet(name, raw)
        counts[name] = len(q)
        if not q.empty:
            parts.append(q)
    allq = (pd.concat(parts, ignore_index=True) if parts
            else pd.DataFrame(columns=["supplier_name", "jan", "item_name",
                                       "price", "order_lot", "min_order_amount"]))
    return allq, counts
