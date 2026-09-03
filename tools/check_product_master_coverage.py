"""商品マスタの 3 項目を、いま社内にあるデータでどこまで埋められるか**実測**する。

Boss 2026-09-03「商品主档的更新，是否能在现在有的数据里拉取呢？」

ECMS 出力で実際に使っているのは 3 つだけ（HSCode は運営の実出力が空なので出さない）:
    英文品名   AH ← いまは運営の Excel だけ
    产品重量   AJ ← 同上
    Product ID AN の URL ← 同上

候補の出所:
    重量       `jdl.v_goods_dimensions.wms_gross_weight_g`（倉庫実測 g · JAN 単位）
    英語品名   `compliance.shopify_item.title`（自建站に上げた商品の英語タイトル · JAN 単位）
    ProductID  Coupang API（受注に入っている。この脚本では見ない）

**推測しない**——実際に何件引けたかを数えて出す。元川で 1 回流せば判断材料になる。

用法（元川）:
  docker exec cms_streamlit python tools/check_product_master_coverage.py
  docker exec cms_streamlit python tools/check_product_master_coverage.py --jans 4901616011007,4901301447647
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.coupang_store import product_map          # noqa: E402
from shared.db import get_connection                  # noqa: E402


def _fetch(sql: str, jans: list[str]) -> tuple[dict, str]:
    marks = ",".join("?" * len(jans))
    conn = get_connection()
    try:
        rows = conn.execute(sql.format(marks=marks), tuple(jans)).fetchall()
        return {str(r[0]): r[1] for r in rows if r[1] not in (None, "")}, ""
    except Exception as e:
        return {}, str(e)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jans", help="カンマ区切り。省略時は商品マスタの全 JAN")
    args = ap.parse_args()

    if args.jans:
        jans = sorted({j.strip() for j in args.jans.split(",") if j.strip()})
        skus = {}
    else:
        skus = product_map()
        jans = sorted({str(v.get("jan") or k.split("_")[0]) for k, v in skus.items()})
    if not jans:
        print("対象 JAN が無い（商品マスタが空。--jans で直接指定もできる）", file=sys.stderr)
        return 2
    print(f"対象: SKU {len(skus)} 件 / JAN {len(jans)} 件\n")

    weights, err_w = _fetch(
        "SELECT jan, wms_gross_weight_g FROM jdl.v_goods_dimensions"
        " WHERE jan IN ({marks})", jans)
    titles, err_t = _fetch(
        "SELECT jan, title FROM compliance.shopify_item WHERE jan IN ({marks})", jans)
    makers, err_m = _fetch(
        "SELECT jan, maker FROM nst.item_master_raw WHERE jan IN ({marks})", jans)

    def pct(n: int) -> str:
        return f"{n}/{len(jans)} ({n / len(jans) * 100:.0f}%)"

    print(f"重量   jdl.v_goods_dimensions        : {pct(len(weights))}"
          + (f"  ← 取得失敗: {err_w}" if err_w else ""))
    print(f"英語名 compliance.shopify_item       : {pct(len(titles))}"
          + (f"  ← 取得失敗: {err_t}" if err_t else ""))
    print(f"品牌   nst.item_master_raw.maker     : {pct(len(makers))}"
          + (f"  ← 取得失敗: {err_m}" if err_m else ""))

    # いま手で入れている値と比べる（マスタがある場合だけ）
    if skus:
        both = [(s, v) for s, v in skus.items()
                if v.get("weight_kg") and str(v.get("jan") or "") in weights]
        if both:
            close = 0
            for s, v in both:
                pack = int(v.get("pack") or 1)
                got = float(weights[str(v["jan"])]) * pack / 1000
                if abs(got - float(v["weight_kg"])) < 0.02:
                    close += 1
            print(f"\n重量の突き合わせ（JDL × 入数 ÷ 1000 と手入力の差 < 20g）: "
                  f"{close}/{len(both)}")
        miss_name = [s for s, v in skus.items() if not v.get("name_en")]
        print(f"英語品名が空の SKU: {len(miss_name)}/{len(skus)}")

    print("\n※ Product ID / 옵션 ID は Coupang の受注 API がそのまま返すので、"
          "API 接続後は取り込み不要になる")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
