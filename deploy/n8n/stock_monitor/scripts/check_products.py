"""周度执行：扫描所有 JAN → 对比上周状态 → 生成差异报告 → 飞书通知

主数据源: NETde卸（按 JAN 索引，命中率 ~20%，可检出 【販売終了】）
SUPER DELIVERY 不按 JAN 索引，暂保留接口不启用。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from config import (
    CSV_SRC, STATE_DIR, REPORTS_DIR, LOGS_DIR,
    COOKIE_NETDEOROSHI, COOKIE_SUPERDELIVERY,
)
from scraper import (
    lookup_netdeoroshi, lookup_superdelivery,
    _build_opener, _load_cookies, throttle,
)

# SQLite 双写路径
WAREHOUSE_DB = "/Users/joe/CC/商品信息管理/data_warehouse/warehouse.db"

STATE_FILE = STATE_DIR / "last_status.json"


def load_jan_list(limit: int | None = None) -> list[dict]:
    items = []
    with open(CSV_SRC, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            jan = row.get("jan", "").strip()
            if not jan:
                continue
            items.append({
                "jan": jan,
                "name": row.get("商品名", ""),
                "maker": row.get("メーカー名", ""),
                "rank": row.get("ランク", ""),
            })
            if limit and len(items) >= limit:
                break
    return items


def load_prev_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def scan_all(items: list[dict], use_sd: bool = False) -> dict[str, dict]:
    opener_nd = _build_opener(_load_cookies(COOKIE_NETDEOROSHI))
    opener_sd = _build_opener(_load_cookies(COOKIE_SUPERDELIVERY)) if use_sd else None

    results: dict[str, dict] = {}
    total = len(items)
    for i, it in enumerate(items, 1):
        jan = it["jan"]
        r_nd = lookup_netdeoroshi(jan, opener_nd)
        throttle()

        entry = {
            "name": it["name"],
            "maker": it["maker"],
            "status": r_nd.status,
            "matched_name": r_nd.matched_name,
            "matched_id": r_nd.matched_product_id,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "source": "netdeoroshi",
        }

        if use_sd:
            r_sd = lookup_superdelivery(jan, opener_sd)
            throttle()
            entry["sd_status"] = r_sd.status

        results[jan] = entry

        if i % 100 == 0 or i == total:
            print(f"[{i}/{total}] 完了", flush=True)

    return results


def diff_status(prev: dict, curr: dict) -> dict:
    """
    变化类型：
      - newly_discontinued: 本周首次被标记 【販売終了】
      - disappeared       : 原本能在 NETde卸 找到，现在 notfound
      - newly_found       : 原本 notfound，本周出现（仅记录，不告警）
    """
    newly_discontinued = []
    disappeared = []
    newly_found = []

    for jan, info in curr.items():
        prev_info = prev.get(jan, {})
        prev_status = prev_info.get("status")
        cur_status = info["status"]

        meta = {
            "jan": jan,
            "name": info["name"],
            "maker": info["maker"],
            "matched_name": info.get("matched_name"),
        }

        if prev_status == "active" and cur_status == "discontinued":
            newly_discontinued.append(meta)
        elif prev_status == "active" and cur_status == "notfound":
            disappeared.append(meta)
        elif prev_status in (None, "notfound") and cur_status == "discontinued":
            # 第一次观察到就已经是停产，仅记录
            newly_discontinued.append(meta)
        elif prev_status == "notfound" and cur_status == "active":
            newly_found.append(meta)

    return {
        "newly_discontinued": newly_discontinued,
        "disappeared": disappeared,
        "newly_found": newly_found,
    }


def write_to_sqlite(alerts: list[dict]):
    """写入 discontinue_alerts 表（幂等）。

    v2 决策：跳过已经「取扱中止」/「メーカー取扱中止」的 SKU 的 alerts
    （这些已经停售，不需再扫改廃信号）。

    alerts: [{jan, sku, source, signal_type, detected_at, ...}]
    """
    try:
        import sqlite3
    except ImportError:
        print("Warning: sqlite3 not available, skipping DB write")
        return

    try:
        con = sqlite3.connect(WAREHOUSE_DB)

        # 取已停售 SKU 列表（按 jan 索引）
        halted_jans = set()
        try:
            for r in con.execute("""
                SELECT DISTINCT upc FROM nst_inventory_snapshot
                WHERE handling_status IN ('取扱中止', 'メーカー取扱中止')
            """):
                if r[0]: halted_jans.add(r[0])
        except sqlite3.OperationalError:
            pass  # 表不存在时跳过过滤

        skipped = 0
        written = 0
        for alert in alerts:
            jan = alert.get("jan")
            if jan in halted_jans:
                skipped += 1
                continue
            con.execute("""
                INSERT OR IGNORE INTO discontinue_alerts
                (jan, sku, source, signal_type, detected_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                jan,
                alert.get("sku"),
                alert.get("source", "netdeoroshi"),
                alert.get("signal_type", "販売終了"),
                alert.get("detected_at")
            ))
            written += 1
        con.commit()
        con.close()
        if alerts:
            print(f"SQLite: {written} alerts written, {skipped} 已停售 SKU 跳过")
    except Exception as e:
        print(f"Warning: SQLite write failed: {e}")


def write_report(curr: dict, diff: dict, run_date: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"report_{run_date}.md"

    total = len(curr)
    by_status = {"active": 0, "discontinued": 0, "notfound": 0, "error": 0}
    for v in curr.values():
        by_status[v["status"]] = by_status.get(v["status"], 0) + 1

    lines = [f"# 停产监控报告 {run_date}", ""]
    lines.append("## 本轮状态汇总")
    lines.append(f"- 商品总数: {total}")
    lines.append(f"- NETde卸 在售: {by_status['active']}")
    lines.append(f"- NETde卸 停产: {by_status['discontinued']}")
    lines.append(f"- NETde卸 无记录: {by_status['notfound']}")
    lines.append(f"- 错误: {by_status['error']}")
    lines.append("")
    lines.append("## 本轮变化")
    lines.append(f"- 新增停产: **{len(diff['newly_discontinued'])}**")
    lines.append(f"- 从站点消失: **{len(diff['disappeared'])}**")
    lines.append(f"- 新增上架: {len(diff['newly_found'])}")

    def _render(title: str, entries: list):
        if not entries:
            return
        lines.append("")
        lines.append(title)
        for e in entries:
            row = f"- `{e['jan']}` {e['maker']} / {e['name']}"
            if e.get("matched_name"):
                row += f"  ←  \"{e['matched_name']}\""
            lines.append(row)

    _render("## 🔴 新增停产", diff["newly_discontinued"])
    _render("## ⚠ 从 NETde卸 消失", diff["disappeared"])
    _render("## 🟢 新增上架", diff["newly_found"])

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="仅扫描前 N 条 JAN（调试用）")
    parser.add_argument("--no-lark", action="store_true",
                        help="跳过飞书通知")
    parser.add_argument("--no-log-file", action="store_true",
                        help="不重定向到日志文件")
    parser.add_argument("--use-sd", action="store_true",
                        help="同时查询 SUPER DELIVERY（默认关闭）")
    args = parser.parse_args()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    run_date = datetime.now().strftime("%Y%m%d")
    log_path = LOGS_DIR / f"run_{run_date}.log"

    if not args.no_log_file:
        sys.stdout = open(log_path, "w", encoding="utf-8")
        sys.stderr = sys.stdout

    print(f"== 停产监控 start  {datetime.now().isoformat(timespec='seconds')} ==")
    items = load_jan_list(args.limit)
    print(f"商品数: {len(items)}")

    prev = load_prev_state()
    curr = scan_all(items, use_sd=args.use_sd)
    save_state(curr)

    diff = diff_status(prev, curr)
    report = write_report(curr, diff, run_date)
    print(f"报告: {report}")

    if not args.no_lark:
        has_change = bool(diff["newly_discontinued"]) or bool(diff["disappeared"])
        if has_change:
            from notify_lark import send_summary
            send_summary(diff, str(report))
        else:
            print("无关键变动，跳过飞书通知")

    # SQLite 双写：将变化写入 warehouse.db
    run_timestamp = datetime.now().isoformat(timespec="seconds")
    alerts_to_write = []
    for item in diff["newly_discontinued"]:
        alerts_to_write.append({
            "jan": item.get("jan"),
            "sku": None,
            "source": "netdeoroshi",
            "signal_type": "販売終了",
            "detected_at": run_timestamp,
        })
    for item in diff["disappeared"]:
        alerts_to_write.append({
            "jan": item.get("jan"),
            "sku": None,
            "source": "netdeoroshi",
            "signal_type": "消失",
            "detected_at": run_timestamp,
        })
    if alerts_to_write:
        write_to_sqlite(alerts_to_write)

    print(f"== end  {datetime.now().isoformat(timespec='seconds')} ==")


if __name__ == "__main__":
    main()
