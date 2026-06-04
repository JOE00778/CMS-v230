"""shared/purchase_engine.compute_recommendations の最小スモークテスト。

データ源は PG 移行後の nst.* スキーマ（sales_monthly / item_master_raw /
inventory_snapshot / inventory_activity_monthly）。SQLite では tests/nstdb.py の
ATTACH shim で同名前空間を再現する。発注ロジック自体は不変なので、各 assert は
移行前と同一（変えたのは入力テーブルの組み立てだけ）。
"""
import math
import sqlite3

import pytest

from shared.purchase_engine import compute_recommendations
from tests.nstdb import new_conn, iid, seed_item, seed_sales, seed_inventory, seed_turnover

# 直近 3 ヶ月（sorted で時系列になる文字列）
_YM = ["2026-02", "2026-03", "2026-04"]

# supplier_quote は schema 無し（裸表）。列順は業務コードの SELECT に一致。
_SUPPLIER_DDL = """
CREATE TABLE supplier_quote (supplier_name TEXT, jan TEXT, display_name TEXT, unit_price INTEGER,
    lot_size INTEGER, case_qty INTEGER, min_order_amount INTEGER, order_condition TEXT,
    lead_time_text TEXT, zone TEXT, zone_rank INTEGER, nst_supplier_code TEXT);
"""


def _conn():
    c = new_conn()
    c.executescript(_SUPPLIER_DDL)
    return c


def _seed_sales(c, jan, vals):
    for i, v in enumerate(vals):
        seed_sales(c, jan, _YM[i], v)


def _set_rank(c, jan, rank):
    c.execute("UPDATE nst.item_master_raw SET item_rank=? WHERE jan=?", (rank, jan))


def test_inventory_deduction_and_lot_rounding():
    """Boss 2026-05-14 公式: 推奨月販×2.5 − 実質在庫 → ケース切り上げ。"""
    c = _conn()
    # 月販 100/100/100 → rec_monthly = 100×1.0 = 100, 目標 = 100×1.5 = 150, 必要 = 100×2.5 − 在庫
    _seed_sales(c, "4900000000001", [100, 100, 100])
    seed_item(c, "4900000000001", display_name="商品A", maker="メーカーX",
              item_rank="Aランク", handling_cd="取扱中")
    # case_qty=NULL → lot_size=50 にフォールバック → pack=50
    c.execute("INSERT INTO supplier_quote VALUES ('仕入先甲','4900000000001','商品A',100,50,NULL,0,'掛','2週間','JD_DIRECT',1,'NST1')")
    # 手持30 + 注文済20 = 実質在庫50 (確保済は引かない、輸送中無視)
    seed_inventory(c, "4900000000001", qty_on_hand=30, committed=0, qty_on_order=20)

    df = compute_recommendations(c, months=3)
    assert len(df) == 1
    r = df.iloc[0]
    assert r["rec_monthly"] == pytest.approx(100.0)
    assert r["target_stock"] == pytest.approx(150.0)    # 100 × 1.5
    assert r["eff_stock"] == pytest.approx(50.0)         # JD 30 + 注文済 20
    assert r["shortfall"] == pytest.approx(200.0)        # 100×2.5 − 50
    assert r["suggested_qty"] == 200                     # ceil(200/50)*50
    assert r["line_cost"] == 200 * 100
    assert df.attrs["inventory_loaded"] is True


def test_skip_when_enough_stock():
    """在庫充足 (実質在庫 ≥ 推奨月販×2.5) → 必要数 ≤0 → スキップ。"""
    c = _conn()
    _seed_sales(c, "4900000000002", [10, 10, 10])
    seed_item(c, "4900000000002", display_name="商品B", maker="メーカーX",
              item_rank=None, handling_cd="取扱中")
    c.execute("INSERT INTO supplier_quote VALUES ('仕入先甲','4900000000002','商品B',100,1,NULL,0,'掛','1週間','JD_DIRECT',1,'NST1')")
    seed_inventory(c, "4900000000002", qty_on_hand=9999)  # JD手持 9999
    df = compute_recommendations(c, months=3)
    assert df.empty


def test_discontinued_excluded_unless_opted_in():
    c = _conn()
    _seed_sales(c, "4900000000003", [50, 50, 50])
    seed_item(c, "4900000000003", display_name="商品C", maker="メーカーX",
              item_rank="取扱中止", handling_cd="メーカー取扱中止")
    c.execute("INSERT INTO supplier_quote VALUES ('仕入先甲','4900000000003','商品C',200,10,NULL,0,'掛','2週間','JD_DIRECT',1,'NST1')")
    seed_inventory(c, "4900000000003", qty_on_hand=0)
    df = compute_recommendations(c, months=3, safety_months=1.0)
    assert df.empty
    assert df.attrs["n_discontinued_excluded"] == 1
    df2 = compute_recommendations(c, months=3, safety_months=1.0, include_discontinued=True)
    assert len(df2) == 1


def test_zone_priority_then_cheapest():
    c = _conn()
    _seed_sales(c, "4900000000004", [40, 40, 40])
    seed_item(c, "4900000000004", display_name="商品D", maker="メーカーX",
              item_rank="Bランク", handling_cd="取扱中")
    # 弁天が単価安いが zone_rank 後 → JD直送(zone_rank1)が選ばれる（弁天 markup 撤廃でも zone 優先は維持）
    c.execute("INSERT INTO supplier_quote VALUES ('JD系A','4900000000004','商品D',120,1,NULL,0,'掛','2週間','JD_DIRECT',1,'NSTJD')")
    c.execute("INSERT INTO supplier_quote VALUES ('弁天系B','4900000000004','商品D',100,1,NULL,0,'掛','2週間','BENTEN_TRANSIT',2,'NSTBT')")
    seed_inventory(c, "4900000000004", qty_on_hand=0)
    df = compute_recommendations(c, months=3, safety_months=1.0)
    assert len(df) == 1
    assert df.iloc[0]["supplier_name"] == "JD系A"
    assert df.iloc[0]["zone"] == "JD_DIRECT"
    # 弁天が選ばれた場合の検証: JD候補を消す
    c.execute("DELETE FROM supplier_quote WHERE supplier_name='JD系A'")
    df2 = compute_recommendations(c, months=3, safety_months=1.0)
    assert df2.iloc[0]["supplier_name"] == "弁天系B"
    assert df2.iloc[0]["effective_price"] == 100   # markup 1.00 → 単価そのまま


def test_runs_without_inventory_table():
    # 在庫テーブルは在るが行ゼロ → inv_map 空 → inventory_loaded False（旧「表無し」と同値）
    c = _conn()
    _seed_sales(c, "4900000000005", [30, 30, 30])
    seed_item(c, "4900000000005", display_name="商品E", maker="メーカーX",
              item_rank="Cランク", handling_cd="取扱中")
    c.execute("INSERT INTO supplier_quote VALUES ('仕入先甲','4900000000005','商品E',50,1,NULL,0,'掛','2週間','JD_DIRECT',1,'NST1')")
    df = compute_recommendations(c, months=3)
    assert len(df) == 1
    assert df.attrs["inventory_loaded"] is False
    assert df.iloc[0]["eff_stock"] == 0.0           # 在庫行無し → 0 扱い
    assert df.iloc[0]["suggested_qty"] == math.ceil(30 * 1.0 * 2.5)   # rec_monthly × 2.5


# ---- 品牌集約 (Boss 2026-05-12) ----


def _add_sku(c, jan, maker, sales=(20, 20, 20)):
    seed_item(c, jan, display_name=f"商品{jan[-2:]}", maker=maker,
              item_rank="Bランク", handling_cd="取扱中")
    for i, v in enumerate(sales):
        seed_sales(c, jan, _YM[i], v)


def _q(c, sup, jan, price, zone="JD_DIRECT", zr=1, lot=1):
    c.execute("INSERT INTO supplier_quote VALUES (?,?,?,?,?,NULL,0,'掛','2週間',?,?,?)",
              (sup, jan, "x", price, lot, zone, zr, "NST"))


def test_brand_consolidation_concentrates_to_max_coverage_supplier():
    c = _conn()
    jans = [f"49000000000{i:02d}" for i in range(8)]
    for j in jans:
        _add_sku(c, j, "ブランドX")
    # 甲: 全 8 SKU を報価 (やや高め 110)。乙: 0-2 (安 100)。丙: 3-5 (安 100)。丁: 6-7 (安 100)。
    for j in jans:
        _q(c, "甲", j, 110)
    for j in jans[0:3]:
        _q(c, "乙", j, 100)
    for j in jans[3:6]:
        _q(c, "丙", j, 100)
    for j in jans[6:8]:
        _q(c, "丁", j, 100)
    # 集約なし: 0-2→乙, 3-5→丙, 6-7→丁 = 3 仕入先（甲は最安でないので選ばれない）
    df0 = compute_recommendations(c, months=3, safety_months=1.0, use_inventory=False, consolidate_by_brand=False, small_brand_skip=4)
    assert df0["supplier_name"].nunique() == 3
    # 集約あり: 甲が全 8 をカバー → 甲 1 社に集約 (110 ≤ 100×1.5 なので移動 OK)
    df = compute_recommendations(c, months=3, safety_months=1.0, use_inventory=False, consolidate_by_brand=True, small_brand_skip=4)
    assert df["supplier_name"].nunique() == 1
    assert set(df["supplier_name"]) == {"甲"}
    assert df["consolidated"].all()
    assert df.attrs["n_consolidated"] == 8


def test_consolidation_never_worsens_zone():
    c = _conn()
    jans = [f"49000000010{i:02d}" for i in range(8)]
    for j in jans:
        _add_sku(c, j, "ブランドY")
    # JD直送「甲」が 0-6 を報価。SKU 7 は JD では「乙」のみ、弁天「丙」も報価。
    for j in jans[0:7]:
        _q(c, "甲", j, 100)
    _q(c, "乙", jans[7], 100, zone="JD_DIRECT", zr=1)
    # 弁天「丙」が 全 8 を報価 (安) → カバー数だけ見ると 丙 が最強。だが zone tier=1 を先に処理するので
    # SKU 0-7 は JD アンカー(甲)に行く。SKU 7 は 甲 が報価無し → 乙(JD) のまま。丙(弁天) には行かない。
    for j in jans:
        _q(c, "丙", j, 80, zone="BENTEN_TRANSIT", zr=2)
    df = compute_recommendations(c, months=3, safety_months=1.0, use_inventory=False, consolidate_by_brand=True, small_brand_skip=4)
    # 全 SKU が JD直送 (丙=弁天 には 1 件も行っていない)
    assert (df["zone"] == "JD_DIRECT").all()
    assert "丙" not in set(df["supplier_name"])
    # SKU 7 は 乙 のまま
    assert df.loc[df["jan"] == jans[7], "supplier_name"].iloc[0] == "乙"


def test_small_brand_not_consolidated():
    c = _conn()
    jans = [f"49000000020{i:02d}" for i in range(4)]   # 4 SKU ≤ small_brand_skip(5)
    for j in jans:
        _add_sku(c, j, "ミニ品牌")
    _q(c, "甲", jans[0], 100); _q(c, "甲", jans[1], 100); _q(c, "甲", jans[2], 100); _q(c, "甲", jans[3], 100)
    _q(c, "乙", jans[0], 90); _q(c, "乙", jans[1], 90)   # 0,1 は 乙 が安い
    df = compute_recommendations(c, months=3, safety_months=1.0, use_inventory=False, consolidate_by_brand=True, small_brand_skip=5)
    # 小品牌なので集約しない → 0,1→乙 / 2,3→甲 の 2 社のまま
    assert df["supplier_name"].nunique() == 2
    assert df.attrs["n_consolidated"] == 0


def test_consolidation_price_guard():
    c = _conn()
    jans = [f"49000000030{i:02d}" for i in range(8)]
    for j in jans:
        _add_sku(c, j, "ブランドZ")
    # 甲: 全 8 を報価だが激高 (1000)。乙: 0-3 (安 100)。丙: 4-7 (安 100)。
    for j in jans:
        _q(c, "甲", j, 1000)
    for j in jans[0:4]:
        _q(c, "乙", j, 100)
    for j in jans[4:8]:
        _q(c, "丙", j, 100)
    df = compute_recommendations(c, months=3, safety_months=1.0, use_inventory=False, consolidate_by_brand=True, small_brand_skip=4)
    # 甲は 1000 > 100×1.5 なので誰も移らない → 乙(4) + 丙(4) の 2 社のまま, 甲 は使われない
    assert "甲" not in set(df["supplier_name"])
    assert df["supplier_name"].nunique() == 2


# ---- optimize モード (Boss 2026-05-12: 最小支出シナリオ) ----


def test_optimize_line_cost_picks_lowest_total_not_lowest_unit_price():
    """Boss 2026-05-14 公式: 必要 = 25×2.5 = 62.5。甲 lot100 = 100個=¥10,000; 乙 lot10 = 70個×¥120 = ¥8,400。
    'line_cost' は発注金額最安 → 乙。'cost' は単価最安 → 甲。"""
    c = _conn()
    _add_sku(c, "4900000040001", "ブランドQ", sales=(25, 25, 25))   # rec_monthly = 25
    _q(c, "甲", "4900000040001", 100, lot=100)   # 100*100 = 10,000
    _q(c, "乙", "4900000040001", 120, lot=10)    # ceil(62.5/10)*10*120 = 70*120 = 8,400
    df_cost = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False, optimize="cost")
    df_lc = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False, optimize="line_cost")
    assert df_cost.iloc[0]["supplier_name"] == "甲"
    assert df_cost.iloc[0]["line_cost"] == 100 * 100
    assert df_lc.iloc[0]["supplier_name"] == "乙"
    assert df_lc.iloc[0]["line_cost"] == 70 * 120
    assert df_lc.attrs["optimize"] == "line_cost"


# ---- ランクフィルタ / 在庫月数上限 (Boss 2026-05-12) ----


def test_rank_filter():
    c = _conn()
    _add_sku(c, "4900000050001", "M", sales=(20, 20, 20))
    _set_rank(c, "4900000050001", "Aランク")
    _add_sku(c, "4900000050002", "M", sales=(20, 20, 20))
    _set_rank(c, "4900000050002", "Cランク")
    _q(c, "甲", "4900000050001", 100); _q(c, "甲", "4900000050002", 100)
    df = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False, ranks=("Aランク", "Bランク"))
    assert len(df) == 1
    assert df.iloc[0]["jan"] == "4900000050001"
    assert df.attrs["n_rank_excluded"] == 1
    df_all = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False)
    assert len(df_all) == 2


def test_max_stock_months_defers_overstock():
    """Boss 2026-05-14 公式: 月販 1, 必要=2.5, lot=100 → qty=100, 発注後在庫=100ヶ月分。
    無 cap: status='needs_review' (箱規/月販=100≥2.5)。cap=4: 'deferred_overstock' が優先。"""
    c = _conn()
    _add_sku(c, "4900000060001", "M", sales=(1, 1, 1))
    _q(c, "甲", "4900000060001", 50, lot=100)
    df_nocap = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False)
    # 箱規 100 / 推奨月販 1 = 100 ≥ 2.5 → needs_review
    assert df_nocap.iloc[0]["status"] == "needs_review"
    assert df_nocap.iloc[0]["stock_months_after"] == 100.0
    df_cap = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False, max_stock_months=4.0)
    assert df_cap.iloc[0]["status"] == "deferred_overstock"
    assert df_cap.iloc[0]["overstock"] is True or df_cap.iloc[0]["overstock"] == 1
    assert df_cap.attrs["n_overstock"] == 1
    # 推奨月販 100, lot 100 → 必要 250, ceil(250/100)*100 = 300, 在庫 300/100 = 3ヶ月 → 上限内
    _add_sku(c, "4900000060002", "M", sales=(100, 100, 100))
    _q(c, "甲", "4900000060002", 50, lot=100)
    df2 = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False, max_stock_months=4.0)
    rec = df2[df2["jan"] == "4900000060002"].iloc[0]
    assert rec["status"] == "recommended"
    assert rec["stock_months_after"] == 3.0    # 300/100 = 3


def test_no_lot_suppliers_ignore_lot():
    """Boss 2026-05-14 公式: 月販 10, 必要 = 10×2.5 = 25。
    甲(lot 100) → 100 個 (rounded up)。ハリマ(NO_LOT) → 25 個 (ぴったり)。"""
    c = _conn()
    _add_sku(c, "4900000070001", "M", sales=(10, 10, 10))
    _q(c, "甲", "4900000070001", 100, lot=100)
    _q(c, "ハリマ", "4900000070001", 120, zone="EMERGENCY", zr=3, lot=100)
    df_jd = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False)
    assert df_jd.iloc[0]["supplier_name"] == "甲"   # zone優先
    assert df_jd.iloc[0]["suggested_qty"] == 100    # ceil(25/100)*100
    # 甲を消して ハリマ のみ → ロット無視で 25 個
    c.execute("DELETE FROM supplier_quote WHERE supplier_name='甲'")
    df_h = compute_recommendations(c, use_inventory=False, consolidate_by_brand=False)
    assert df_h.iloc[0]["supplier_name"] == "ハリマ"
    assert df_h.iloc[0]["suggested_qty"] == 25      # NO_LOT: pack=1 → ceil(25/1)*1 = 25
    assert df_h.iloc[0]["pack_size"] == 1
    assert df_h.iloc[0]["line_cost"] == 25 * 120


# ---- 完売率系数取代趋势系数 (Boss 2026-05-26: 订货依据 × 发注AI 结合) ----


def test_sellthrough_factor_replaces_trend():
    """月完売率 ≥0.9(断货) → 系数 1.5 → 推奨月販 = base × 1.5（取代趋势系数）。"""
    c = _conn()
    _seed_sales(c, "4900000080001", [100, 100, 100])   # base_monthly = 100
    seed_item(c, "4900000080001", display_name="商品H", maker="M",
              item_rank="Aランク", handling_cd="取扱中", item_code="IC-1")
    c.execute("INSERT INTO supplier_quote VALUES ('甲','4900000080001','商品H',100,1,NULL,0,'掛','2週間','JD_DIRECT',1,'NST1')")
    seed_turnover(c, "4900000080001", "202604", opening_qty=5, received_qty=95, sold_qty=95)  # 完売率 95/100 = 0.95
    df = compute_recommendations(c, months=3, use_inventory=False, consolidate_by_brand=False)
    r = df.iloc[0]
    assert r["sellthrough"] == pytest.approx(0.95)
    assert r["sellthrough_factor"] == pytest.approx(1.5)
    assert r["rec_monthly"] == pytest.approx(150.0)              # 100 × 1.5
    assert r["suggested_qty"] == math.ceil(150 * 2.5)            # rec × 2.5 = 375


def test_sellthrough_low_shrinks_order():
    """月完売率 <0.5(压库存) → 系数 0.5 → 推奨月販减半。"""
    c = _conn()
    _seed_sales(c, "4900000080002", [100, 100, 100])
    seed_item(c, "4900000080002", display_name="商品I", maker="M",
              item_rank="Aランク", handling_cd="取扱中", item_code="IC-2")
    c.execute("INSERT INTO supplier_quote VALUES ('甲','4900000080002','商品I',100,1,NULL,0,'掛','2週間','JD_DIRECT',1,'NST1')")
    seed_turnover(c, "4900000080002", "202604", opening_qty=70, received_qty=30, sold_qty=30)  # 完売率 30/100 = 0.30
    df = compute_recommendations(c, months=3, use_inventory=False, consolidate_by_brand=False)
    r = df.iloc[0]
    assert r["sellthrough_factor"] == pytest.approx(0.5)
    assert r["rec_monthly"] == pytest.approx(50.0)               # 100 × 0.5
