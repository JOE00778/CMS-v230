"""等级判定规则 — 核心逻辑 (Boss 重构 v3: 4 档)

等级値は NST item_master_raw.item_rank の合法値に統一（回写 NST のため・中日 UI 共通）:
    Aランク / Bランク / Cランク / NEW / 取扱中止
"""
from typing import Literal, Dict

Rank = Literal['Aランク', 'Bランク', 'Cランク', '取扱中止']


def classify_rank(sku_data: dict) -> Rank:
    """
    4 档判定规则 (Boss 重构 v3)

    优先级 (按顺序判断,匹配即返回):
        1. NetSuite 取扱中止 → '取扱中止'
        2. 3 个月无动销     → '取扱中止'  (跟取扱中止合并)
        3. top 80% + ≥59% 高利 → 'Aランク'
        4. top 80%           → 'Bランク'
        5. 其他              → 'Cランク'

    Args:
        sku_data: 含以下字段
            - netsuite_status: str (取扱区分: '取扱中' / '取扱中止' / 'メーカー取扱中止')
            - sales_amount_rank_pct: float (销售额累计排名百分位,0-1)
            - gross_margin_rate: float (粗利率,0-1)
            - no_sales_3m: bool (最近 3 个月窗口内总销量 = 0)

    Returns: 'Aランク' / 'Bランク' / 'Cランク' / '取扱中止' (NST item_rank 合法値)

    注: acknowledged_action (改廃确认路径) 已删除 (Boss 决定: 不需要重复路径,
        NetSuite 取扱中止 单一权威源即可)
    """
    # 1. NetSuite 取扱中止 → 取扱中止 (最高优先, 系统级状态)
    if sku_data.get('netsuite_status') in ('取扱中止', 'メーカー取扱中止'):
        return '取扱中止'

    # 2. 3 个月无动销 → 取扱中止 (Boss: 长期不动销品也归取扱中止档)
    if sku_data.get('no_sales_3m'):
        return '取扱中止'

    # 3. top 80% + 高利 → Aランク
    is_top_80 = sku_data.get('sales_amount_rank_pct', 1.0) <= 0.80
    is_high_margin = sku_data.get('gross_margin_rate', 0) >= 0.59

    if is_top_80 and is_high_margin:
        return 'Aランク'
    if is_top_80:
        return 'Bランク'

    return 'Cランク'


def calc_sales_rank(sku_to_sales: Dict[str, float]) -> Dict[str, float]:
    """
    全 SKU 按销售额降序，计算 cumsum rank_pct。

    Args:
        sku_to_sales: {sku: total_sales_amount}

    Returns:
        {sku: rank_pct}，其中 rank_pct 表示该 SKU 销售额在全部中的累计百分位 (0-1)
        - rank_pct <= 0.80 => top 80%
    """
    if not sku_to_sales:
        return {}

    # 按销售额降序排列
    sorted_skus = sorted(sku_to_sales.items(), key=lambda x: x[1], reverse=True)
    total_sales = sum(v for _, v in sorted_skus)

    result = {}
    cumsum = 0.0
    for sku, sales in sorted_skus:
        cumsum += sales
        rank_pct = cumsum / total_sales if total_sales > 0 else 1.0
        result[sku] = rank_pct

    return result
