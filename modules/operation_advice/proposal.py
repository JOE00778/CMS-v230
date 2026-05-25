"""运营建议批量生成（基于 cross_ratio_monthly + rank_classifier 输出）"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from .rules import advise


def _norm_rank(x) -> str:
    """主档 item_rank → A/B/C/停售（A/停售 由 advise 跳过）。"""
    s = str(x or "").strip()
    if s.startswith("A"):
        return "A"
    if s.startswith("B"):
        return "B"
    if s.startswith("C"):
        return "C"
    if "停" in s or "中止" in s:
        return "停售"
    return "C"


def generate_advice(year_month: str = "2026-04",
                    db_path: str = "data_warehouse/warehouse.db",
                    persist: bool = True) -> List[dict]:
    """跑全 SKU（B/C 档）出运营建议清单（nst.* 实时算·2026-05-26 迁移）。

    旧源 cross_ratio_monthly 已停灌 → 改实时算:
    - 毛利率 = Σ粗利 / Σ収益 ×100（当月 nst.sales_monthly）
    - 月周转 = 月販売数 / 当前在庫（最新 inventory_snapshot · JD-物流-千葉）
    - 库存价值 = 在庫 × 定義原価(cost_estimate)
    - 等级取主档 item_rank（A/停售 跳过，仅 B/C 出建议）
    """
    from shared.db import get_connection
    conn = get_connection()

    try:
        rows = conn.execute("""
            SELECT im.item_code AS sku,
                   MIN(im.display_name) AS name,
                   MIN(im.item_rank) AS item_rank,
                   SUM(s.revenue) AS revenue,
                   SUM(s.gross_profit) AS gp,
                   SUM(s.qty_sold) AS qty_sold,
                   MAX(inv.qty_on_hand) AS qty,
                   MAX(im.cost_estimate) AS std_cost
            FROM nst.sales_monthly s
            JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id
            LEFT JOIN (
                SELECT item_internal_id, qty_on_hand FROM nst.inventory_snapshot
                WHERE warehouse = 'JD-物流-千葉'
                  AND snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)
            ) inv ON inv.item_internal_id = s.item_internal_id
            WHERE s.year_month = ?
            GROUP BY im.item_code
        """, (year_month,)).fetchall()

        results = []
        for r in rows:
            sku = r["sku"]
            if not sku:
                continue
            rank = _norm_rank(r["item_rank"])
            revenue = float(r["revenue"] or 0)
            gp = float(r["gp"] or 0)
            qty_sold = float(r["qty_sold"] or 0)
            qty = float(r["qty"] or 0)
            std_cost = float(r["std_cost"] or 0)
            margin_pct = (gp / revenue * 100) if revenue else 0
            m_turn = (qty_sold / qty) if qty else 0
            adv = advise(rank, margin_pct, m_turn)
            if adv["advice"] == "—":
                continue
            results.append({
                "sku": sku,
                "name": r["name"] or sku,
                "rank": rank,
                "margin_pct": round(margin_pct, 1),
                "monthly_turnover": round(m_turn, 3),
                "cross_ratio": round(margin_pct * m_turn / 100, 2),
                "inventory_value": round(qty * std_cost, 0),
                **adv,
            })

        # 按"重点"优先 + 库存价值降序
        priority_order = {
            "🔥 重点提价": 0, "🔥 重点降价": 0,
            "⬆️ 提价候选": 1, "⚠️ 降价候选": 1,
            "⬇️ 降级候选": 2, "✅ 维持": 3,
        }
        results.sort(key=lambda x: (priority_order.get(x["advice"], 9), -x["inventory_value"]))

        # 持久化（DELETE+INSERT·避免 SQLite 専用 INSERT OR REPLACE 在 PG 失败）
        if persist and results:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute("DELETE FROM operation_advice_monthly WHERE year_month = ?", (year_month,))
            conn.executemany("""
                INSERT INTO operation_advice_monthly
                (sku, year_month, rank, margin_pct, monthly_turnover, cross_ratio,
                 margin_lv, turnover_lv, advice, reason, inventory_value, calculated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (r["sku"], year_month, r["rank"], r["margin_pct"], r["monthly_turnover"],
                 r["cross_ratio"], r["margin_lv"], r["turnover_lv"], r["advice"],
                 r["reason"], r["inventory_value"], now)
                for r in results
            ])
            conn.commit()

        return results

    finally:
        conn.close()
