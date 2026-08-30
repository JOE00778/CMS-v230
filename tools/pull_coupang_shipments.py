"""Coupang の発送対象注文を CMS へ取り込む（元川機の定時タスクから叩く）。

Boss 2026-08-30「每天定点拉取 coupang 数据，临时放到 CMS 中，发送完后可以删掉，
数据大概留一个星期就 OK」。

やること 3 つだけ:
  1. Coupang から発送前（ACCEPT / INSTRUCT）の箱を引く
  2. 商品マスタと突き合わせて coupang_shipment_queue に入れる（**送信済みは上書きしない**）
  3. 7 日を過ぎた行を消す（この表は受取人氏名・電話・住所・PCCC を持つ）

ECMS へは**送らない**。送信は運営が page41 で中身を確認してからボタンを押す。

必要な環境変数（元川 .env · streamlit と同じもの）:
  COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY / COUPANG_VENDOR_ID
  DATABASE_URL（prod PG）
  COUPANG_KRW_USD_RATE（任意 · 既定 0.00068 = 運営 Excel の固定係数）

用法:
  python tools/pull_coupang_shipments.py --days 3
  python tools/pull_coupang_shipments.py --dry-run    # 引くだけ、書かない
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import coupang_client as cp          # noqa: E402
from shared import coupang_ecms as ce            # noqa: E402
from shared import coupang_store as store        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="直近何日ぶんを引くか")
    ap.add_argument("--dry-run", action="store_true", help="引くだけで書かない")
    args = ap.parse_args()

    if not cp.is_configured():
        print("NG: COUPANG_ACCESS_KEY / SECRET_KEY / VENDOR_ID 未設定", file=sys.stderr)
        return 2

    try:
        boxes = cp.fetch_shippable(days=args.days)
    except cp.CoupangError as e:
        print(f"NG: Coupang 取得失敗: {e}", file=sys.stderr)
        return 1

    pm = store.product_map()
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    rows = [ce.to_queue_row(b, pm.get, now) for b in boxes]
    incomplete = sum(1 for r in rows if ce.missing_fields(r))

    if args.dry_run:
        print(f"dry-run: 取得={len(boxes)} 要確認={incomplete} マスタ={len(pm)} · 書き込みなし")
        return 0

    saved, skipped = store.upsert_queue(rows)
    purged = store.purge_old()
    print(f"ok={saved} skipped={skipped} purged={purged} "
          f"取得={len(boxes)} 要確認={incomplete} マスタ={len(pm)}")
    if incomplete:
        print(f"注意: {incomplete} 件は必須項目が欠けている（英語品名 / 重量 / PCCC など）。"
              f"page41「🇰🇷 Coupang」で確認してから送信すること", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
