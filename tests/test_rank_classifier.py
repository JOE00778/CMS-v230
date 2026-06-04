"""等级判定模块的测试（T-016）。

覆盖 8+ 测试：
- 4 档边界：A / B / C / 停售（各 1）
- 销售前 80% 边界（0.79 / 0.80 / 0.81）
- 利润率边界（0.58 / 0.59 / 0.60）
- 停售优先（即使 top_80 + high_margin 也归停售）
- generate_proposal 跑通
"""
from __future__ import annotations

import pytest

import shared.db as shared_db
from modules.rank_classifier.rules import (
    classify_rank,
    calc_sales_rank,
    Rank,
)
from modules.rank_classifier.proposal import (
    generate_proposal,
    export_csv,
)
from tests.nstdb import new_conn, seed_item, seed_sales, seed_inventory


class TestClassifyRank:
    """classify_rank 核心规则测试"""

    def test_rank_a_top80_high_margin(self):
        """A 档：销售 top 80% + 粗利 >= 59%"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.5,  # <= 0.80
            'gross_margin_rate': 0.60,     # >= 0.59
        }
        assert classify_rank(sku_data) == 'Aランク'

    def test_rank_b_top80_low_margin(self):
        """B 档：销售 top 80% + 粗利 < 59%"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.50,
            'gross_margin_rate': 0.50,
        }
        assert classify_rank(sku_data) == 'Bランク'

    def test_rank_c_outside_top80(self):
        """C 档：销售不在 top 80%"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.90,  # > 0.80
            'gross_margin_rate': 0.70,      # 即使高利润也是 C
        }
        assert classify_rank(sku_data) == 'Cランク'

    def test_rank_discontinued_netsuite_status(self):
        """停售：NetSuite 取扱中止"""
        sku_data = {
            'netsuite_status': '取扱中止',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.10,  # 即使是 top 也停售
            'gross_margin_rate': 0.90,
        }
        assert classify_rank(sku_data) == '取扱中止'

    def test_rank_discontinued_maker_status(self):
        """停售：NetSuite メーカー取扱中止"""
        sku_data = {
            'netsuite_status': 'メーカー取扱中止',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.05,
            'gross_margin_rate': 0.95,
        }
        assert classify_rank(sku_data) == '取扱中止'

    def test_sales_rank_boundary_0_79(self):
        """销售 rank_pct 0.79 < 0.80 → top_80"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.79,
            'gross_margin_rate': 0.60,
        }
        assert classify_rank(sku_data) == 'Aランク'  # top 80 + high margin

    def test_sales_rank_boundary_0_80(self):
        """销売 rank_pct 0.80 <= 0.80 → top_80（边界包含）"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.80,
            'gross_margin_rate': 0.60,
        }
        assert classify_rank(sku_data) == 'Aランク'  # top 80 + high margin

    def test_sales_rank_boundary_0_81(self):
        """销売 rank_pct 0.81 > 0.80 → not top_80 → C"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.81,
            'gross_margin_rate': 0.60,
        }
        assert classify_rank(sku_data) == 'Cランク'  # not top 80 → C

    def test_margin_boundary_0_58(self):
        """粗利率 0.58 < 0.59 → low_margin → B"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.50,
            'gross_margin_rate': 0.58,
        }
        assert classify_rank(sku_data) == 'Bランク'  # top 80 + low margin

    def test_margin_boundary_0_59(self):
        """粗利率 0.59 >= 0.59 → high_margin → A"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.50,
            'gross_margin_rate': 0.59,
        }
        assert classify_rank(sku_data) == 'Aランク'  # top 80 + high margin

    def test_margin_boundary_0_60(self):
        """粗利率 0.60 >= 0.59 → high_margin → A"""
        sku_data = {
            'netsuite_status': '取扱中',
            'acknowledged_action': None,
            'sales_amount_rank_pct': 0.50,
            'gross_margin_rate': 0.60,
        }
        assert classify_rank(sku_data) == 'Aランク'

    def test_discontinued_priority(self):
        """停售优先：netsuite_status 优先于 acknowledged_action"""
        sku_data = {
            'netsuite_status': '取扱中止',
            'acknowledged_action': '取扱中止',
            'sales_amount_rank_pct': 0.10,
            'gross_margin_rate': 0.95,
        }
        assert classify_rank(sku_data) == '取扱中止'

    def test_missing_fields_defaults(self):
        """缺失字段默认值处理"""
        sku_data = {
            'netsuite_status': '取扱中',
            # 不提供 acknowledged_action → 默认 None
            # 不提供 sales_amount_rank_pct → 默认 1.0
            # 不提供 gross_margin_rate → 默认 0
        }
        assert classify_rank(sku_data) == 'Cランク'  # 不 top 80, 低利润


class TestCalcSalesRank:
    """calc_sales_rank 函数测试"""

    def test_empty_input(self):
        """空输入返回空字典"""
        result = calc_sales_rank({})
        assert result == {}

    def test_single_sku(self):
        """单个 SKU 的 rank_pct = 1.0"""
        result = calc_sales_rank({'SKU001': 1000.0})
        assert result == {'SKU001': 1.0}

    def test_multiple_skus_cumsum(self):
        """多个 SKU 按降序累计排名"""
        sku_to_sales = {
            'SKU001': 500.0,   # 50%
            'SKU002': 300.0,   # 50 + 30 = 80%
            'SKU003': 200.0,   # 50 + 30 + 20 = 100%
        }
        result = calc_sales_rank(sku_to_sales)
        assert result['SKU001'] == 0.5
        assert result['SKU002'] == 0.8
        assert result['SKU003'] == 1.0

    def test_rank_pct_order(self):
        """rank_pct 随销售额递增"""
        sku_to_sales = {
            'HIGH': 1000.0,
            'MED': 500.0,
            'LOW': 100.0,
        }
        result = calc_sales_rank(sku_to_sales)
        assert result['HIGH'] < result['MED'] < result['LOW']


class TestGenerateProposal:
    """generate_proposal 集成测试。

    generate_proposal は内部で shared.db.get_connection() を呼び、PG 移行後の
    nst.* schema 限定表（sales_monthly / item_master_raw / inventory_snapshot）を
    クエリする。SQLite では tests/nstdb.py の ATTACH shim で再現し、get_connection を
    monkeypatch で seed 済み接続に差し替える（渡した db_path は本番コードでは無視される）。
    """

    # (item_code, display_name, total_revenue, margin, netsuite_status)
    _DATA = [
        ('SKU001', '商品001', 1000.0, 0.65, '取扱中'),
        ('SKU002', '商品002', 800.0, 0.60, '取扱中'),
        ('SKU003', '商品003', 600.0, 0.55, '取扱中'),
        ('SKU004', '商品004', 400.0, 0.50, '取扱中'),
        ('SKU005', '商品005', 300.0, 0.65, '取扱中'),
        ('SKU006', '商品006', 200.0, 0.60, '取扱中'),
        ('SKU007', '商品007', 100.0, 0.40, '取扱中'),
        ('SKU008', '商品008', 50.0, 0.35, '取扱中'),
        ('SKU009', '商品009', 30.0, 0.50, '取扱中'),
        ('SKU010', '商品010', 20.0, 0.45, '取扱中'),
        ('SKU011', '商品011', 10.0, 0.55, '取扱中'),
        ('SKU012', '商品012', 5.0, 0.45, '取扱中止'),
    ]

    @pytest.fixture
    def patched_conn(self, monkeypatch):
        """get_connection を、毎回 seed 済み in-memory(nst ATTACH) 接続を返すよう差し替える。"""
        data = self._DATA

        def _make():
            c = new_conn()
            for code, name, rev, margin, status in data:
                seed_item(c, code, item_code=code, display_name=name,
                          item_rank="Bランク", handling_cd=status)
                seed_sales(c, code, "2026-04", 10, revenue=rev, gross_profit=margin * rev)
                seed_inventory(c, code, qty_on_hand=0)
            c.commit()
            return c

        monkeypatch.setattr(shared_db, "get_connection", _make)
        return _make

    def test_generate_proposal_returns_list(self, patched_conn):
        result = generate_proposal()
        assert isinstance(result, list)
        assert len(result) == 12

    def test_generate_proposal_fields(self, patched_conn):
        result = generate_proposal()
        required_fields = ['sku', 'name', 'old_rank', 'new_rank', 'sales', 'margin', 'rank_pct']
        for p in result:
            for field in required_fields:
                assert field in p, f"Missing field: {field}"

    def test_generate_proposal_rank_distribution(self, patched_conn):
        """新等级分布（classify_rank の戻り値ラベルで集計）。

        revenue 累計で top80% = SKU001-004（4 件）。うち margin≥0.59 → Aランク
        (SKU001/002)、それ以外 → Bランク(SKU003/004)。残り 7 件は top80 外 → Cランク。
        SKU012 は netsuite 取扱中止。
        """
        result = generate_proposal()
        from collections import Counter
        rank_counts = Counter(p['new_rank'] for p in result)

        assert rank_counts['Aランク'] >= 2
        assert rank_counts['Bランク'] >= 2
        assert rank_counts['Cランク'] >= 4
        assert rank_counts['取扱中止'] >= 1

    def test_export_csv(self, patched_conn, tmp_path):
        proposals = generate_proposal()
        csv_path = tmp_path / "rank_proposal.csv"

        export_csv(proposals, csv_path)
        assert csv_path.exists()

        with open(csv_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            assert len(lines) == 13  # 头 + 12 条数据
            assert 'item_code' in lines[0]
            assert 'new_rank' in lines[0]
