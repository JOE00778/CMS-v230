"""店铺 → 负责人 映射（shopee&lazada 团队 · 「负责人毛利情况」表 + Boss 补充）。

业务规则（Boss 2026-05-20 确认）：
- 负责人毛利情况表（1月）の店舗割当 + COUPANG=尹雪莉
- 日本店（Amazon / ヤフー）は担当者統計の対象外（忽略）
- 未登録の店舗は「未分配」（Boss に確認して _SHOP_OWNER に追記 → 再デプロイ）

负责人は期ごとに変動するため、更新は本 dict を編集して SSH 一括デプロイ。
"""
from __future__ import annotations

import pandas as pd

OWNER_NA = "未分配"
OWNER_EXCLUDED = "対象外"

# 市場大区（国別に分けない · Boss 2026-05-21）: 东南亚 / 韩国 / 日本
MARKET_OTHER = "其他"


def classify_market(shop: str | None) -> str:
    """店舗名 → 市場大区（东南亚 / 韩国 / 日本）· 国別細分なし。

    COUPANG=韩国 · Amazon/ヤフー=日本 · その他(Shopee/Lazada 各国)=东南亚。
    """
    s = str(shop or "").strip()
    if not s:
        return MARKET_OTHER
    if "COUPANG" in s.upper():
        return "韩国"
    if "Amazon" in s or "ヤフー" in s:
        return "日本"
    return "东南亚"

# 负责人 → 担当店舗（「一元管理导出改」2026/5 最终版 · Boss 権威マッピング）
_SHOP_OWNER: dict[str, list[str]] = {
    "邓晓庆": ["Lazada MY", "Lazada PH", "Lazada SG", "Shopee TW", "Shopee toy TW"],
    # japan_finds.PH は 2026-08-03 尹雪莉さん申請で「未分配」から移管
    "尹雪莉": ["Shopee Cosme VN", "Shopee VN", "Shopee TH", "Shopee japan_finds.PH"],
    "许慧杰": ["Smikiejapan COUPANG"],
    "刘颖":   ["Shopee Kurashi-Mart.PH", "Shopee Mall PH", "Shopee PH"],
    "裴晓晗": ["Shopee BR", "Shopee Cosme PH", "Shopee Cosme SG",
              "Shopee J-Beauty Hub PH", "Shopee Kurashi-Mart.SG", "Shopee SG",
              "Shopee kurashi_mart.BR"],
}
# shop（strip 済）→ owner の逆引き
_OWNER_BY_SHOP = {
    shop.strip(): owner for owner, shops in _SHOP_OWNER.items() for shop in shops
}

ALL_OWNERS = list(_SHOP_OWNER.keys())


def classify_owner(shop: str | None) -> str:
    """店舗名 → 负责人。日本店（Amazon/ヤフー）は対象外、未登録は未分配。"""
    if not shop:
        return OWNER_NA
    s = str(shop).strip()
    if "Amazon" in s or "ヤフー" in s:   # 日本店は担当者統計の対象外
        return OWNER_EXCLUDED
    return _OWNER_BY_SHOP.get(s, OWNER_NA)


def add_owner_column(
    df: pd.DataFrame, shop_col: str = "shop",
    owner_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    """DataFrame に 'owner' 列を付与（shop 列ベース）。新しい DataFrame を返す。

    owner_map を渡すとその対応表で引く（= 対象月の担当者。ops.shop_owner 由来）。
    未登録の店舗は OWNER_NONE("")＝担当者未設定。省略時は従来どおり
    classify_owner（ハードコード基線）で、日本店は OWNER_EXCLUDED。
    """
    if df.empty or shop_col not in df.columns:
        out = df.copy()
        out["owner"] = OWNER_NONE if owner_map is not None else OWNER_NA
        return out
    out = df.copy()
    if owner_map is None:
        out["owner"] = out[shop_col].apply(classify_owner)
    else:
        out["owner"] = out[shop_col].apply(lambda s: owner_of(s, owner_map))
    return out


# ============================================================
# 期間対応の担当者マッピング（2026-08-27 Boss 依頼）
# ------------------------------------------------------------
# 上の _SHOP_OWNER は「基線」として残す（テーブルに履歴が無い店舗の既定値）。
# 変更は ops.shop_owner に「発効年月つき」で積む → 過去月の数字は動かない。
# 担当者未設定（OWNER_NONE）の店舗は page05 の集計から全面的に除外される。
# ============================================================

OWNER_NONE = ""            # 担当者未設定 = page05 の全計算から除外
OPS_SCHEMA = "ops"
OWNER_TABLE = "ops.shop_owner"


def resolve_owner_map(
    records, ym: str, *, baseline: dict[str, str] | None = None,
) -> dict[str, str]:
    """発効年月つきレコード列 + 対象月 → {店舗: 担当者} の純関数。

    records: (shop, effective_ym, owner) の反復可能。effective_ym は 'YYYY-MM'。
    採用規則: 各店舗について effective_ym <= ym の中で最新の 1 本。
             owner が空/None のレコードは「その月から担当者なし」を意味する。
    レコードが 1 本も無い店舗は baseline（既定 = _OWNER_BY_SHOP）を使う。
    """
    best: dict[str, tuple[str, str]] = {}
    for shop, eff, owner in records:
        s = str(shop or "").strip()
        e = str(eff or "").strip()
        if not s or not e or e > ym:      # 未来の発効は当該月には効かない
            continue
        cur = best.get(s)
        if cur is None or e >= cur[0]:
            best[s] = (e, str(owner or "").strip())
    out = {s: o for s, (_e, o) in best.items()}
    for s, o in (_OWNER_BY_SHOP if baseline is None else baseline).items():
        out.setdefault(s, o)
    return out


def load_owner_map(conn, ym: str) -> dict[str, str]:
    """ops.shop_owner を読んで対象月の {店舗: 担当者} を返す。読めなければ基線。"""
    try:
        cur = conn.execute(
            f"SELECT shop, effective_ym, owner FROM {OWNER_TABLE}")
        rows = [tuple(r)[:3] for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return dict(_OWNER_BY_SHOP)
    return resolve_owner_map(rows, ym)


def owner_of(shop: str | None, owner_map: dict[str, str]) -> str:
    """owner_map ベースの担当者引き。未登録は OWNER_NONE（= 集計対象外）。"""
    return owner_map.get(str(shop or "").strip(), OWNER_NONE)


def has_owner(owner: str | None) -> bool:
    """集計に入れるべき行か。担当者未設定 / 未分配 / 対象外 は False。"""
    o = str(owner or "").strip()
    return bool(o) and o not in (OWNER_NA, OWNER_EXCLUDED)
