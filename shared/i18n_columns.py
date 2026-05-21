"""i18n column 标签 · ja / zh / en 三语 dataframe 列名翻译。

设计：
- DB 内部 column 保持英文（性能 + SQL 简洁）
- UI 显示 / 导出文件用对应语言列名
- page st.dataframe 渲染前调 localize_df(df) 自动 rename

使用：
    from shared.i18n_columns import localize_df
    df = pd.DataFrame(...)
    st.dataframe(localize_df(df), use_container_width=True)

切换语言：shared/i18n.lang_selector() 已经有 ja/zh/en，本模块自动跟随。
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from shared.i18n import get_lang


# ============================================================
# 翻译表 · 英文 column → {ja, zh, en}
# ============================================================
COLUMN_LABELS: dict[str, dict[str, str]] = {
    # ── ID / 商品基础（item_v2）──
    "jan":              {"ja": "JAN",          "zh": "JAN",       "en": "JAN"},
    "item_code":        {"ja": "アイテム",      "zh": "商品编号",   "en": "Item Code"},
    "internal_id":      {"ja": "内部ID",        "zh": "内部 ID",   "en": "Internal ID"},
    "upc":              {"ja": "UPCコード",     "zh": "UPC",       "en": "UPC"},
    "display_name":     {"ja": "表示名",        "zh": "商品名",     "en": "Display Name"},
    "maker":            {"ja": "メーカー",      "zh": "品牌",       "en": "Maker"},
    "rank":             {"ja": "ランク",        "zh": "等级",       "en": "Rank"},
    "handling_status":  {"ja": "取扱区分",      "zh": "经营状态",   "en": "Handling Status"},
    "department":       {"ja": "部門",          "zh": "部门",       "en": "Department"},

    # ── 进货 ──
    "std_cost":         {"ja": "定義原価",      "zh": "定义原价",   "en": "Std Cost"},
    "avg_cost":         {"ja": "平均原価",      "zh": "平均原价",   "en": "Avg Cost"},
    "actual_cost":      {"ja": "実績原価",      "zh": "实绩原价",   "en": "Actual Cost"},
    "min_cost":         {"ja": "最安原価",      "zh": "最低原价",   "en": "Min Cost"},
    "case_qty":         {"ja": "ケース入数",    "zh": "箱入数",     "en": "Case Qty"},
    "order_lot":        {"ja": "発注ロット",    "zh": "订货起订量", "en": "Order Lot"},
    "weight":           {"ja": "重量",          "zh": "重量",       "en": "Weight"},
    "supplier_default": {"ja": "仕入先",        "zh": "默认供应商", "en": "Supplier"},
    "supplier_name":    {"ja": "仕入先",        "zh": "供应商",     "en": "Supplier"},
    "supply_cycle_days":{"ja": "仕入サイクル",  "zh": "进货周期",   "en": "Supply Cycle"},
    "bucket":           {"ja": "サイクル区分",  "zh": "周期分类",   "en": "Bucket"},
    "cost_class":       {"ja": "原価ランク",    "zh": "原价等级",   "en": "Cost Class"},
    "unit_cost":        {"ja": "単価",          "zh": "单价",       "en": "Unit Cost"},
    "currency":         {"ja": "通貨",          "zh": "币种",       "en": "Currency"},

    # ── 库存 ──
    "on_hand_total":    {"ja": "手持合計",      "zh": "总在库",     "en": "On Hand"},
    "on_order_total":   {"ja": "注文済合計",    "zh": "在途总量",   "en": "On Order"},
    "qty_on_hand":      {"ja": "手持",          "zh": "在库",       "en": "Qty On Hand"},
    "qty_committed":    {"ja": "確保済",        "zh": "已占用",     "en": "Committed"},
    "qty_committed_total": {"ja": "確保済合計", "zh": "已占用合计", "en": "Committed Total"},
    "qty_backorder":    {"ja": "バック・オーダー", "zh": "缺货中",   "en": "Backorder"},
    "total_amount":     {"ja": "在庫金額",      "zh": "库存金额",   "en": "Stock Value"},
    "location":         {"ja": "場所",          "zh": "仓库",       "en": "Location"},
    "bin_number":       {"ja": "保管棚",        "zh": "货架号",     "en": "Bin"},
    "snapshot_at":      {"ja": "棚卸日",        "zh": "快照时间",   "en": "Snapshot At"},
    "status":           {"ja": "ステータス",    "zh": "状态",       "en": "Status"},
    "owner":            {"ja": "担当者",        "zh": "负责人",     "en": "Owner"},

    # ── 销售（shop_sales / item_sales_history）──
    "shop_id":          {"ja": "店舗ID",        "zh": "店铺 ID",   "en": "Shop ID"},
    "store":            {"ja": "店舗",          "zh": "店铺",       "en": "Store"},
    "fb_store":         {"ja": "FB店舗",        "zh": "FB 店铺",    "en": "FB Store"},
    "market_id":        {"ja": "市場",          "zh": "市场",       "en": "Market"},
    "platform":         {"ja": "プラットフォーム", "zh": "平台",     "en": "Platform"},
    "channel":          {"ja": "チャネル",      "zh": "渠道",       "en": "Channel"},
    "granularity":      {"ja": "粒度",          "zh": "粒度",       "en": "Granularity"},
    "period_start":     {"ja": "期間開始",      "zh": "开始日期",   "en": "Period Start"},
    "period_end":       {"ja": "期間終了",      "zh": "结束日期",   "en": "Period End"},
    "year_month":       {"ja": "年月",          "zh": "年月",       "en": "Year-Month"},
    "qty_sold":         {"ja": "販売数量",      "zh": "销量",       "en": "Qty Sold"},
    "unit_price":       {"ja": "単価",          "zh": "单价",       "en": "Unit Price"},
    "revenue":          {"ja": "売上",          "zh": "收入",       "en": "Revenue"},
    "revenue_jpy":      {"ja": "売上(JPY)",     "zh": "收入(JPY)",  "en": "Revenue JPY"},
    "cost":             {"ja": "原価",          "zh": "成本",       "en": "Cost"},
    "gross_profit":     {"ja": "粗利",          "zh": "毛利",       "en": "Gross Profit"},
    "gross_margin":     {"ja": "粗利率",        "zh": "毛利率",     "en": "Gross Margin"},
    "rank_at_sale":     {"ja": "販売時ランク",  "zh": "销售时等级", "en": "Rank At Sale"},

    # ── shop_monthly KPI ──
    "gmv":              {"ja": "GMV",          "zh": "GMV",        "en": "GMV"},
    "profit":           {"ja": "利益",          "zh": "利润",       "en": "Profit"},
    "margin_rate":      {"ja": "利益率",        "zh": "利润率",     "en": "Margin Rate"},
    "profit_contrib":   {"ja": "利益貢献率",    "zh": "利润贡献率", "en": "Profit Contrib"},
    "deduction_total":  {"ja": "控除合計",      "zh": "扣减合计",   "en": "Deductions"},
    "order_count":      {"ja": "注文数",        "zh": "订单数",     "en": "Orders"},
    "store_rating":     {"ja": "店舗評価",      "zh": "店铺评分",   "en": "Rating"},
    "online_products":  {"ja": "出品数",        "zh": "在线商品",   "en": "Online SKUs"},

    # ── 通用元数据 ──
    "source":           {"ja": "ソース",        "zh": "来源",       "en": "Source"},
    "source_file":      {"ja": "元ファイル",    "zh": "源文件",     "en": "Source File"},
    "source_priority":  {"ja": "優先度",        "zh": "优先级",     "en": "Priority"},
    "imported_at":      {"ja": "登録日時",      "zh": "导入时间",   "en": "Imported At"},
    "updated_at":       {"ja": "更新日時",      "zh": "更新时间",   "en": "Updated At"},
    "created_at":       {"ja": "作成日時",      "zh": "创建时间",   "en": "Created At"},
    "completed_at":     {"ja": "完了日時",      "zh": "完成时间",   "en": "Completed At"},
    "triggered_at":     {"ja": "実行日時",      "zh": "触发时间",   "en": "Triggered At"},
    "triggered_by":     {"ja": "実行者",        "zh": "触发者",     "en": "Triggered By"},
    "ran_at":           {"ja": "実行日時",      "zh": "运行时间",   "en": "Ran At"},
    "active":           {"ja": "有効",          "zh": "启用",       "en": "Active"},
    "display_name_2":   {"ja": "表示名",        "zh": "显示名",     "en": "Display Name"},

    # ── ETL / automation_runs / _v2_migration_runs ──
    "step":             {"ja": "ステップ",      "zh": "步骤",       "en": "Step"},
    "source_table":     {"ja": "ソーステーブル", "zh": "源表",      "en": "Source Table"},
    "rows_read":        {"ja": "読込件数",      "zh": "读入行数",   "en": "Rows Read"},
    "rows_written":     {"ja": "書込件数",      "zh": "写入行数",   "en": "Rows Written"},
    "errors":           {"ja": "エラー数",      "zh": "错误数",     "en": "Errors"},
    "notes":            {"ja": "備考",          "zh": "备注",       "en": "Notes"},
    "run_id":           {"ja": "実行ID",        "zh": "运行 ID",   "en": "Run ID"},
    "module":           {"ja": "モジュール",    "zh": "模块",       "en": "Module"},
    "payload":          {"ja": "ペイロード",    "zh": "参数",       "en": "Payload"},
    "summary":          {"ja": "結果",          "zh": "结果",       "en": "Summary"},

    # ── 改廃监控 ──
    "signal_type":      {"ja": "シグナル種別",  "zh": "信号类型",   "en": "Signal Type"},
    "detected_at":      {"ja": "検出日時",      "zh": "检测时间",   "en": "Detected At"},
    "acknowledged_by":  {"ja": "確認者",        "zh": "确认人",     "en": "Acked By"},
    "acknowledged_at":  {"ja": "確認日時",      "zh": "确认时间",   "en": "Acked At"},
    "action":           {"ja": "対応",          "zh": "处置",       "en": "Action"},

    # ── 月完売率 / 健康度 ──
    "qty_total_in":     {"ja": "入荷合計",      "zh": "入库合计",   "en": "Qty In"},
    "qty_total_out":    {"ja": "出荷合計",      "zh": "出库合计",   "en": "Qty Out"},
    "qty_received":     {"ja": "入荷数",        "zh": "入库数",     "en": "Qty Received"},
    "open_qty":         {"ja": "期初在庫",      "zh": "期初库存",   "en": "Open Qty"},
    "close_qty":        {"ja": "期末在庫",      "zh": "期末库存",   "en": "Close Qty"},
    "open_amount":      {"ja": "期初金額",      "zh": "期初金额",   "en": "Open Amount"},
    "close_amount":     {"ja": "期末金額",      "zh": "期末金额",   "en": "Close Amount"},
    "out_amount":       {"ja": "出荷金額",      "zh": "出库金额",   "en": "Out Amount"},
    "sell_through_rate":{"ja": "完売率",        "zh": "完売率",     "en": "Sell-Through"},
    "risk_label":       {"ja": "リスク区分",    "zh": "风险等级",   "en": "Risk Label"},

    # ── 在途库存 ──
    "qty_on_order":     {"ja": "発注中",        "zh": "订货中",     "en": "On Order"},
    "qty_waiting":      {"ja": "入荷待ち",      "zh": "等待入库",   "en": "Waiting"},
    "qty_in_transit":   {"ja": "輸送中",        "zh": "在途中",     "en": "In Transit"},

    # ── 时间戳补全 ──
    "last_received_at": {"ja": "最終入荷日",    "zh": "最近入库",   "en": "Last Received"},
    "last_sold_at":     {"ja": "最終販売日",    "zh": "最近销售",   "en": "Last Sold"},

    # ── Shopee 财务 (NST 拨款) ──
    "nst_payment":      {"ja": "NST 入金",      "zh": "NST 付款",   "en": "NST Payment"},
    "nst_refund":       {"ja": "NST 返金",      "zh": "NST 退款",   "en": "NST Refund"},
    "nst_bill":         {"ja": "NST 請求",      "zh": "NST 账单",   "en": "NST Bill"},
    "payout_amount":    {"ja": "拨款金額",      "zh": "拨款金额",   "en": "Payout Amount"},
    "payout_date":      {"ja": "拨款日",        "zh": "拨款日",     "en": "Payout Date"},
    "payout_month":     {"ja": "拨款月",        "zh": "拨款月",     "en": "Payout Month"},
    "payout_week":      {"ja": "拨款週",        "zh": "拨款周",     "en": "Payout Week"},
    "seller_account":   {"ja": "店舗アカウント", "zh": "店铺账号",   "en": "Seller Account"},
    "buyer_account":    {"ja": "購入者",        "zh": "买家账号",   "en": "Buyer Account"},
    "_jpy_rate":        {"ja": "JPY 換算レート", "zh": "JPY 换算率", "en": "JPY Rate"},
    "order_create_month":{"ja": "受注月",       "zh": "下单月",     "en": "Order Month"},
    "order_create_week":{"ja": "受注週",        "zh": "下单周",     "en": "Order Week"},
    "order_created_at": {"ja": "受注日時",      "zh": "下单时间",   "en": "Order Created"},
    "gross_price":      {"ja": "商品原価",      "zh": "商品原价",   "en": "Gross Price"},
    "product_discount": {"ja": "商品割引",      "zh": "商品折扣",   "en": "Discount"},
    "refund_amount":    {"ja": "返金額",        "zh": "退款金额",   "en": "Refund"},
    "commission":       {"ja": "手数料",        "zh": "佣金",       "en": "Commission"},
    "service_fee":      {"ja": "サービス料",    "zh": "服务费",     "en": "Service Fee"},
    "transaction_fee":  {"ja": "取引手数料",    "zh": "交易费",     "en": "Transaction Fee"},
    "buyer_shipping":   {"ja": "購入者送料",    "zh": "买家运费",   "en": "Buyer Shipping"},
    "seller_shipping":  {"ja": "出品者送料",    "zh": "卖家运费",   "en": "Seller Shipping"},

    # ── Shopee 订单 / 订单导出 ──
    "order_no":         {"ja": "注文番号",      "zh": "订单号",     "en": "Order No"},
    "shop_name":        {"ja": "店舗名",        "zh": "店铺名",     "en": "Shop Name"},
    "country":          {"ja": "国",            "zh": "国家",       "en": "Country"},
    "market":           {"ja": "市場",          "zh": "市场",       "en": "Market"},
    "local_sku":        {"ja": "現地SKU",       "zh": "本地 SKU",   "en": "Local SKU"},
    "payment_amount":   {"ja": "決済金額",      "zh": "支付金额",   "en": "Payment Amount"},
    "ship_qty":         {"ja": "出荷数",        "zh": "发货数量",   "en": "Ship Qty"},

    # ── 等级历史 / 趋势 ──
    "sku":              {"ja": "SKU",          "zh": "SKU",        "en": "SKU"},
    "quarter":          {"ja": "四半期",        "zh": "季度",       "en": "Quarter"},
    "old_rank":         {"ja": "旧ランク",      "zh": "旧等级",     "en": "Old Rank"},
    "new_rank":         {"ja": "新ランク",      "zh": "新等级",     "en": "New Rank"},
    "old_score":        {"ja": "旧スコア",      "zh": "旧分数",     "en": "Old Score"},
    "new_score":        {"ja": "新スコア",      "zh": "新分数",     "en": "New Score"},
    "changed_by":       {"ja": "変更者",        "zh": "变更人",     "en": "Changed By"},
    "changed_at":       {"ja": "変更日時",      "zh": "变更时间",   "en": "Changed At"},

    # ── page07 等级判定 proposal 列 ──
    "name":             {"ja": "商品名",        "zh": "商品名",     "en": "Name"},
    "trend":            {"ja": "推移",          "zh": "趋势",       "en": "Trend"},
    "advice":           {"ja": "アドバイス",    "zh": "运营建议",   "en": "Advice"},
    "sales":            {"ja": "売上",          "zh": "销售额",     "en": "Sales"},
    "margin":           {"ja": "粗利率",        "zh": "毛利率",     "en": "Margin"},
    "rank_pct":         {"ja": "累計順位",      "zh": "累计排名",   "en": "Rank %"},

    # ── 改廃 / discontinue ──
    "month":            {"ja": "月",            "zh": "月份",       "en": "Month"},
    "reason":           {"ja": "理由",          "zh": "理由",       "en": "Reason"},
    "note":             {"ja": "備考",          "zh": "备注",       "en": "Note"},
    "item_id":          {"ja": "商品ID",        "zh": "商品 ID",   "en": "Item ID"},
    "item_key":         {"ja": "商品キー",      "zh": "商品键",     "en": "Item Key"},
    "action_at":        {"ja": "操作日時",      "zh": "操作时间",   "en": "Action At"},

    # ── 入荷困难/进货历史 ──
    "supplier":         {"ja": "仕入先",        "zh": "供应商",     "en": "Supplier"},
    "qty":              {"ja": "数量",          "zh": "数量",       "en": "Qty"},
    "ordered_at":       {"ja": "発注日",        "zh": "下单日期",   "en": "Ordered At"},
    "quantity":         {"ja": "数量",          "zh": "数量",       "en": "Quantity"},
    "order_date":       {"ja": "注文日",        "zh": "订单日期",   "en": "Order Date"},
    "order_id":         {"ja": "注文ID",        "zh": "订单 ID",    "en": "Order ID"},
    "memo":             {"ja": "メモ",          "zh": "备注",       "en": "Memo"},
}


def label(col: str, lang: str | None = None) -> str:
    """单列翻译。未登记的 col 直接返回原名。"""
    if lang is None:
        try:
            lang = get_lang()
        except Exception:
            lang = "ja"
    if lang not in ("ja", "zh", "en"):
        lang = "ja"
    return COLUMN_LABELS.get(col, {}).get(lang, col)


def localize_df(df: Any, lang: str | None = None):
    """DataFrame column rename 到当前语言（保持原 df 不变）。

    用法：st.dataframe(localize_df(df))

    None 安全：传 None / 空 df 直接返回。
    """
    if df is None:
        return df
    try:
        if not hasattr(df, "rename"):
            return df
        if lang is None:
            try:
                lang = get_lang()
            except Exception:
                lang = "ja"
        if lang not in ("ja", "zh", "en"):
            lang = "ja"
        rename_map = {
            c: COLUMN_LABELS[c][lang]
            for c in df.columns
            if c in COLUMN_LABELS and lang in COLUMN_LABELS[c]
        }
        return df.rename(columns=rename_map) if rename_map else df
    except Exception:
        return df


def localize_records(rows: list[dict], lang: str | None = None) -> list[dict]:
    """list[dict] 列名翻译（pandas 不可用时）"""
    if not rows:
        return rows
    if lang is None:
        try:
            lang = get_lang()
        except Exception:
            lang = "ja"
    if lang not in ("ja", "zh", "en"):
        lang = "ja"
    out = []
    for r in rows:
        out.append({
            COLUMN_LABELS.get(k, {}).get(lang, k): v
            for k, v in r.items()
        })
    return out
