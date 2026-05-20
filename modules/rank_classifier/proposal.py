"""等级判定提案生成（T-016）— generate_proposal + export_csv

v2 决策（2026-05-05）：
- 仓库限定 = JD-物流-千葉（其他仓库不参与等级 / 订货决策）
- 月周转率 = 订货决策核心指标（决定再订货点 + 下单量）
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import List
from datetime import datetime, timezone

from .rules import classify_rank, calc_sales_rank, Rank


# 仓库硬过滤（v2 决策 · 跟 modules/inventory_health/metrics.py 保持一致）
WAREHOUSE_FILTER = "JD-物流-千葉"

# 安全系数（按等级差异化订货）
SAFETY_FACTOR = {'A': 1.5, 'B': 1.0, 'C': 0.5, '停售': 0.0}


def calc_reorder(monthly_sales: float, lead_time_days: int, rank: str) -> dict:
    """订货决策（基于月销量 + 进货周期 + 等级安全系数）

    再订货点（库存低于此就要补货）
    建议下单量
    """
    safety = SAFETY_FACTOR.get(rank, 0.5)
    lead_time_months = (lead_time_days or 30) / 30.0

    reorder_point = monthly_sales * lead_time_months * safety
    suggested_order_qty = monthly_sales * min(lead_time_months, 3) * safety
    return {
        "reorder_point": round(reorder_point, 1),
        "suggested_order_qty": round(suggested_order_qty, 1),
        "safety_factor": safety,
        "lead_time_days": lead_time_days or 30,
    }


def generate_proposal(
    quarter: str = '2026-Q1',
    db_path: str = 'data_warehouse/warehouse.db',
    *,
    period_start: str | None = None,
    period_end: str | None = None,
) -> List[dict]:
    """
    生成等级判定建议清单（proposal）

    Args:
        quarter: 仅作为 metadata 标识 (e.g. 'FY2026-Q1' / '2026-04')
        db_path: SQLite DB path
        period_start / period_end: 'YYYY-MM-DD' 期间过滤
            - 月度: 都传单期间 (例 '2026-04-01' ~ '2026-04-30')
              SQL 用 = 精确匹配 sales_line.period_start/period_end
            - 季度: 传 Q 范围 (例 '2026-03-01' ~ '2026-05-31')
              SQL 用 >= / <= 范围,聚合 3 个月度报表
            - 都不传: 聚合全部 asean_monthly (不分期间, 兼容旧调用)

    Returns:
        list of dicts (含 sku/old_rank/new_rank/sales/margin/rank_pct 等)
    """
    from shared.db import get_connection
    conn = get_connection()

    try:
        # ========================================================
        # 等级判定: NST nst.sales_monthly + item_master_raw ベース
        #   利益率 = (revenue − cost_estimate×qty) / revenue
        #            （sales_monthly に粗利未保存のため定義原価で算出・page04/05 と同基準）
        #   期間: period_start/end の YYYY-MM を year_month 範囲へ変換
        # ========================================================
        if period_start and period_end:
            ym_where = "WHERE s.year_month >= ? AND s.year_month <= ?"
            params = (period_start[:7], period_end[:7])
        else:
            ym_where = ""
            params = ()
        sales_data = [dict(r) for r in conn.execute(f"""
            SELECT
                im.item_code AS item_code,
                MIN(im.display_name) AS display_name,
                MIN(im.handling_cd) AS handling_status,
                COALESCE(SUM(s.revenue), 0) AS total_revenue,
                COALESCE(SUM(s.qty_sold), 0) AS total_qty,
                COALESCE(SUM(s.gross_profit), 0) AS total_gp
            FROM nst.sales_monthly s
            JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id
            {ym_where}
            GROUP BY im.item_code
        """, params).fetchall()]

        if not sales_data:
            return []

        # PG NUMERIC → Decimal を float へ正規化（calc_sales_rank の cumsum 等で float 混在を防ぐ）
        for row in sales_data:
            row['total_revenue'] = float(row['total_revenue'] or 0)
            row['total_qty'] = float(row['total_qty'] or 0)
            row['total_gp'] = float(row['total_gp'] or 0)

        # 利益率 = NetSuite 真実毛利(estgrossprofit) / 売上
        for row in sales_data:
            rev = row['total_revenue']
            row['avg_margin'] = (row['total_gp'] / rev) if rev else 0.0

        sku_to_sales = {row['item_code']: row['total_revenue'] for row in sales_data}
        rank_pcts = calc_sales_rank(sku_to_sales)
        status_map = {row['item_code']: row['handling_status'] or '取扱中' for row in sales_data}

        # 在庫量（NST 最新スナップショット · JD-物流-千葉 単一仓）→ reorder 字段用のみ
        qty_map = {}
        try:
            inv_data = conn.execute("""
                SELECT im.item_code AS item_code, SUM(inv.qty_on_hand) AS qty
                FROM nst.inventory_snapshot inv
                JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id
                WHERE inv.snapshot_date = (SELECT MAX(snapshot_date) FROM nst.inventory_snapshot)
                GROUP BY im.item_code
            """).fetchall()
            qty_map = {row['item_code']: float(row['qty'] or 0) for row in inv_data}
        except Exception:
            pass

        # 現行 rank（NST item_master_raw.item_rank）
        old_rank_map = {
            row['item_code']: row['item_rank']
            for row in conn.execute(
                "SELECT item_code, item_rank FROM nst.item_master_raw WHERE item_code IS NOT NULL"
            ).fetchall()
        }

        # 進货周期: NST になし → calc_reorder で既定 30 日
        lead_time_map = {}

        # 3 ヶ月無動销: sales_monthly の直近 3 ヶ月で qty_sold=0 の SKU
        no_sales_3m_set: set[str] = set()
        try:
            if period_end:
                _ref_ym = period_end[:7]
            else:
                _r = conn.execute("SELECT MAX(year_month) AS m FROM nst.sales_monthly").fetchone()
                _ref_ym = _r['m'] if _r else None
            if _ref_ym:
                _y, _m = int(_ref_ym[:4]), int(_ref_ym[5:7])
                _sm, _sy = _m - 2, _y
                while _sm <= 0:
                    _sm += 12
                    _sy -= 1
                _start_ym = f"{_sy:04d}-{_sm:02d}"
                _active = {
                    r['item_code']
                    for r in conn.execute("""
                        SELECT im.item_code AS item_code
                        FROM nst.sales_monthly s
                        JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id
                        WHERE s.year_month >= ? AND s.year_month <= ?
                        GROUP BY im.item_code
                        HAVING COALESCE(SUM(s.qty_sold), 0) > 0
                    """, (_start_ym, _ref_ym)).fetchall()
                }
                no_sales_3m_set = {row['item_code'] for row in sales_data} - _active
        except Exception:
            no_sales_3m_set = set()

        # 6. 生成 proposal（含订货建议）
        proposals = []
        for row in sales_data:
            item_code = row['item_code']
            name = row['display_name'] or item_code
            sales = row['total_revenue']
            margin = row['avg_margin'] or 0
            monthly_qty_sold = row['total_qty'] or 0
            qty_on_hand = qty_map.get(item_code, 0)

            netsuite_status = status_map.get(item_code, '取扱中')
            rank_pct = rank_pcts.get(item_code, 1.0)
            no_sales_3m = item_code in no_sales_3m_set

            new_rank = classify_rank({
                'netsuite_status': netsuite_status,
                'acknowledged_action': None,
                'sales_amount_rank_pct': rank_pct,
                'gross_margin_rate': margin,
                'no_sales_3m': no_sales_3m,
            })
            old_rank = old_rank_map.get(item_code, 'NEW')

            # Boss 規則（2026-05-21）: 取扱中止は吸収状態 — 現行が取扱中止/停售なら
            # 等级判定の対象外（降级で取扱中止に入るのは可、取扱中止から A/B/C へ戻すのは不可）
            if str(old_rank) in ('取扱中止', 'メーカー取扱中止', '停售'):
                new_rank = '停售'

            # 等级波动标记（升 / 降 / 维持）
            rank_order = {'A': 4, 'Aランク': 4, 'Bランク': 3, 'B': 3,
                          'Cランク': 2, 'C': 2, 'NEW': 1, '停售': 0,
                          '取扱中止': 0, 'メーカー取扱中止': 0}
            old_score = rank_order.get(str(old_rank), 1)
            new_score = rank_order.get(new_rank, 2)
            if new_score > old_score:
                trend = "⬆️ 升级"
            elif new_score < old_score:
                trend = "⬇️ 降级"
            else:
                trend = "➡️ 维持"

            # 订货建议（基于月销量 × 进货周期 × 等级安全系数）
            lead_time_days = lead_time_map.get(item_code)
            reorder = calc_reorder(monthly_qty_sold, lead_time_days, new_rank)

            proposals.append({
                'sku': item_code,
                'name': name,
                'old_rank': old_rank,
                'new_rank': new_rank,
                'trend': trend,
                'sales': sales,
                'margin': margin,
                'rank_pct': rank_pct,
                'netsuite_status': netsuite_status,
                'no_sales_3m': no_sales_3m,
                'monthly_qty_sold': monthly_qty_sold,
                'qty_on_hand': qty_on_hand,
                'reorder_point': reorder['reorder_point'],
                'suggested_order_qty': reorder['suggested_order_qty'],
                'lead_time_days': reorder['lead_time_days'],
                'need_reorder': qty_on_hand < reorder['reorder_point'],
                'acknowledged_action': None,
            })

        return proposals

    finally:
        conn.close()


def export_csv(proposals: List[dict], path: str | Path) -> None:
    """
    导出 NetSuite Item Import 格式 CSV

    Args:
        proposals: generate_proposal 的输出
        path: 输出文件路径
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        'item_code',
        'display_name',
        'old_rank',
        'new_rank',
        'total_revenue',
        'avg_margin_rate',
        'sales_rank_pct',
        'netsuite_status',
    ]

    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for p in proposals:
            writer.writerow({
                'item_code': p['sku'],
                'display_name': p['name'],
                'old_rank': p['old_rank'],
                'new_rank': p['new_rank'],
                'total_revenue': round(p['sales'], 2),
                'avg_margin_rate': f"{p['margin']*100:.1f}%",
                'sales_rank_pct': f"{p['rank_pct']*100:.1f}%",
                'netsuite_status': p['netsuite_status'],
            })
