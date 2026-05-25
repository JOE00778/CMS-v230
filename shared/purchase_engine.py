"""自動発注 計算エンジン.

入力:
  - supplier_quote テーブル (仕入先 × JAN の報価, zone 分類付き)
  - nst.sales_monthly (輸出·月次 SKU 販売数量·旧 shop_sales/export_item 置換 2026-05-26)
  - nst.item_master_raw (display_name / maker / item_rank / handling_cd·旧 item_v2)
  - nst.inventory_snapshot (現在庫: 手持 / 確保済 / 注文済·旧 item_inventory_snapshot_v2)
  - nst.inventory_activity_monthly (月完売率·旧 item_monthly_turnover)

発注ロジック (Boss 2026-05-12):
  目標在庫 = max(平均月販, 直近月販) × トレンド係数(1.2/1.0/0.7) × (納期カバー月数 + 安全在庫月数)
  有効在庫 = 手持 − 確保済 + 注文済        ← 「注文済」= 発注済で未入荷, 在途扱い (輸送中 列は無視)
  発注数 = max(0, 目標在庫 − 有効在庫) を lot 倍数に切り上げ ; ≤0 なら推奨に出さない

仕入先選定 (Boss 2026-05-12):
  1) 各 SKU は候補仕入先を zone 優先 (JD_DIRECT > BENTEN_TRANSIT > EMERGENCY > PREPAID > OTHER)
     → 同 zone は単価最安 でランク付け。主力 = 1位, 備用 = 2〜3位。
  2) メーカー単位で「なるべく 1〜3 仕入先に集約」(品牌集中)。
     1 仕入先がそのメーカーの SKU を数個しか持たない散らばりを避ける。
     ただしメーカー全体が数 SKU しか無い場合はこのルールを無視。
  3) 弁天倉庫は自社倉庫 → 中継費なし (markup 撤廃)。

除外: 取扱中止 / メーカー取扱中止 の SKU は発注対象外。
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

import pandas as pd

# 弁天は自社倉庫 → 中継費なし (Boss 2026-05-12)。運費モデルは現状なし。
ZONE_MARKUP = {
    "JD_DIRECT": 1.00,
    "BENTEN_TRANSIT": 1.00,
    "EMERGENCY": 1.00,
    "PREPAID": 1.00,
    "OTHER": 1.00,
}
DEFAULT_TREND_FACTORS = {"up": 1.2, "flat": 1.0, "down": 0.7}
DISCONTINUED_VALUES = {"取扱中止", "メーカー取扱中止", "取扱停止", "廃番", "生産終了"}
ZONE_LABEL_JA = {"JD_DIRECT": "JD直送", "BENTEN_TRANSIT": "弁天経由", "EMERGENCY": "応急",
                 "PREPAID": "前払い", "OTHER": "他"}
# 起定量を無視して「必要数ぴったり」で発注する仕入先 (応急/参考用なので。Boss 2026-05-12)
NO_LOT_SUPPLIERS = {"ハリマ", "SD"}


def _effective_pack(q: dict) -> int:
    """発注単位 (ケース入数)。NO_LOT_SUPPLIERS は 1 (起定量なし)。
    ケース入数が無ければ lot_size, それも無ければ 1 にフォールバック (人工確認フラグは別途立てる)。"""
    if q.get("supplier_name") in NO_LOT_SUPPLIERS:
        return 1
    cq = q.get("case_qty")
    if cq and cq > 0:
        return int(cq)
    lot = q.get("lot_size")
    if lot and lot > 0:
        return int(lot)
    return 1


# 旧名のエイリアス (後方互換)
_effective_lot = _effective_pack


def _parse_lead_days(text: str | None) -> int:
    """納期テキスト → 日数 (おおよそ)。'2週間'→14, '4/5週間'→35, '1週間'→7。"""
    if not text:
        return 14
    s = str(text)
    weeks = [int(x) for x in re.findall(r"(\d+)\s*週", s)]
    if weeks:
        return max(weeks) * 7
    days = [int(x) for x in re.findall(r"(\d+)\s*日", s)]
    if days:
        return max(days)
    months = [int(x) for x in re.findall(r"(\d+)\s*[ヶカか]?月", s)]
    if months:
        return max(months) * 30
    return 14


def _order_months_from_lead(lead_days: int, safety_months: float = 1.0) -> float:
    return math.ceil(lead_days / 30) + safety_months


def _classify_trend(months: list[float]) -> str:
    vals = [v for v in months if v is not None]
    if len(vals) < 2:
        return "flat"
    avg = sum(vals) / len(vals)
    last = vals[-1]
    if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) and last >= avg * 1.2:
        return "up"
    if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)) and last <= avg * 0.8:
        return "down"
    return "flat"


SALES_SOURCE_PRIORITY = ["export_item"]


def _load_monthly_sales(conn, months: int = 3, sales_source: str = "auto") -> tuple[pd.DataFrame, str]:
    # nst.sales_monthly（輸出·月次 SKU 販売数）→ JAN 単位集計（旧 shop_sales/export_item 置換·2026-05-26）
    rows = conn.execute(
        "SELECT im.jan AS jan, s.year_month AS period_start, SUM(s.qty_sold) AS qty "
        "FROM nst.sales_monthly s "
        "JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
        "WHERE im.jan IS NOT NULL AND s.qty_sold IS NOT NULL "
        "GROUP BY im.jan, s.year_month"
    ).fetchall()
    src = "export_item"
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        periods = sorted(df["period_start"].dropna().unique())[-months:]
        df = df[df["period_start"].isin(periods)].copy()
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
        return df, src
    return pd.DataFrame(columns=["jan", "period_start", "qty"]), src


def _load_inventory(conn) -> dict[str, dict]:
    """item_inventory_snapshot_v2 を JAN 単位に集計（Boss 2026-05-14 仕様）。

      jd_on_hand   = JD-物流-千葉 倉庫 の手持のみ (弁天は除外)
      on_order     = 注文済 (発注済で未入荷 = 在途, 倉庫横断 SUM)
      committed    = 確保済 (参考用, 計算には使わない)
      実質在庫 = jd_on_hand + on_order
    """
    inv: dict[str, dict] = {}
    # nst.inventory_snapshot 最新快照 → JAN 単位（旧 item_inventory_snapshot_v2 置換·2026-05-26）
    # jd_on_hand は JD 仓のみ（弁天除外·実質在庫 = jd_on_hand + on_order の Boss 口径維持）
    try:
        rows = conn.execute(
            "SELECT im.jan AS jan, "
            "COALESCE(SUM(CASE WHEN inv.warehouse LIKE 'JD%' THEN inv.qty_on_hand ELSE 0 END),0) AS jd_oh, "
            "COALESCE(SUM(inv.qty_on_hand),0) AS oh_all, "
            "COALESCE(SUM(inv.qty_on_hand - COALESCE(inv.qty_available, inv.qty_on_hand)),0) AS cm, "
            "COALESCE(SUM(inv.qty_on_order),0) AS oo "
            "FROM nst.inventory_snapshot inv "
            "JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id "
            "WHERE im.jan IS NOT NULL "
            "  AND inv.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot) "
            "GROUP BY im.jan"
        ).fetchall()
    except Exception:
        return inv
    for r in rows:
        inv[r["jan"]] = {
            "jd_on_hand": float(r["jd_oh"] or 0),
            "on_hand_all": float(r["oh_all"] or 0),
            "committed": float(r["cm"] or 0),
            "on_order": float(r["oo"] or 0),
        }
    return inv


def _sellthrough_factor(rate: float | None) -> float:
    """月完売率 → 発注係数（page18 と同一閾値・Boss 2026-05-26 トレンド係数を置換）。
    ≥0.9 断货风险→1.5 / 0.5-0.9 正常→1.0 / <0.5 压库存→0.5 / データ無し→1.0(中立)。
    """
    if rate is None:
        return 1.0
    if rate >= 0.9:
        return 1.5
    if rate >= 0.5:
        return 1.0
    return 0.5


def _load_sellthrough(conn) -> dict[str, float]:
    """item_monthly_turnover の最新月 月完売率を JAN 単位で返す（page18 と同一指標）。
    完売率 = qty_sold / (open_qty + qty_total_in)。item_code→JAN は item_v2 経由。
    表が無い/データ無し → 空 dict（→ 係数 1.0 で中立、テスト fixture も安全）。
    """
    try:
        rows = conn.execute(
            "SELECT im.jan AS jan, "
            "SUM(a.sold_qty) / NULLIF(SUM(a.opening_qty + a.received_qty), 0) AS rate "
            "FROM nst.inventory_activity_monthly a "
            "JOIN nst.item_master_raw im ON im.internal_id = a.item_internal_id "
            "WHERE a.year_month = (SELECT MAX(year_month) FROM nst.inventory_activity_monthly) "
            "  AND im.jan IS NOT NULL "
            "GROUP BY im.jan"
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for r in rows:
        if r["rate"] is not None:
            out[r["jan"]] = float(r["rate"])
    return out


def _load_items(conn) -> dict[str, dict]:
    out: dict[str, dict] = {}
    # nst.item_master_raw（旧 item_v2 置換·2026-05-26）: item_rank→rank, handling_cd→handling_status
    for r in conn.execute(
        "SELECT jan, display_name, maker, item_rank AS rank, handling_cd AS handling_status "
        "FROM nst.item_master_raw WHERE jan IS NOT NULL"
    ).fetchall():
        hs = (r["handling_status"] or "").strip()
        rk = (r["rank"] or "").strip()
        out[r["jan"]] = {
            "display_name": r["display_name"], "maker": r["maker"] or "(不明)", "rank": r["rank"],
            "handling_status": r["handling_status"],
            "discontinued": (hs in DISCONTINUED_VALUES) or (rk in DISCONTINUED_VALUES),
        }
    return out


def _supplier_label(q: dict) -> str:
    return f"{q['supplier_name']}({ZONE_LABEL_JA.get(q['zone'], q['zone'])})·¥{q['unit_price']}"


# 集約のアンカーに使ってよい zone tier。応急(3)/前払い(4) は絶対にアンカーにしない
#  → 品牌集約のために JD直送 の SKU を 応急/前払い に移すことはない。
ANCHOR_ZONE_TIERS = (1, 2)   # JD_DIRECT, BENTEN_TRANSIT
# 集約で発注先を移すときの単価許容比。元の最安より これ以上 高くなる移動はしない。
MAX_CONSOLIDATION_PRICE_RATIO = 1.5


def _consolidate_brand(jan_to_candidates: dict[str, list[dict]], maker_jans: dict[str, list[str]],
                       *, max_suppliers_per_brand: int, small_brand_skip: int) -> dict[str, str]:
    """メーカー単位で発注先を集約。戻り値: {jan: 採用 supplier_name}。

    既定 = 各 JAN の候補1位 (zone優先→最安)。
    メーカーの SKU 数 > small_brand_skip のとき:
      zone tier ごと (JD直送 → 弁天) に「そのメーカーの SKU を最も多くカバーできる仕入先」を貪欲に
      合計 max_suppliers_per_brand 社まで選ぶ (アンカー)。
      各 SKU は「その SKU の最良 zone と同じ zone のアンカー」が報価を持つなら、カバー数最大のアンカーへ。
      → ⚠️ zone tier をまたいだ移動は絶対にしない (JD直送→弁天/応急 等にはならない)。
        最良 zone が 応急/前払い の SKU はそのまま (選択肢が無いので)。
      タイブレーク: カバー数 → 平均単価安い。
    """
    chosen: dict[str, str] = {jan: c[0]["supplier_name"] for jan, c in jan_to_candidates.items()}

    for jans in maker_jans.values():
        if len(jans) <= small_brand_skip:
            continue  # 小品牌は集約しない
        jan_best_zr = {jan: jan_to_candidates[jan][0]["zone_rank"] for jan in jans}
        sup_zone: dict[str, int] = {}
        sup_jans: dict[str, set] = defaultdict(set)
        sup_prices: dict[str, list] = defaultdict(list)
        for jan in jans:
            for q in jan_to_candidates[jan]:
                sn = q["supplier_name"]
                sup_zone[sn] = q["zone_rank"]   # 1 仕入先 = 1 zone (sheet 由来) なので上書きで OK
                sup_jans[sn].add(jan)
                sup_prices[sn].append(q["unit_price"])

        anchors: list[str] = []
        anchor_set: set[str] = set()
        for tier in ANCHOR_ZONE_TIERS:
            if len(anchors) >= max_suppliers_per_brand:
                break
            tier_sups = [s for s in sup_zone if sup_zone[s] == tier and s not in anchor_set]
            need = {jan for jan in jans if jan_best_zr[jan] == tier}   # この tier が最良 zone の SKU
            while need and tier_sups and len(anchors) < max_suppliers_per_brand:
                def _key(s: str):
                    gain = len(sup_jans[s] & need)
                    avg_p = sum(sup_prices[s]) / len(sup_prices[s])
                    return (gain, -avg_p)
                best_s = max(tier_sups, key=_key)
                if len(sup_jans[best_s] & need) == 0:
                    break
                anchors.append(best_s)
                anchor_set.add(best_s)
                need -= sup_jans[best_s]
                tier_sups.remove(best_s)
        if not anchor_set:
            continue
        anchor_rank = {s: i for i, s in enumerate(anchors)}  # 早い = カバー数多い
        for jan in jans:
            bzr = jan_best_zr[jan]
            orig_price = jan_to_candidates[jan][0]["unit_price"]
            # その SKU の最良 zone と同じ zone のアンカーで, 報価があり, 単価が許容比内のもの
            cands = [q for q in jan_to_candidates[jan]
                     if q["supplier_name"] in anchor_set and q["zone_rank"] == bzr
                     and q["unit_price"] <= orig_price * MAX_CONSOLIDATION_PRICE_RATIO]
            if cands:
                cands.sort(key=lambda q: anchor_rank[q["supplier_name"]])
                chosen[jan] = cands[0]["supplier_name"]
            # else: 既定 (アンカー無し / 応急・前払い / 単価が高すぎ) のまま
    return chosen


def compute_recommendations(
    conn,
    *,
    months: int = 4,
    safety_months: float = 1.0,            # 互換のため残置 (Boss 2026-05-14 仕様では未使用)
    trend_factors: dict | None = None,
    fixed_order_months: float | None = None,  # 互換のため残置 (未使用)
    sales_source: str = "auto",
    include_discontinued: bool = False,
    use_inventory: bool = True,
    consolidate_by_brand: bool = True,
    max_suppliers_per_brand: int = 3,
    small_brand_skip: int = 5,
    optimize: str = "zone",
    ranks: list[str] | tuple[str, ...] | None = None,
    max_stock_months: float | None = None,
) -> pd.DataFrame:
    """発注推奨を計算 (Boss 2026-05-14 仕様)。

    数量公式:
      推奨月販 = max(平均月販, 直近月販) × トレンド係数(1.2/1.0/0.7)    ← 近 `months` 月のトレンド
      実質在庫 = JD-物流-千葉 手持 + 注文済(全倉横断)                   ← 弁天は含めない (Boss 2026-05-14)
      上次発注時剩余 = 実質在庫 − 推奨月販
      目標在庫 = 推奨月販 × 1.5
      必要数 = 目標在庫 − 上次発注時剩余 = 推奨月販 × 2.5 − 実質在庫
      発注箱数 = CEIL(必要数 / ケース入数)     ← ロット ではなく ケース で取整 (ハリマ/SD は 1)
      発注数 = 発注箱数 × ケース入数

    スキップ / 人工確認:
      ランク = 取扱中止 → スキップ (除外件数表示)
      ランク = NEW → status='new_passive' (受動的発注: 需要が来たら手動)
      推奨月販 = 0 → スキップ (実績なし)
      必要数 ≤ 0 → スキップ (在庫充足)
      ケース入数 が 0/欠落 → status='needs_review_pack' (人工確認 + 単位=1 で計算)
      ケース入数 / 推奨月販 ≥ 2.5 → status='needs_review_oversize' (箱規が大き過ぎ → 出はするが人工確認)

    主な引数:
        months: 月販トレンドに使う直近期間数 (既定 4)
        trend_factors: {'up':1.2,'flat':1.0,'down':0.7}
        include_discontinued: True なら 取扱中止 品も含める (既定 False)
        use_inventory: True なら 実質在庫 (JD手持+注文済) を差し引く (既定 True)
        consolidate_by_brand / max_suppliers_per_brand / small_brand_skip: 品牌集約 (Boss 2026-05-12)
        optimize: 'zone'(既定) / 'line_cost' / 'cost' (仕入先選定方針)
        ranks: 対象ランク絞り込み (例 ('Aランク','Bランク'))
        max_stock_months: 発注後在庫月数の上限 (None=チェックなし)
        safety_months / fixed_order_months: 旧仕様の名残, Boss 2026-05-14 仕様では未使用

    DataFrame.attrs: sales_source / periods / n_discontinued_excluded / n_rank_excluded / inventory_loaded
                     / n_consolidated / optimize / n_overstock / max_stock_months / n_new_passive / n_needs_review
    """
    tf = trend_factors or DEFAULT_TREND_FACTORS

    sales, used_source = _load_monthly_sales(conn, months=months, sales_source=sales_source)
    periods = sorted(sales["period_start"].dropna().unique()) if not sales.empty else []
    sales_pivot: dict[str, dict[str, float]] = {}
    for _, r in sales.iterrows():
        sales_pivot.setdefault(r["jan"], {})[r["period_start"]] = float(r["qty"])

    item_map = _load_items(conn)
    inv_map = _load_inventory(conn) if use_inventory else {}
    inventory_loaded = bool(inv_map)
    st_map = _load_sellthrough(conn)   # JAN → 月完売率（係数算出用·Boss 2026-05-26 取代趋势系数）

    quotes: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT supplier_name, jan, display_name, unit_price, lot_size, case_qty, "
        "min_order_amount, order_condition, lead_time_text, zone, zone_rank, nst_supplier_code "
        "FROM supplier_quote WHERE unit_price IS NOT NULL AND unit_price > 0"
    ).fetchall():
        quotes.setdefault(r["jan"], []).append(dict(r))

    # ---- Phase 1: SKU ごとの候補リスト + 基礎指標 (発注先はまだ決めない) ----
    n_discontinued_excluded = 0
    n_rank_excluded = 0
    rank_set = set(ranks) if ranks else None
    sku: dict[str, dict] = {}
    jan_to_candidates: dict[str, list[dict]] = {}
    for jan, qlist in quotes.items():
        meta = item_map.get(jan, {})
        if meta.get("discontinued") and not include_discontinued:
            n_discontinued_excluded += 1
            continue
        if rank_set is not None and (meta.get("rank") not in rank_set):
            n_rank_excluded += 1
            continue
        is_new_passive = (meta.get("rank") == "NEW")    # NEW = 需要待ちで受動発注 (Boss 2026-05-14)

        msales = sales_pivot.get(jan, {})
        m_seq = [msales.get(p, 0.0) for p in periods]
        avg_monthly = (sum(m_seq) / len(m_seq)) if m_seq else 0.0
        latest_monthly = m_seq[-1] if m_seq else 0.0
        # 実績ゼロは NEW 以外スキップ
        if not is_new_passive and avg_monthly <= 0 and latest_monthly <= 0:
            continue

        for q in qlist:
            q["_eff"] = q["unit_price"] * ZONE_MARKUP.get(q["zone"], 1.0)
        iv = inv_map.get(jan, {})
        jd_on_hand = iv.get("jd_on_hand", 0.0)
        on_hand_all = iv.get("on_hand_all", 0.0)
        committed = iv.get("committed", 0.0)
        on_order = iv.get("on_order", 0.0)
        # Boss 2026-05-14: 実質在庫 = JD仓 + 注文済  (弁天は除外, 確保済も差し引かない)
        eff_stock = jd_on_hand + on_order

        base_monthly = max(avg_monthly, latest_monthly)
        trend = _classify_trend(m_seq)              # 参考: 売上方向（数量計算には使わない）
        st_rate = st_map.get(jan)
        stfac = _sellthrough_factor(st_rate)        # 完売率係数（Boss 2026-05-26 トレンド係数を置換）
        rec_monthly = base_monthly * stfac          # 推奨月販 = max(平均,直近) × 完売率係数

        def _est_line_cost(q: dict) -> int:
            """その仕入先で発注した場合の line_cost 見積り (Boss 2026-05-14 公式 + ケース丸め)。"""
            pack_ = _effective_pack(q)
            needed = rec_monthly * 2.5 - eff_stock
            if needed <= 0:
                return 0
            return math.ceil(needed / pack_) * pack_ * q["unit_price"]

        if optimize == "cost":
            candidates = sorted(qlist, key=lambda q: (q["_eff"], q["zone_rank"], q["supplier_name"]))
        elif optimize == "line_cost":
            candidates = sorted(qlist, key=lambda q: (_est_line_cost(q), q["zone_rank"], q["supplier_name"]))
        else:
            candidates = sorted(qlist, key=lambda q: (q["zone_rank"], q["_eff"], q["supplier_name"]))

        sku[jan] = {
            "candidates": candidates, "meta": meta, "m_seq": m_seq,
            "avg_monthly": avg_monthly, "latest_monthly": latest_monthly,
            "base_monthly": base_monthly, "rec_monthly": rec_monthly,
            "trend": trend, "sellthrough": st_rate, "sellthrough_factor": stfac,
            "jd_on_hand": jd_on_hand, "on_hand_all": on_hand_all,
            "committed": committed, "on_order": on_order,
            "eff_stock": eff_stock,
            "is_new_passive": is_new_passive,
        }
        jan_to_candidates[jan] = candidates

    # ---- Phase 2: メーカー単位の集約 → 発注先決定 ----
    if consolidate_by_brand:
        maker_jans: dict[str, list[str]] = defaultdict(list)
        for jan, d in sku.items():
            maker_jans[d["meta"].get("maker", "(不明)")].append(jan)
        final_supplier = _consolidate_brand(
            jan_to_candidates, maker_jans,
            max_suppliers_per_brand=max_suppliers_per_brand, small_brand_skip=small_brand_skip,
        )
    else:
        final_supplier = {jan: d["candidates"][0]["supplier_name"] for jan, d in sku.items()}

    # ---- Phase 3: 発注先確定後に数量・金額を確定 (Boss 2026-05-14 公式) ----
    n_consolidated = 0
    rows: list[dict] = []
    for jan, d in sku.items():
        cands = d["candidates"]
        fs = final_supplier.get(jan, cands[0]["supplier_name"])
        best = next((q for q in cands if q["supplier_name"] == fs), cands[0])
        moved = fs != cands[0]["supplier_name"]
        if moved:
            n_consolidated += 1
        tfac = tf.get(d["trend"], 1.0)
        rec_monthly = d["rec_monthly"]            # 推奨月販 = base × トレンド係数
        eff_stock = d["eff_stock"]                # 実質在庫 = JD手持 + 注文済
        target_stock = rec_monthly * 1.5          # Boss: 目標在庫 = 推奨月販 × 1.5
        prev_remaining = eff_stock - rec_monthly  # Boss: 上次発注時剩余 = 実質在庫 − 推奨月販
        needed = target_stock - prev_remaining    # = rec_monthly × 2.5 − eff_stock
        meta = d["meta"]

        # 発注単位 (ケース入数) と ロット欠落の検知
        raw_case = best.get("case_qty")
        raw_lot = best.get("lot_size")
        pack = _effective_pack(best)              # ハリマ/SD は 1, それ以外は case → lot → 1
        pack_source = ("ケース" if (raw_case and raw_case > 0)
                       else ("ロット(代替)" if (raw_lot and raw_lot > 0)
                             else ("ぴったり(応急)" if best["supplier_name"] in NO_LOT_SUPPLIERS else "未設定")))
        needs_review_pack = (best["supplier_name"] not in NO_LOT_SUPPLIERS) and not (
            (raw_case and raw_case > 0) or (raw_lot and raw_lot > 0))
        needs_review_oversize = (rec_monthly > 0) and (pack > 0) and (pack / rec_monthly >= 2.5)
        backups = [q for q in cands if q["supplier_name"] != fs][:2]
        b1 = backups[0] if len(backups) >= 1 else None
        b2 = backups[1] if len(backups) >= 2 else None
        base_row = {
            "jan": jan,
            "display_name": meta.get("display_name") or best.get("display_name") or "",
            "maker": meta.get("maker", "(不明)"),
            "rank": meta.get("rank"),
            "handling_status": meta.get("handling_status"),
            **{f"m{i+1}": d["m_seq"][i] if i < len(d["m_seq"]) else None for i in range(months)},
            "avg_monthly": round(d["avg_monthly"], 1),
            "latest_monthly": d["latest_monthly"],
            "trend": d["trend"],
            "trend_factor": tfac,
            "sellthrough": d.get("sellthrough"),
            "sellthrough_factor": d.get("sellthrough_factor"),
            "rec_monthly": round(rec_monthly, 1),
            "lead_time_text": best["lead_time_text"],
            "jd_on_hand": d["jd_on_hand"],
            "on_hand_all": d["on_hand_all"],
            "qty_committed": d["committed"],
            "on_order": d["on_order"],
            "eff_stock": eff_stock,
            "target_stock": round(target_stock, 1),
            "prev_remaining": round(prev_remaining, 1),
            "shortfall": round(needed, 1),
            # --- 発注先 (主力) + 備用 ---
            "supplier_name": fs,
            "supplier_primary": fs,
            "zone": best["zone"],
            "zone_rank": best["zone_rank"],
            "nst_supplier_code": best["nst_supplier_code"],
            "unit_price": best["unit_price"],
            "effective_price": round(best["_eff"]),
            "case_qty": raw_case,
            "lot_size": raw_lot,
            "pack_size": pack,
            "pack_source": pack_source,
            "consolidated": moved,
            "supplier_backup1": b1["supplier_name"] if b1 else None,
            "backup1_zone": b1["zone"] if b1 else None,
            "backup1_price": b1["unit_price"] if b1 else None,
            "supplier_backup2": b2["supplier_name"] if b2 else None,
            "backup2_zone": b2["zone"] if b2 else None,
            "backup2_price": b2["unit_price"] if b2 else None,
            "needs_review_pack": needs_review_pack,
            "needs_review_oversize": needs_review_oversize,
            "min_order_amount": best["min_order_amount"] or 0,
            "order_condition": best["order_condition"],
            "n_alt_suppliers": len(cands),
            "alt_suppliers": " / ".join(_supplier_label(q) for q in cands[:5]),
            # 旧仕様互換用フィールド (UI 表示はこれら使う場面あり)
            "on_hand": d["jd_on_hand"],          # = 実質在庫の JD 部分 (旧名)
            "order_months": 2.5,                  # 新仕様の固定相当値
        }

        # NEW = 受動発注 → 0 個・status='new_passive' で出力
        if d["is_new_passive"]:
            base_row.update({
                "suggested_qty": 0, "boxes": 0, "line_cost": 0,
                "stock_months_after": round(eff_stock / max(rec_monthly, 1e-6), 1) if rec_monthly > 0 else None,
                "overstock": False,
            })
            rows.append(base_row)
            continue

        # 必要数 ≤ 0 → スキップ (在庫充足)
        if needed <= 0:
            continue

        boxes = math.ceil(needed / pack)
        suggested_qty = boxes * pack
        if suggested_qty <= 0:
            continue
        line_cost = round(suggested_qty * best["unit_price"])
        stock_months_after = (eff_stock + suggested_qty) / max(rec_monthly, 1e-6)
        overstock = (max_stock_months is not None) and (stock_months_after > max_stock_months)

        base_row.update({
            "boxes": boxes,
            "suggested_qty": suggested_qty,
            "stock_months_after": round(stock_months_after, 1),
            "overstock": overstock,
            "line_cost": line_cost,
        })
        rows.append(base_row)

    df = pd.DataFrame(rows)
    if df.empty:
        df.attrs.update(sales_source=used_source, periods=periods,
                        n_discontinued_excluded=n_discontinued_excluded, n_rank_excluded=n_rank_excluded,
                        inventory_loaded=inventory_loaded, n_consolidated=0, optimize=optimize,
                        n_overstock=0, max_stock_months=max_stock_months,
                        n_new_passive=0, n_needs_review=0)
        return df

    # 推奨分のみで supplier_total を集計 (NEW/保留 は 0 円なので除外しても同じ)
    df["supplier_total"] = df["supplier_name"].map(
        df[df["line_cost"] > 0].groupby("supplier_name")["line_cost"].sum()
    ).fillna(0).astype(int)
    df["meets_min_order"] = df.apply(
        lambda r: (r["min_order_amount"] == 0) or (r["supplier_total"] >= r["min_order_amount"]),
        axis=1,
    )

    def _status(r):
        # NEW = 受動発注 (Boss 2026-05-14)
        if r.get("rank") == "NEW":
            return "new_passive"
        if r["overstock"]:
            return "deferred_overstock"
        if not r["meets_min_order"]:
            return "deferred_min_order"
        if r["needs_review_pack"] or r["needs_review_oversize"]:
            return "needs_review"   # 出はするが人工確認 (Boss 2026-05-14: 仍出在订货单 + 标记)
        return "recommended"
    df["status"] = df.apply(_status, axis=1)

    def _reason(r):
        if r.get("rank") == "NEW":
            return f"🆕NEW (受動発注)・候補仕入先={r['supplier_primary']}・ケース{r['pack_size']}"
        z = ZONE_LABEL_JA.get(r["zone"], r["zone"])
        rm = r["rec_monthly"]
        base = (f"{z}・単価{r['unit_price']}・{r['trend']}×{r['trend_factor']}"
                f"｜推奨月販{rm:.1f}×2.5={rm*2.5:.0f}−実質在庫{r['eff_stock']:.0f}(JD{r['jd_on_hand']:.0f}+注文済{r['on_order']:.0f})"
                f"＝必要{r['shortfall']:.0f}→{r['boxes']}箱×{r['pack_size']}={r['suggested_qty']}個"
                f"(発注後在庫{r['stock_months_after']:.1f}ヶ月)")
        if r["consolidated"]:
            base += "・🔗品牌集約"
        if r["needs_review_pack"]:
            base += "・⚠️ケース/ロット未設定"
        if r["needs_review_oversize"]:
            base += f"・⚠️箱規{r['pack_size']}/月販{rm:.0f}={r['pack_size']/max(rm,1e-6):.1f}≥2.5"
        if r["overstock"]:
            base += f"・⚠️発注後在庫{r['stock_months_after']:.1f}>上限{max_stock_months}→保留"
        if not r["meets_min_order"]:
            base += f"・⚠️最低受注¥{r['min_order_amount']:,}未達(現¥{r['supplier_total']:,.0f})"
        return base
    df["reason"] = df.apply(_reason, axis=1)

    df = df.sort_values(["status", "zone_rank", "supplier_name", "maker", "line_cost"],
                        ascending=[True, True, True, True, False]).reset_index(drop=True)
    n_new = int((df["status"] == "new_passive").sum())
    n_need = int((df["status"] == "needs_review").sum())
    df.attrs.update(sales_source=used_source, periods=periods,
                    n_discontinued_excluded=n_discontinued_excluded, n_rank_excluded=n_rank_excluded,
                    inventory_loaded=inventory_loaded, n_consolidated=n_consolidated, optimize=optimize,
                    n_overstock=int(df["overstock"].sum()), max_stock_months=max_stock_months,
                    n_new_passive=n_new, n_needs_review=n_need)
    return df
