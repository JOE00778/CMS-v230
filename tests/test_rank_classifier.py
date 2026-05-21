"""等级判定模块的测试（T-016）。

覆盖 8+ 测试：
- 4 档边界：A / B / C / 停售（各 1）
- 销售前 80% 边界（0.79 / 0.80 / 0.81）
- 利润率边界（0.58 / 0.59 / 0.60）
- 停售优先（即使 top_80 + high_margin 也归停售）
- generate_proposal 跑通
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from modules.rank_classifier.rules import (
    classify_rank,
    calc_sales_rank,
    Rank,
)
from modules.rank_classifier.proposal import (
    generate_proposal,
    export_csv,
)


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
    """generate_proposal 集成测试"""

    @pytest.fixture
    def test_db(self):
        """创建临时测试数据库，包含 nst_store_sales 和 nst_inventory_snapshot"""
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")

            # 创建必要的表
            conn.executescript("""
                CREATE TABLE nst_store_sales (
                    id INTEGER PRIMARY KEY,
                    fb_store TEXT,
                    item_code TEXT NOT NULL,
                    upc TEXT,
                    handling_status TEXT,
                    display_name TEXT,
                    qty_sold REAL,
                    unit_price REAL,
                    revenue REAL,
                    defined_cost REAL,
                    gross_profit REAL,
                    gross_margin REAL,
                    rank TEXT,
                    ingested_at TIMESTAMP
                );

                CREATE TABLE nst_inventory_snapshot (
                    id INTEGER PRIMARY KEY,
                    internal_id TEXT,
                    item_code TEXT,
                    upc TEXT,
                    display_name TEXT,
                    status TEXT,
                    bin_number TEXT,
                    location TEXT,
                    handling_status TEXT,
                    qty_on_hand REAL,
                    qty_committed REAL,
                    qty_backorder REAL,
                    std_cost REAL,
                    total_amount REAL,
                    avg_cost REAL,
                    owner TEXT,
                    department TEXT,
                    ingested_at TIMESTAMP
                );

                CREATE TABLE item_master_netsuite (
                    internal_id TEXT PRIMARY KEY,
                    upc TEXT,
                    display_name TEXT,
                    avg_cost REAL,
                    std_cost REAL,
                    last_purchase REAL,
                    on_hand REAL,
                    available REAL,
                    on_order REAL,
                    department TEXT,
                    rank TEXT,
                    sku_id TEXT,
                    created_at TEXT,
                    maker TEXT,
                    source_file TEXT,
                    imported_at TEXT
                );
            """)

            # 插入测试数据：12 个 SKU，销售额分布为 top 80% 约 6-7 个
            test_data = [
                ('SKU001', '商品001', 1000.0, 0.65, '取扱中'),      # A档
                ('SKU002', '商品002', 800.0, 0.60, '取扱中'),       # A档
                ('SKU003', '商品003', 600.0, 0.55, '取扱中'),       # A档
                ('SKU004', '商品004', 400.0, 0.50, '取扱中'),       # B档
                ('SKU005', '商品005', 300.0, 0.65, '取扱中'),       # B档
                ('SKU006', '商品006', 200.0, 0.60, '取扱中'),       # B档
                ('SKU007', '商品007', 100.0, 0.40, '取扱中'),       # C档
                ('SKU008', '商品008', 50.0, 0.35, '取扱中'),        # C档
                ('SKU009', '商品009', 30.0, 0.50, '取扱中'),        # C档
                ('SKU010', '商品010', 20.0, 0.45, '取扱中'),        # C档
                ('SKU011', '商品011', 10.0, 0.55, '取扱中'),        # C档
                ('SKU012', '商品012', 5.0, 0.45, '取扱中止'),       # 停售
            ]

            for item_code, name, revenue, margin, status in test_data:
                conn.execute("""
                    INSERT INTO nst_store_sales
                    (fb_store, item_code, display_name, qty_sold, unit_price, revenue, gross_margin, handling_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, ('store01', item_code, name, 10, 100, revenue, margin, status))

                conn.execute("""
                    INSERT INTO nst_inventory_snapshot
                    (item_code, display_name, handling_status, location)
                    VALUES (?, ?, ?, ?)
                """, (item_code, name, status, 'MAIN'))

                # 插入 old rank（模拟现有的 item_master_netsuite）
                old_rank = 'A' if item_code in ['SKU001', 'SKU002'] else 'B' if item_code in ['SKU003', 'SKU004'] else 'C'
                conn.execute("""
                    INSERT INTO item_master_netsuite
                    (internal_id, upc, display_name, rank)
                    VALUES (?, ?, ?, ?)
                """, (f'id_{item_code}', item_code, name, old_rank))

            conn.commit()
            yield str(db_path)
            conn.close()

    def test_generate_proposal_returns_list(self, test_db):
        """generate_proposal 返回列表"""
        result = generate_proposal(db_path=test_db)
        assert isinstance(result, list)
        assert len(result) == 12

    def test_generate_proposal_fields(self, test_db):
        """生成的每条 proposal 包含必要字段"""
        result = generate_proposal(db_path=test_db)
        required_fields = ['sku', 'name', 'old_rank', 'new_rank', 'sales', 'margin', 'rank_pct']
        for p in result:
            for field in required_fields:
                assert field in p, f"Missing field: {field}"

    def test_generate_proposal_rank_distribution(self, test_db):
        """验证生成的 rank 分布合理"""
        result = generate_proposal(db_path=test_db)
        ranks = [p['new_rank'] for p in result]

        from collections import Counter
        rank_counts = Counter(ranks)

        # 预期分布：A ~3, B ~3, C ~5, 停售 ~1
        assert rank_counts['A'] >= 2
        assert rank_counts['B'] >= 2
        assert rank_counts['C'] >= 4
        assert rank_counts['停售'] >= 1

    def test_export_csv(self, test_db, tmp_path):
        """export_csv 生成 CSV 文件"""
        proposals = generate_proposal(db_path=test_db)
        csv_path = tmp_path / "rank_proposal.csv"

        export_csv(proposals, csv_path)
        assert csv_path.exists()

        # 验证 CSV 内容
        with open(csv_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            assert len(lines) == 13  # 头 + 12 条数据
            assert 'item_code' in lines[0]
            assert 'new_rank' in lines[0]
