"""每日库存构成 飞书汇报 · 复用 CMS page06「构成 KPI（全仓库合算）」单一事实源.

数据口径与 pages/06_📦_库存监控.py 的 _render_composition() 完全一致:
  · 全仓库 = nst.inventory_snapshot（JD-物流-千葉 / CGF倉庫（COUPANG））
    + 弁天倉庫(nst.inventory_bin_snapshot · bin 単位で用途判定)
  · 金额 = qty_on_hand × item_master_raw.cost_estimate
  · 用途分类 CASE: JD 全归「输出」; 弁天按 nst.bin_category 标识(优先级 返品>不良品>输出中国>输出)

汇报结构（Boss 2026-06-25 拍板）:
  顶层 4 桶: 通常输出 / 输出中国 / 返品(HENPIN-EX) / 不良品(FF-3) + 库存合计 + 总数量
  └ 通常输出 内部再按「等级」细分: Rank A/B/C / NEW / 中止 / 未分类（口径同 page06 各等级库存金额）

  📥 前日入库段（Boss 2026-06-30 追加）: 口径同 page30「📦 入库状态 → 前日入库」tab —
     收货单数 / SKU数(=COUNT DISTINCT item·非明细行数) / 检收金额 / 供货商数。
     前日无数据则兜底落最新收货日。取数失败只省略该段、不影响库存汇报主体。

发送: 飞书群「自定义机器人」webhook（零权限・与 trend-radar 同款）。
  · LARK_DIGEST_WEBHOOK         群机器人 webhook URL（必填，非 dry-run）
  · LARK_DIGEST_WEBHOOK_SECRET  若机器人开了「签名校验」则填，否则留空
  · DATABASE_URL                prod PG 连接串（元川机已配）

用法:
  python tools/daily_lark_digest.py --dry-run   # 只打印卡片 JSON，不发送（本地/校验用）
  python tools/daily_lark_digest.py             # 取数 + 发群（元川机定时任务调用）
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

import psycopg2
from psycopg2.extras import DictCursor

# page06 _render_composition() 的口径搬运（保持单一事实源）。
# 在原 4 类别分组上多带一列 item_rank，用于「通常输出」内部按等级细分。
_SQL = """
WITH inv_all AS (
    SELECT warehouse, item_internal_id, qty_on_hand, NULL::TEXT AS bin_number
    FROM nst.inventory_snapshot
    WHERE snapshot_date = %(d_jd)s AND warehouse <> '弁天倉庫'
    UNION ALL
    SELECT '弁天倉庫' AS warehouse, item_internal_id, qty_on_hand, bin_number
    FROM nst.inventory_bin_snapshot
    WHERE snapshot_date = %(d_bn)s
)
SELECT
    CASE
        WHEN inv_all.warehouse <> '弁天倉庫' THEN '输出'
        WHEN COALESCE(bc.is_return, FALSE) THEN '返品'
        WHEN COALESCE(bc.is_defect, FALSE) THEN '不良品'
        WHEN COALESCE(bc.is_cb,     FALSE) THEN '输出中国'
        ELSE '输出'
    END AS category,
    im.item_rank                                                  AS item_rank,
    COUNT(DISTINCT inv_all.item_internal_id)                      AS sku,
    SUM(inv_all.qty_on_hand)                                      AS qty,
    SUM(inv_all.qty_on_hand * COALESCE(im.cost_estimate, 0))::NUMERIC(16,2) AS amt
FROM inv_all
LEFT JOIN nst.item_master_raw im ON im.internal_id = inv_all.item_internal_id
LEFT JOIN nst.bin_category bc     ON bc.bin_number  = inv_all.bin_number
GROUP BY 1, 2
"""

# 顶层 4 桶固定顺序 + 标签（与 page06 _labels 一致）
_CATEGORIES = ("输出", "输出中国", "返品", "不良品")
_LABELS = {
    "输出": "通常输出",
    "输出中国": "输出中国",
    "返品": "返品（HENPIN-EX）",
    "不良品": "返品（全损）",
}

# 等级 raw → 展示名 + 固定顺序（口径同 page06 _RANK_ORDER；中止 = 取扱中止）
_RANK_DISPLAY = {
    "Aランク": "Rank A", "Bランク": "Rank B", "Cランク": "Rank C",
    "NEW": "NEW", "取扱中止": "中止",
}
_RANK_ORDER = ["Rank A", "Rank B", "Rank C", "NEW", "中止", "未分类"]


def _rank_label(raw) -> str:
    s = "" if raw is None else str(raw).strip()
    if s in ("", "nan", "None"):
        return "未分类"
    return _RANK_DISPLAY.get(s, s)


def build_composition(conn) -> dict:
    """复用 page06 口径取「构成 KPI」+ 通常输出内部等级细分。"""
    cur = conn.cursor(cursor_factory=DictCursor)
    cur.execute("SELECT max(snapshot_date) AS d FROM nst.inventory_snapshot")
    inv_date = cur.fetchone()["d"]
    cur.execute("SELECT max(snapshot_date) AS d FROM nst.inventory_bin_snapshot")
    row = cur.fetchone()
    bin_date = row["d"] if row else None
    if not inv_date:
        raise RuntimeError("nst.inventory_snapshot 无快照数据")

    cur.execute(_SQL, {"d_jd": inv_date, "d_bn": bin_date or inv_date})

    cat_agg: dict[str, dict] = {}          # category -> {qty, amt}
    shutsu_rank: dict[str, dict] = {}      # 仅 category='输出' 的 rank_label -> {qty, amt}
    for r in cur.fetchall():
        cat = r["category"]
        qty = float(r["qty"] or 0)
        amt = float(r["amt"] or 0)
        a = cat_agg.setdefault(cat, {"qty": 0.0, "amt": 0.0})
        a["qty"] += qty
        a["amt"] += amt
        if cat == "输出":
            rl = _rank_label(r["item_rank"])
            b = shutsu_rank.setdefault(rl, {"qty": 0.0, "amt": 0.0, "sku": 0})
            b["qty"] += qty
            b["amt"] += amt
            b["sku"] += int(r["sku"] or 0)

    tot_amt = sum(v["amt"] for v in cat_agg.values())
    tot_qty = sum(v["qty"] for v in cat_agg.values())

    buckets = []
    for cat in _CATEGORIES:
        v = cat_agg.get(cat, {"qty": 0.0, "amt": 0.0})
        buckets.append({
            "label": _LABELS[cat], "amt": v["amt"], "qty": v["qty"],
            "pct": (v["amt"] / tot_amt * 100) if tot_amt else 0.0,
        })

    # 通常输出内部等级细分（占比 = 占通常输出，便于看清主仓结构）
    shutsu_amt = cat_agg.get("输出", {}).get("amt", 0.0)
    ordered = [rl for rl in _RANK_ORDER if rl in shutsu_rank]
    ordered += [rl for rl in shutsu_rank if rl not in _RANK_ORDER]  # 兜底未知等级
    rank_rows = [{
        "label": rl, "amt": shutsu_rank[rl]["amt"], "qty": shutsu_rank[rl]["qty"],
        "sku": shutsu_rank[rl]["sku"],
        "pct": (shutsu_rank[rl]["amt"] / shutsu_amt * 100) if shutsu_amt else 0.0,
    } for rl in ordered]

    return {
        "inv_date": str(inv_date),
        "bin_date": str(bin_date) if bin_date else "（暂无）",
        "tot_amt": tot_amt, "tot_qty": tot_qty,
        "buckets": buckets, "shutsu_amt": shutsu_amt, "rank_rows": rank_rows,
    }


def build_receipt_yesterday(conn) -> dict:
    """前日（昨天）一天的收货 4 指标 · 口径同 page30「📦 入库状态 → 前日入库」tab。
    前日无数据则兜底落最新有收货的一天（与 shared/receipt_status_view._render_yesterday 一致）。
    SKU数 = COUNT(DISTINCT item_internal_id)（非明细行数·±修正行不重复计 SKU）。"""
    cur = conn.cursor(cursor_factory=DictCursor)
    target = str(dt.date.today() - dt.timedelta(days=1))
    cur.execute("SELECT 1 FROM nst.item_receipt_line WHERE receipt_date = %s LIMIT 1", (target,))
    if cur.fetchone() is None:
        cur.execute("SELECT MAX(receipt_date) AS d FROM nst.item_receipt_line")
        row = cur.fetchone()
        latest = row["d"] if row else None
        if latest is not None and str(latest) != target:
            target = str(latest)

    cur.execute(
        "SELECT COUNT(DISTINCT receipt_no)       AS receipts, "
        "       COUNT(DISTINCT item_internal_id) AS skus, "
        "       COALESCE(SUM(amount), 0)         AS amount, "
        "       COUNT(DISTINCT vendor_id)        AS vendors "
        "FROM nst.item_receipt_line WHERE receipt_date = %s",
        (target,))
    r = cur.fetchone()
    return {
        "date": target,
        "receipts": int(r["receipts"] or 0),
        "skus": int(r["skus"] or 0),
        "amount": float(r["amount"] or 0),
        "vendors": int(r["vendors"] or 0),
    }


def render_card(d: dict, recv: dict | None = None) -> dict:
    """渲染飞书 interactive 卡片（4 构成桶 + 通常输出按等级内訳 + 可选 前日入库段）。"""
    cat_fields = [{
        "is_short": True,
        "text": {"tag": "lark_md",
                 "content": (f"**{b['label']}**\n¥{b['amt']:,.0f}\n"
                             f"{b['pct']:.0f}% 占比 · {b['qty']:,.0f} 数量")},
    } for b in d["buckets"]]

    rank_fields = [{
        "is_short": True,
        "text": {"tag": "lark_md",
                 "content": (f"**{r['label']}**\n¥{r['amt']:,.0f}\n"
                             f"{r['pct']:.0f}% · {r['sku']:,} SKU · {r['qty']:,.0f} 件")},
    } for r in d["rank_rows"]]

    elements = [
        {"tag": "div", "text": {"tag": "lark_md",
            "content": (f"**库存合计** ¥{d['tot_amt']:,.0f}　·　总数量 {d['tot_qty']:,.0f}\n"
                        f"<font color='grey'>快照日 JD {d['inv_date']} · 弁天 {d['bin_date']}</font>")}},
        {"tag": "hr"},
        {"tag": "div", "fields": cat_fields},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
            "content": f"**📦 通常输出 ¥{d['shutsu_amt']:,.0f} · 按等级内訳**（占比＝占通常输出）"}},
    ]
    if rank_fields:
        elements.append({"tag": "div", "fields": rank_fields})

    if recv:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"**📥 前日入库 · {recv['date']}**"}})
        elements.append({"tag": "div", "fields": [
            {"is_short": True, "text": {"tag": "lark_md",
                "content": f"**收货单数**\n{recv['receipts']:,} 单"}},
            {"is_short": True, "text": {"tag": "lark_md",
                "content": f"**SKU 数**\n{recv['skus']:,}"}},
            {"is_short": True, "text": {"tag": "lark_md",
                "content": f"**检收金额**\n¥{recv['amount']:,.0f}"}},
            {"is_short": True, "text": {"tag": "lark_md",
                "content": f"**供货商数**\n{recv['vendors']:,}"}},
        ]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "每日数据汇报 / 通常データ報告"},
        },
        "elements": elements,
    }


def _signed(secret: str) -> dict:
    """飞书自定义机器人「签名校验」: sign = base64(hmac-sha256(key=timestamp+\\n+secret, msg=''))。"""
    ts = str(int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), b"", hashlib.sha256).digest()
    return {"timestamp": ts, "sign": base64.b64encode(digest).decode("utf-8")}


def send(card: dict, webhook: str, secret: str = "") -> dict:
    payload = {"msg_type": "interactive", "card": card}
    if secret:
        payload.update(_signed(secret))
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.loads(r.read())
    if resp.get("code") not in (0, None) and resp.get("StatusCode") not in (0, None):
        raise RuntimeError(f"飞书发送失败: {resp}")
    return resp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印卡片 JSON，不发送")
    ap.add_argument("--db-url", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--webhook", default=os.environ.get("LARK_DIGEST_WEBHOOK", ""))
    ap.add_argument("--secret", default=os.environ.get("LARK_DIGEST_WEBHOOK_SECRET", ""))
    args = ap.parse_args()

    if not args.db_url:
        print("[err] DATABASE_URL 未设置")
        return 2
    conn = psycopg2.connect(args.db_url)
    try:
        data = build_composition(conn)
        try:
            recv = build_receipt_yesterday(conn)
        except Exception as e:          # 前日入库段失败不应拖垮库存汇报主体
            recv = None
            conn.rollback()
            print(f"[warn] 前日入库段取数失败，本次汇报省略该段: {e}")
    finally:
        conn.close()
    card = render_card(data, recv)

    if args.dry_run:
        # ASCII 友好明细（Windows cmd 终端中文会乱码，数字可读；rank 顺序固定 A/B/C/NEW/中止/未分类）
        print(f"[dry-run] tot_amt={data['tot_amt']:,.0f} tot_qty={data['tot_qty']:,.0f} "
              f"shutsu_amt={data['shutsu_amt']:,.0f}")
        for i, b in enumerate(data["buckets"]):
            print(f"  CAT[{i}] amt={b['amt']:,.0f} qty={b['qty']:,.0f} pct={b['pct']:.1f}")
        for i, r in enumerate(data["rank_rows"]):
            print(f"  RANK[{i}] {r['label']!r} sku={r['sku']:,} qty={r['qty']:,.0f} "
                  f"amt={r['amt']:,.0f} pct={r['pct']:.1f}")
        if recv:
            print(f"  RECV date={recv['date']} receipts={recv['receipts']:,} "
                  f"skus={recv['skus']:,} amount={recv['amount']:,.0f} vendors={recv['vendors']:,}")
        else:
            print("  RECV (省略·取数失败或无表)")
        return 0

    if not args.webhook:
        print("[err] LARK_DIGEST_WEBHOOK 未设置（非 dry-run 必填）")
        return 2
    resp = send(card, args.webhook, args.secret)
    print(f"[ok] 已发送 · resp={resp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
