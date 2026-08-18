"""未来半年（M1〜M6）の供給先別・月別 発注計画生成スクリプト (Boss 2026-05-14).

目的:
  ・各仕入先に「半年分の確約発注ベース数量」を通知できる Excel を作る
  ・運営が毎月の確認時に「変動係数」を掛けて微調整 → 欠品 / 滞留を防ぐ

ロジック:
  ・compute_recommendations を呼んで A/B ランク推奨を取得 (Boss 2026-05-14 公式)
  ・M1 (今月分・初回) = engine の suggested_qty  (2.5 ヶ月在庫まで補充)
  ・M2〜M6 (定期分) = rec_monthly を ケース(case_qty) で天井丸め
        - 月間ベース箱数 = CEIL(rec_monthly / pack)
        - 月間ベース数量 = base_boxes × pack
  ・運営側で 月別 × 0.7〜1.3 の変動係数を掛けて確認後発注 (UI / 手動)

出力 (~/Desktop/ に Excel):
  Sheet 1: 説明                 — ロジック / 公式 / 使い方
  Sheet 2: 供給先サマリ          — 供給先 × 月の発注金額マトリクス + 半年合計
  Sheet 3: 月別サマリ            — M1〜M6 のグランドトータル
  Sheet 4: M1-初回発注 (補充)    — JAN 行明細
  Sheet 5〜9: M2〜M6 (定期)       — 同上
  Sheet 10〜N: 供給先-XXX        — 上位供給先ごとに 半年分明細
  Sheet 最後: 要人工確認         — needs_review (ケース欠落 / 箱規過大)
"""
from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import get_connection  # noqa: E402
from shared.purchase_engine import (  # noqa: E402
    NO_LOT_SUPPLIERS,
    ZONE_LABEL_JA,
    compute_recommendations,
)

DESKTOP = Path.home() / "Desktop"
OUT_PATH = DESKTOP / f"{date.today().isoformat()}_半年発注計画_AB.xlsx"

# 半年分のラベル (実装時の今月 = M1)。Boss 側は「未来半年」と言っているので
# 今月 (2026-05) を初回 = M1 とする。
TODAY = date.today()
MONTH_LABELS = []
y, m = TODAY.year, TODAY.month
for i in range(6):
    MONTH_LABELS.append(f"{y}-{m:02d}")
    m += 1
    if m == 13:
        m = 1
        y += 1
M1, M2, M3, M4, M5, M6 = MONTH_LABELS


def _pack_size(row) -> int:
    """発注単位を取得 (NO_LOT_SUPPLIERS は 1, それ以外は case_qty → lot_size → 1)。"""
    if row["supplier_name"] in NO_LOT_SUPPLIERS:
        return 1
    cq = row.get("case_qty")
    if cq and cq > 0:
        return int(cq)
    lot = row.get("lot_size")
    if lot and lot > 0:
        return int(lot)
    return 1


def _monthly_base_qty(rec_monthly: float, pack: int) -> int:
    """定期月 (M2〜M6) のベース発注数量。
    rec_monthly を pack で 天井丸め → 月販トレンドを下回らない最小 ケース。
    rec_monthly が 0 以下の場合は 0 (発注しない)。"""
    if rec_monthly <= 0:
        return 0
    return math.ceil(rec_monthly / pack) * pack


def build_plan() -> pd.DataFrame:
    """SKU × 月 の発注ベース表を作る (M1 = engine 補充, M2〜M6 = 定期)。"""
    conn = get_connection()
    df = compute_recommendations(
        conn,
        months=4,
        ranks=("Aランク", "Bランク"),
        consolidate_by_brand=True,
        optimize="zone",
    )
    if df.empty:
        raise SystemExit("発注推奨が 0 件 — nst.sales_monthly / supplier_quote のデータを確認")

    # 状態 recommended + needs_review (出はするが要確認) を含める
    df = df[df["status"].isin(["recommended", "needs_review"])].copy().reset_index(drop=True)
    df["pack_size"] = df.apply(_pack_size, axis=1)
    df["zone_ja"] = df["zone"].map(ZONE_LABEL_JA).fillna(df["zone"])

    # M1 = engine の suggested_qty (初回・在庫補充, 2.5 ヶ月までに引き上げ)
    df[f"{M1}_qty"] = df["suggested_qty"].astype(int)
    df[f"{M1}_boxes"] = df["boxes"].astype(int)

    # M2〜M6 = rec_monthly を ケース丸め (月間定期発注ベース)
    df["base_qty"] = df.apply(lambda r: _monthly_base_qty(r["rec_monthly"], r["pack_size"]), axis=1)
    df["base_boxes"] = df.apply(
        lambda r: 0 if r["pack_size"] == 0 else int(r["base_qty"] // max(r["pack_size"], 1)),
        axis=1,
    )
    for label in (M2, M3, M4, M5, M6):
        df[f"{label}_qty"] = df["base_qty"]
        df[f"{label}_boxes"] = df["base_boxes"]

    # 金額
    for label in MONTH_LABELS:
        df[f"{label}_amount"] = (df[f"{label}_qty"] * df["unit_price"]).round().astype(int)
    df["half_year_qty"] = df[[f"{l}_qty" for l in MONTH_LABELS]].sum(axis=1).astype(int)
    df["half_year_amount"] = df[[f"{l}_amount" for l in MONTH_LABELS]].sum(axis=1).astype(int)

    return df


def _supplier_matrix(plan: pd.DataFrame) -> pd.DataFrame:
    """供給先 × 月 の発注金額マトリクス + 半年合計 + SKU 数。"""
    rows = []
    for sup, g in plan.groupby("supplier_name"):
        row = {
            "供給先": sup,
            "zone": g["zone_ja"].iloc[0],
            "NSTコード": g["nst_supplier_code"].iloc[0] or "",
            "SKU数": g["jan"].nunique(),
            "要人工確認": int((g["status"] == "needs_review").sum()),
        }
        for label in MONTH_LABELS:
            row[f"{label} 金額"] = int(g[f"{label}_amount"].sum())
        row["半年合計"] = int(g["half_year_amount"].sum())
        row["最低受注額"] = int(g["min_order_amount"].iloc[0] or 0)
        row["納期"] = g["lead_time_text"].iloc[0] or ""
        rows.append(row)
    out = pd.DataFrame(rows).sort_values("半年合計", ascending=False).reset_index(drop=True)
    return out


def _monthly_summary(plan: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for i, label in enumerate(MONTH_LABELS):
        boxes_col = f"{label}_boxes"
        qty_col = f"{label}_qty"
        amount_col = f"{label}_amount"
        nz = plan[plan[qty_col] > 0]
        rows.append({
            "月": label,
            "区分": "M1 初回(補充)" if i == 0 else f"M{i+1} 定期",
            "供給先数": nz["supplier_name"].nunique(),
            "SKU数": nz["jan"].nunique(),
            "総箱数": int(nz[boxes_col].sum()),
            "総数量": int(nz[qty_col].sum()),
            "総金額": int(nz[amount_col].sum()),
        })
    rows.append({
        "月": "半年合計", "区分": "",
        "供給先数": plan["supplier_name"].nunique(),
        "SKU数": plan["jan"].nunique(),
        "総箱数": int(plan[[f"{l}_boxes" for l in MONTH_LABELS]].sum(axis=1).sum()),
        "総数量": int(plan["half_year_qty"].sum()),
        "総金額": int(plan["half_year_amount"].sum()),
    })
    return pd.DataFrame(rows)


def _month_detail(plan: pd.DataFrame, label: str, is_first: bool) -> pd.DataFrame:
    qty_col = f"{label}_qty"
    box_col = f"{label}_boxes"
    amt_col = f"{label}_amount"
    out = plan[plan[qty_col] > 0].copy()
    out = out[[
        "supplier_name", "zone_ja", "nst_supplier_code", "maker", "jan", "display_name",
        "rank", "trend", "trend_factor", "rec_monthly", "unit_price", "pack_size",
        box_col, qty_col, amt_col, "status",
    ]].rename(columns={
        "supplier_name": "供給先", "zone_ja": "zone", "nst_supplier_code": "NSTコード",
        "maker": "メーカー", "jan": "JAN", "display_name": "品名",
        "rank": "ランク", "trend": "トレンド", "trend_factor": "係数",
        "rec_monthly": "推奨月販", "unit_price": "単価", "pack_size": "ケース入数",
        box_col: "箱数", qty_col: "数量", amt_col: "金額(¥)", "status": "状態",
    })
    out["備考"] = ("M1 初回 (2.5ヶ月補充)" if is_first else "M2-M6 定期 (rec_monthly ケース丸め)")
    return out.sort_values(["供給先", "メーカー", "JAN"]).reset_index(drop=True)


def _supplier_detail(plan: pd.DataFrame, supplier: str) -> pd.DataFrame:
    g = plan[plan["supplier_name"] == supplier].copy()
    cols_base = ["maker", "jan", "display_name", "rank", "rec_monthly", "unit_price", "pack_size"]
    monthly_cols = []
    for label in MONTH_LABELS:
        monthly_cols.extend([f"{label}_boxes", f"{label}_qty", f"{label}_amount"])
    g = g[cols_base + monthly_cols + ["half_year_qty", "half_year_amount", "status"]].rename(columns={
        "maker": "メーカー", "jan": "JAN", "display_name": "品名",
        "rank": "ランク", "rec_monthly": "推奨月販", "unit_price": "単価",
        "pack_size": "ケース入数",
        "half_year_qty": "半年合計数量", "half_year_amount": "半年合計金額", "status": "状態",
    })
    rename_monthly = {}
    for label in MONTH_LABELS:
        rename_monthly[f"{label}_boxes"] = f"{label} 箱"
        rename_monthly[f"{label}_qty"] = f"{label} 数"
        rename_monthly[f"{label}_amount"] = f"{label} ¥"
    g = g.rename(columns=rename_monthly)
    return g.sort_values(["メーカー", "JAN"]).reset_index(drop=True)


def _needs_review_sheet(plan: pd.DataFrame) -> pd.DataFrame:
    nr = plan[plan["status"] == "needs_review"].copy()
    if nr.empty:
        return nr
    return nr[[
        "supplier_name", "jan", "display_name", "maker", "rank",
        "rec_monthly", "unit_price", "case_qty", "lot_size", "pack_size",
        "needs_review_pack", "needs_review_oversize",
        f"{M1}_qty", f"{M1}_amount", "half_year_qty", "half_year_amount", "reason",
    ]].rename(columns={
        "supplier_name": "供給先", "jan": "JAN", "display_name": "品名", "maker": "メーカー",
        "rank": "ランク", "rec_monthly": "推奨月販", "unit_price": "単価",
        "case_qty": "ケース", "lot_size": "ロット", "pack_size": "採用入数",
        "needs_review_pack": "⚠️入数未設定", "needs_review_oversize": "⚠️箱規過大",
        f"{M1}_qty": "M1(初回)数量", f"{M1}_amount": "M1(初回)金額",
        "half_year_qty": "半年数量", "half_year_amount": "半年金額", "reason": "理由",
    }).reset_index(drop=True)


def _description_sheet() -> pd.DataFrame:
    text = [
        ("📦 半年発注計画 v1", ""),
        ("対象", "AB ランク + 取扱中・実績ありの SKU"),
        ("生成日", date.today().isoformat()),
        ("公式 (Boss 2026-05-14)", ""),
        ("  推奨月販", "= max(平均月販, 直近月販) × トレンド係数(↑1.2/→1.0/↓0.7) ← 直近 4 ヶ月"),
        ("  実質在庫", "= JD-物流-千葉 手持 + 注文済 (弁天は除外)"),
        ("  目標在庫", "= 推奨月販 × 1.5 ヶ月"),
        ("  M1 (初回) 数量", "= CEIL((推奨月販 × 2.5 − 実質在庫) / ケース入数) × ケース入数"),
        ("  M2〜M6 数量", "= CEIL(推奨月販 / ケース入数) × ケース入数 (毎月 1 ヶ月分定期)"),
        ("使い方", ""),
        ("  1", "「供給先サマリ」で月別金額 → 各仕入先へ「半年確約ベース」として通知"),
        ("  2", "毎月の発注前に運営が「変動係数(0.7〜1.3)」を月別数量に掛けて確認 → 欠品/滞留調整"),
        ("  3", "M1 だけは初回・補充用のため 2.5ヶ月在庫まで一括で引き上げる"),
        ("  4", "「要人工確認」シートを必ずチェック → ケース未設定 / 箱規が月販の 2.5 倍超"),
        ("注意", ""),
        ("  ・", "ハリマ / SD は応急用 → ロット無視で必要数ぴったり発注 (NO_LOT_SUPPLIERS)"),
        ("  ・", "JD直送 > 弁天経由 > 応急 > 前払い の zone 優先で仕入先を選定"),
        ("  ・", "メーカー単位で 最大 3 社に集約 (品牌集約)。小品牌 (≤5 SKU) は集約対象外"),
        ("  ・", "弁天は自社倉庫 → 中継費なし (markup 撤廃済)"),
    ]
    return pd.DataFrame(text, columns=["項目", "内容"])


def main() -> None:
    print(f"▶ 半年発注計画を生成中... (出力先: {OUT_PATH})")
    plan = build_plan()
    print(f"  推奨 + 要確認 = {len(plan)} 行 ({plan['supplier_name'].nunique()} 仕入先)")

    sup_mx = _supplier_matrix(plan)
    mon_sum = _monthly_summary(plan)
    needs_rev = _needs_review_sheet(plan)
    descr = _description_sheet()

    DESKTOP.mkdir(exist_ok=True)
    with pd.ExcelWriter(OUT_PATH, engine="openpyxl") as xw:
        descr.to_excel(xw, sheet_name="📖説明", index=False)
        sup_mx.to_excel(xw, sheet_name="供給先サマリ", index=False)
        mon_sum.to_excel(xw, sheet_name="月別サマリ", index=False)

        # 月別明細 (M1=初回, M2〜M6=定期)
        for i, label in enumerate(MONTH_LABELS):
            det = _month_detail(plan, label, is_first=(i == 0))
            sheet = f"{label} ({'初回' if i == 0 else '定期'})"
            det.to_excel(xw, sheet_name=sheet[:31], index=False)

        # 上位 15 仕入先ごと 半年明細
        for sup in sup_mx.head(15)["供給先"]:
            sd = _supplier_detail(plan, sup)
            sheet = f"📦{sup}"[:31]
            sd.to_excel(xw, sheet_name=sheet, index=False)

        if not needs_rev.empty:
            needs_rev.to_excel(xw, sheet_name="⚠️要人工確認", index=False)

    print(f"✅ 出力完了 → {OUT_PATH}")
    print(f"  半年総額: ¥{plan['half_year_amount'].sum():,.0f}")
    print(f"  M1 ({M1} 初回): ¥{plan[f'{M1}_amount'].sum():,.0f}")
    print(f"  M2〜M6 月間ベース: ¥{plan[f'{M2}_amount'].sum():,.0f}/月")
    print(f"  仕入先数: {plan['supplier_name'].nunique()}")
    print(f"  SKU 数: {plan['jan'].nunique()}")
    print(f"  要人工確認: {len(needs_rev)} 行")


if __name__ == "__main__":
    main()
