"""自动订货 · 每日作业（元川 09:00 · 任务计划 SmikieAutoOrder → run_auto_order.bat → docker exec cms_streamlit）

每天：到货闭环（🔴 转第二货源 / 🟡 迟到）；周一另附巡检触发清单；每月 1 日另附定期单金额。
发送：飞书 CMS 自建应用 → 测试期只发 Boss（--to 邮箱 · Boss 2026-11-19）。--send-after 之前只写日志不发。
用法：
  python tools/auto_order_job.py --dry-run                 # 打印正文不发
  python tools/auto_order_job.py --to jo@example.com --send-after 2026-11-22
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd  # noqa: E402

from shared import auto_order_engine as E  # noqa: E402
from shared.db import get_readonly_connection  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))


def build(today: dt.date) -> tuple[str, dict]:
    inp = E.load_inputs(get_readonly_connection(), today)
    res = E.compute(inp); s = E.summary(res); trk = E.po_tracking(inp, res)
    wan = lambda x: f"{x / 1e4:,.0f}万"
    L = [f"🤖 自动订货日报 {today}（快照 {inp['snap_date']} · 销量至 {inp['sales_max']}）"]
    cnt = trk.state.value_counts().to_dict() if len(trk) else {}
    red = trk[trk.state == "🔴 转第二货源"] if len(trk) else trk
    out = trk[(trk.state == "⛔ 已断货") & (trk["rank"] != "C")] if len(trk) else trk
    yel = trk[trk.state.isin(["🟠 盯第一货源", "🟡 迟到"]) & (trk["rank"] != "C")] if len(trk) else trk
    L.append("🚚 到货闭环：" + " / ".join(f"{k} {v}" for k, v in cnt.items()) + f" · 在途合计 {len(trk)} 行 {wan(trk.amount.sum()) if len(trk) else '0'}")
    for _, r in out.head(10).iterrows():
        L.append(f"  ⛔ {r.po_number} {r.vendor[:12]} {r['name'][:22]} 已断货 · 计划到货 {r.eta}" + (f" → {r.alt_vendor[:10]} 可下 {int(r.q_alt)} 件" if r.q_alt else " · 无更快货源"))
    for _, r in red.head(10).iterrows():
        L.append(f"  🔴 {r.po_number} {r.vendor[:12]} {r['name'][:22]} 预计断货 {r.stockout} 最晚决策 {r.decide_by} → {r.alt_vendor[:10]} 下 {int(r.q_alt)} 件")
    if len(red) > 10: L.append(f"  …还有 {len(red) - 10} 行，见 CMS「自动订货（测试）→ 到货闭环」")
    for _, r in yel.head(8).iterrows():
        L.append(f"  {r.state[:2]} {r.po_number} {r.vendor[:12]} {r['name'][:22]} 计划 {r.eta} 迟 {r.late_days} 天 · 预计断货 {r.stockout}")
    if today.weekday() == 0 or today.day == 1:
        L.append(f"🚨 本周巡检：A/B {s['emerg_sku']} SKU {wan(s['emerg_amt'])}（现货渠道 {s['emerg_card_sku']} SKU {wan(s['emerg_card_amt'])}）")
    if today.day == 1:
        L.append(f"📅 本月定期单：A/B {s['next_ab_sku']} SKU {wan(s['next_ab_amt'])} · C {s['next_c_sku']} SKU {wan(s['next_c_amt'])}")
    L.append(f"📦 A/B 手持 {wan(s['oh_ab'])} · 在途 {wan(s['pipe_ab'])} · 月销原价 {wan(s['month_cogs_ab'])}")
    return "\n".join(L), dict(red=len(red), out=len(out), yellow=len(yel), emerg=s["emerg_sku"], ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--to", default="")
    ap.add_argument("--send-after", default="2000-01-01"); ap.add_argument("--today", default="")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(JST).date()
    text, meta = build(today)
    print(text); print(json.dumps(meta, ensure_ascii=False))
    if a.dry_run or not a.to or today < dt.date.fromisoformat(a.send_after):
        print(f"[not sent] dry_run={a.dry_run} to={'set' if a.to else 'empty'} send_after={a.send_after}"); return 0
    from shared.lark_openapi import im_send_text
    mid = im_send_text(a.to, "email", text); print(f"[sent] message_id={mid}"); return 0


if __name__ == "__main__":
    sys.exit(main())
