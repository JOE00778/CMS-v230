# 数据库表完整参考 · 全表清单 + 字段 + 用途 + 读写者

> 状态：v1 · 2026-05-09 · Phase 3.6 完成后实测
> 数据源：本地 SQLite `data_warehouse/warehouse.db`（行数为实测值，Boss 的 Postgres 行数会更大）
> 总表数：**56**（含 v2 新增 11 张 + 旧表 45 张 + 元数据 / 月度产物）

---

## 📑 目录索引

| 分组 | 表数 | 角色 |
|---|---|---|
| 🟢 [v2 核心模型（主推使用）](#a-v2-核心模型) | 11 | item_v2 + 维度 A + 维度 B |
| 🟡 [旧商品主数据（保留并行）](#b-旧商品主数据) | 4 | item / item_master / item_master_netsuite / nst_item_summary |
| 🟡 [旧 NST 事实表（保留并行）](#c-旧-nst-事实表) | 4 | nst_inventory_snapshot / nst_turnover / nst_store_sales / store_monthly |
| 🟡 [销售统一事实表](#d-销售事实表) | 1 | sales_line |
| 🟡 [Shopee 财务领域](#e-shopee-财务) | 5 | shopee_orders / payouts / fees / adjustments / income_lines |
| 🟡 [进货 + 供应商](#f-进货供应商) | 8 | purchase* / supplier* / supply_cycle |
| 🟡 [其他业务表](#g-其他业务) | 7 | benten_stock / warehouse_stock / item_expiry / lot / inventory / inventory_snapshot / inventory_turnover |
| 🟣 [月度回写产物](#h-月度产物) | 6 | health_grade / cross_ratio / stock_sales / dead_inventory / rank_history / operation_advice |
| 🔵 [自动化 + 元数据](#i-自动化元数据) | 6 | automation_runs / _ingest_runs / _ingest_errors / _export_runs / _schema_version / _v2_migration_runs |
| 🔴 [警报 + 历史](#j-告警) | 3 | discontinue_alerts / difficult_items / difficult_items_history |
| ❌ [已废弃（自动 DROP）](#k-废弃) | 3 | sales / store_profit_lines / store_profit_daily_lines |

---

## A · v2 核心模型 ⭐

### `item_v2` — 商品主表（PK = JAN）

**实测行数**：8,056

| 字段 | 类型 | 含义 | 来源 |
|---|---|---|---|
| **`jan`** | TEXT PK | 13 位 JAN | item_master_netsuite.upc / item_master.jan |
| `item_code` | TEXT | NetSuite アイテム | item_master_netsuite / nst_item_summary |
| `internal_id` | TEXT | NetSuite 内部 ID | item_master_netsuite |
| `upc` | TEXT | 同 jan，兼容老字段 | — |
| `display_name` | TEXT | 表示名 | 4 表合并 |
| `maker` | TEXT | 品牌 ⭐ | item_master_netsuite / item_master |
| `rank` | TEXT | A/B/C/D 等级 | item_master_netsuite / item_master |
| `handling_status` | TEXT | 取扱区分 | nst_item_summary / item_master |
| `std_cost` | REAL | アイテム定義原価 | item_master_netsuite / nst_item_summary |
| `avg_cost` | REAL | 平均原価 | item_master_netsuite / nst_item_summary |
| `actual_cost` | REAL | 实绩原価 | item_master |
| `min_cost` | REAL | 最安原価 | item_master |
| `case_qty / order_lot / weight` | INTEGER/REAL | ケース入数 / 発注ロット / 重量 | item_master |
| `supplier_default` | TEXT | 默认供应商（cost_class='AB' 优先）| item_supplier_link 回填 |
| `supply_cycle_days / bucket` | INTEGER/TEXT | 进货周期 / short/normal/long | supply_cycle |
| `on_hand_total / on_order_total` | REAL | 全仓汇总库存 / 在途 | nst_inventory_snapshot SUM |
| `source_priority` | TEXT | nst > supplier > manual | — |
| `imported_at / updated_at` | TEXT | ETL 时间戳 | — |

**索引**：item_code / internal_id / maker / rank / handling_status

**读写者**：
- 写：`tools/migrate_to_v2.step_item_v2`（page 99 Tab 1 自动 / Tab 6 手工触发）
- 读：`shared/v2_browser`（admin 快查），`pages/03 定義原価編集` (maker / avg_cost fallback)

---

### `item_purchase_history` — 进货明细

**实测行数**：0（本地 purchase 表空）

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | INTEGER PK | auto |
| **`jan`** | TEXT | item_v2 关联 |
| `po_number` | TEXT | PO 号 |
| `supplier` | TEXT | 供应商名 |
| `qty / unit_cost / total_cost` | INTEGER/REAL | 数量 / 单价 / 总价 |
| `ordered_at / received_at` | TEXT | 下单 / 收货 |
| `source` | TEXT | 'netsuite_po' / 'predict' / 'history' |

**UNIQUE**：`(po_number, jan, source)`（同一 PO 在不同 source 可重复落）

**读写者**：写 step_item_purchase_history（合并 purchase + purchase_data + purchase_history）

---

### `item_sales_history` — 销售明细（jan × 期间 × 渠道）

**实测行数**：8,008

| 字段 | 含义 |
|---|---|
| `jan / period_start / period_end` | 时间范围 |
| `channel` | 'shopee_tw' / 'lazada_my' 等（platform_market 拼接）|
| `qty_sold / revenue / cost / gross_profit / gross_margin` | 销售四件套 |
| `source` | 数据来源（nst_store_sales / shopee_orders / asean_monthly 等）|

**UNIQUE**：`(jan, period_start, period_end, channel, source)`

**读写者**：写 step_item_sales_history（从 shop_sales 聚合 GROUP BY）

---

### `item_inventory_snapshot_v2` — 库存快照

**实测行数**：17,244（含 nst + benten + warehouse 三源合并）

| 字段 | 含义 |
|---|---|
| `jan / location / bin_number / snapshot_at` | 4 维 PK |
| `qty_on_hand / qty_committed / qty_backorder` | 库存三件套 |
| `std_cost / avg_cost` | 原価 |

**`location` 取值**：
- 来自 nst_inventory_snapshot.location（如 `JD-物流-千葉`）
- `'benten'`（弁天仓库）
- `'warehouse'`（外部仓库）

**读写者**：
- 写：step_item_inventory + step_item_inventory_extra（拼三源）
- 读：v2_browser（按 JAN 查 → 显示库存详情）

---

### `item_cost_history` — 原価变更历史

**实测行数**：0（本地 std_cost_history 空）

| 字段 | 含义 |
|---|---|
| `jan / std_cost / avg_cost / changed_by / changed_at / reason` | 变更记录 |

---

### `market_segment` — 市场字典

**实测行数**：12

| 字段 | 类型 | 含义 |
|---|---|---|
| **`market_id`** | TEXT PK | TW / SG / MY / PH / TH / VN / ID / JP / US / CN / KR / BR |
| `display_name` | TEXT | "台湾" / "Singapore" 等 |
| `currency` | TEXT | TWD / SGD 等 |
| `active` | INTEGER | 1/0 |

---

### `shop` — 店铺主档

**实测行数**：20

| 字段 | 含义 |
|---|---|
| **`shop_id`** | PK，'netsuite_Shopee BR' / 'shopee_tw_smikie' 等 |
| `market_id` | 关联 market_segment |
| `platform` | shopee / lazada / amazon / coupang / netsuite / unknown |
| `display_name / currency / owner / active / created_at` | 元信息 |

---

### `shop_sales` — 店铺 × SKU 销售明细

**实测行数**：12,150

| 字段 | 含义 |
|---|---|
| `shop_id / jan / period_start / period_end / source` | 5 维 UNIQUE |
| `qty_sold / revenue / revenue_jpy / cost / gross_profit / gross_margin` | 销售六件套 |
| `rank` | 商品ランク（来自 nst_store_sales.rank）|

---

### `shop_monthly` — 店铺月度 KPI

**实测行数**：0（本地 store_monthly.store_id 全空）

| 字段 | 含义 |
|---|---|
| **`(shop_id, year_month)`** | 复合 PK |
| `gmv / profit / margin_rate / profit_contrib / deduction_total / order_count / store_rating / online_products` | KPI |

---

### `item_supplier_link` — 商品 × 供应商关联（多对多）

**实测行数**：4,391（合并 supplier_cost 445 + supplier_jan_list 4132 去重）

| 字段 | 含义 |
|---|---|
| **`(jan, supplier_name)`** | 复合 PK |
| `cost_class` | 'AB' / 'C'（来自 supplier_cost）|
| `unit_cost / currency` | 报价（来自 supplier_cost）|
| `status` | 在该供应商的状态（来自 supplier_jan_list）|
| `source` | 'supplier_cost' / 'supplier_jan_list' / 'merged' |

**读写者**：
- 写：step_item_supplier_link
- 读：v2_browser「按供应商查」Tab

---

### `_v2_migration_runs` — ETL 历史

**实测行数**：29

| 字段 | 含义 |
|---|---|
| `step / source_table / rows_read / rows_written / errors / ran_at / notes` | 每步 ETL 记录 |

---

## B · 旧商品主数据（保留并行）

### `item` — 旧商品主表（PK = internal_id）

**实测行数**：0（这就是 Boss 之前发现的"item 表为空"问题）

| 字段 | 含义 |
|---|---|
| `internal_id` PK / `item_code` UNIQUE / `jan` | 三个 key |
| `display_name / maker / rank / handling_status` | 基础 |
| `case_qty / order_lot / weight / avg_cost / std_cost / inactive_flag` | 业务 |

⚠️ **现状**：T-010/T-011 ingestor 任务未把 NetSuite 主数据同步过来，导致这表持续为空。
v2 模型 `item_v2` 已绕过此问题。

---

### `item_master` — 供应商口径商品主档（PK = jan）

**实测行数**：6,614

来自 SKU 一元管理表格 / item_master_cleaned.csv。含 maker / actual_cost / min_cost / case_qty / order_lot / weight。

---

### `item_master_netsuite` — NetSuite「All Item 0405 sheet」（PK = internal_id）

**实测行数**：7,228

来自 NetSuite 全量商品导出。含 internal_id / upc / display_name / avg_cost / std_cost / department / rank / sku_id / created_at / maker。

> v2 ETL 主源 — `step_item_v2` 优先从这张表合并。

---

### `nst_item_summary` — NetSuite アイテム概要 8 列（PK = item_code）

**实测行数**：0（本地空）

8 列对应 アイテム.xls：A=item_code, B=upc, C=display_name, D=handling_status, E=std_cost, F=available, G=available_on_hand, H=avg_cost。

> **page 03 定義原価編集** 之前依赖此表的 H 列 avg_cost；现已加 v2 fallback。

---

## C · 旧 NST 事实表（保留并行）

### `nst_inventory_snapshot` — 多仓库库存快照

**实测行数**：5,772 · UNIQUE `(internal_id, location, bin_number)`

字段：internal_id / item_code / upc / display_name / status / bin_number / location / handling_status / qty_on_hand / qty_committed / qty_backorder / std_cost / total_amount / avg_cost / owner / department。

**读者**：page 04 / 06 / 07 / 11 / 13，modules/inventory_health/metrics.py，modules/operation_advice/proposal.py，modules/rank_classifier/proposal.py。

> v2 已通过 `item_inventory_snapshot_v2` 转换。后续 page 切换后此表退役。

---

### `nst_turnover` — 库存周转率

**实测行数**：10,479 · UNIQUE `(item_code, department)`

字段：department / item_code / handling_status / cost / avg_value / **turnover_rate** / **avg_days_on_hand**。

**读者**：page 04（用于「平均在庫日数」列）。

> ⚠️ 这表**没有 upc** — page 04 通过 nst_inventory_snapshot 拿 item_code↔upc 映射后再 join。

---

### `nst_store_sales` — 店舗 × SKU 销售（FB_店舗 维度）

**实测行数**：6,099 · UNIQUE `(fb_store, item_code)`

字段：fb_store / item_code / upc / handling_status / display_name / qty_sold / unit_price / revenue / defined_cost / gross_profit / gross_margin / rank。

> 实测 item_code 99% 是 13 位 JAN（直接当 jan 用，不需要 nst_item_summary 映射）。

---

### `store_monthly` — ASEAN 店铺月度（旧表）

**实测行数**：42（store_id 全空，仅 ASEAN/market 维度）

字段：year_month / market / store_id / online_products / revenue / profit / margin_rate / profit_contrib / store_rating / deduction_total / order_count。

---

## D · 销售事实表

### `sales_line` — 跨平台销售统一表

**实测行数**：0（本地空；Boss 的 Postgres 实际有数据）

UNIQUE：自增 id（无业务唯一约束 — DELETE+INSERT 模式）

| 关键字段 | 含义 |
|---|---|
| `store / item_code / upc / display_name / handling_status / rank / maker` | 维度 |
| `qty_sold / unit_purchase_price / revenue / defined_cost / gross_profit / gross_margin` | 销售 |
| `period_start / period_end` | 时间 |
| `source` | 'asean_monthly' / 'asean_daily' / 'export_item' / 'export_store' |

> 4 类销售导出共用这张表，通过 `source` 字段区分。

---

## E · Shopee 财务

### `shopee_orders` — Shopee 订单（PK = order_no, sku_or_jan）

**实测行数**：17,361

### `shopee_orders_raw` — 订单导出原表（UNIQUE order_no）

**实测行数**：0

### `shopee_payouts` — 拨款主档（PK = payout_id）

**实测行数**：2,114

### `shopee_fees` — Shopee 手续费明细（page 14 用）

**实测行数**：16,280

### `shopee_adjustments` — Shopee 调整费

**实测行数**：646

### `shopee_income_lines` — 拨款明细（UNIQUE order_no, refund_id）

**实测行数**：0（本地）

> page 14 Shopee 财务的数据源链路。

---

## F · 进货 + 供应商

### `purchase` — NetSuite PO 明细（PK = po_number, internal_id, ordered_at）

**实测行数**：0

### `purchase_data` — 采购预测 / 计划

**实测行数**：0（page 08 発注 AI 用）

### `purchase_history` — 历史入库

**实测行数**：0

> 三表通过 `step_item_purchase_history` 合并到 `item_purchase_history`，按 source 区分。

---

### `supplier` — 供应商主档（PK = supplier_id）

**实测行数**：0

| 字段 | 含义 |
|---|---|
| supplier_id PK / name / lead_time_days / moq / payment_terms |

---

### `supplier_cost` — 供应商报价（PK = jan, supplier_name）

**实测行数**：445

| 字段 | 含义 |
|---|---|
| jan / supplier_name / **cost_class** ('AB'/'C') / unit_cost / currency |

---

### `supplier_jan_list` — 供应商商品清单（PK = jan, supplier_name）

**实测行数**：4,132

| 字段 | 含义 |
|---|---|
| jan / supplier_name / **status** |

> 这两张表已合并到 `item_supplier_link`（v2）。

---

### `supply_cycle` — 进货周期（PK = jan）

**实测行数**：418

| 字段 | 含义 |
|---|---|
| jan / lead_time_days / bucket ('short'/'normal'/'long') |

> v2 已合并到 `item_v2.supply_cycle_days / bucket`。

---

## G · 其他业务

### `benten_stock` — 弁天仓库库存（UNIQUE jan, snapshot_at）

**实测行数**：0

字段：jan / stock / snapshot_at

**读者**：page 08 発注 AI

---

### `warehouse_stock` — 外部仓库快照（UNIQUE product_code, snapshot_at）

**实测行数**：0

字段：product_code / jan / stock_available / snapshot_at

**读者**：page 08 / page 19 保质期管理

---

### `item_expiry` — 保质期管理（来自飞书多维表手动同步）

**实测行数**：0

**读者**：page 19。

---

### `lot` — 批次（UNIQUE internal_id, lot_number）

**实测行数**：0

字段：internal_id / lot_number / expiry_date / qty_remaining / received_at。

---

### `inventory` — 主表 4 库存快照（UNIQUE internal_id, snapshot_at）

**实测行数**：0

> ⚠️ 与 `nst_inventory_snapshot` 重叠概念，本地未填。可考虑后期废弃。

---

### `inventory_snapshot` — 多仓库库存快照（UNIQUE internal_id, location, bin_number, snapshot_at）

**实测行数**：0

> ⚠️ 与 `nst_inventory_snapshot` 重叠（schema 几乎一样）。看起来是 NetSuite 不同导出版本的结果，本地为空。

---

### `inventory_turnover` — 库存周转（UNIQUE item_code, period_start, period_end）

**实测行数**：0

> ⚠️ 与 `nst_turnover` 重叠概念。本地空。

---

## H · 月度回写产物

### `health_grade_monthly`（PK = sku, year_month）

**实测行数**：3,594

| 字段 | 含义 |
|---|---|
| sku / year_month / bucket / threshold / cross_ratio / **grade**（🟢优秀/🟡健康/🟠注意/🔴死钱）/ dead_money_jpy |

写：`modules/inventory_health/metrics.py`

---

### `cross_ratio_monthly`（PK = sku, year_month）

**实测行数**：3,594

`gross_margin × turnover = cross_ratio` (健康度公式)

---

### `stock_sales_ratio_monthly`（PK = sku, year_month）

**实测行数**：3,594

`end_inventory / monthly_sales = ratio_months` (库销比)

---

### `dead_inventory_monthly`（UNIQUE jan, year_month, status）

**实测行数**：86,725 ⭐ 表里数据最多的一张

`status` 取值：'3ヶ月滞留' 等。

---

### `rank_history`（PK = sku, quarter）

**实测行数**：0

字段：old_rank / new_rank / changed_by / changed_at（page 07 等级判定写）

---

### `operation_advice_monthly`（PK = sku, year_month）

**实测行数**：1,681

写：`modules/operation_advice/proposal.py`（page 11 运营建议）

---

## I · 自动化 + 元数据

### `automation_runs` — 自动化任务跟踪 ⭐

**实测行数**：0

| 字段 | 含义 |
|---|---|
| run_id PK (uuid) / module / payload / **status** (pending/processing/completed/failed) / summary / triggered_by / triggered_at / completed_at |

**用途**：CMS → N8N webhook 触发链路追踪。Phase 2 飞书机器人 + Phase 3 Shopee 上架都依赖。

---

### `_ingest_runs` — 导入操作记录

**实测行数**：16

字段：ingestor / source_file / total_rows / inserted / updated / errors / run_at。

---

### `_ingest_errors` — 失败行明细（关联 _ingest_runs.run_id）

**实测行数**：0

字段：row_number / error_message / raw_row（JSON）

---

### `_export_runs` — NetSuite CSV 回写记录（⚠️ 已废弃 2026-06-03）

**实测行数**：0

~~写：`data_warehouse/exports/cost_update.py`~~ — `exports/` 孤儿子系统已删除（见 docs/20）；成本更新 CSV 现由 page 03 定義原価編集 + `templates/nst_item_master` 生成

---

### `_schema_version` — schema 版本（PK = version）

**实测行数**：7（多个 SCHEMA_VERSION 历史）

---

### `_v2_migration_runs` — v2 ETL 历史

见上 A 节。

---

## J · 告警

### `discontinue_alerts`（PK = jan, source, signal_type, detected_at）

**实测行数**：0

字段：jan / sku / source / **signal_type**（'販売終了' / '削除' / 'NEW' 等）/ detected_at / acknowledged_by / acknowledged_at / action

写：stock_monitor 容器（改廃监控月度 cron）；读：page 13 改廃確認

---

### `difficult_items` + `difficult_items_history`

实测行数：0 / 0

入荷困難商品（page 12 用）

---

## K · 已废弃（启动时自动 DROP）

| 表 | 状态 | DROP 触发 |
|---|---|---|
| `sales` | 空表 | `migrations.DEPRECATED_TABLES` |
| `store_profit_lines` | 无 SELECT 引用 | `migrations.DEPRECATED_TABLES` |
| `store_profit_daily_lines` | 无 SELECT 引用 | `migrations.DEPRECATED_TABLES` |

> SQLite 路径：`init_db()` 启动时跑 `DROP TABLE IF EXISTS`
> Postgres 路径：`_get_postgres_connection()` 第一次连库时跑 `DROP TABLE IF EXISTS ... CASCADE`

---

## 📊 关键关联图

```
                          item_v2 (PK = jan) ⭐
                              ▲
                    ┌─────────┼─────────────────────┐
                    │         │                     │
        item_supplier_link    │      item_purchase_history
        (jan, supplier)       │      (PO + 预测 + 历史)
                              │
                              │
   ┌──────────────────────────┴──────────────────────────┐
   │                                                      │
item_inventory_snapshot_v2                  item_sales_history
(jan, location, bin, snapshot_at)           (jan, period, channel) ↑ AGG
                                                              │
                                                       shop_sales
                                                       (shop_id, jan, period)
                                                              ▲
                                                              │
                                                              │
                                                          shop_monthly
                                                          (shop_id, ym)
                                                              ▲
                                                              │
                                                       shop ──→ market_segment
```

---

## 🎯 最佳查询路径建议（v2 主用）

| 场景 | SQL 起点 |
|---|---|
| 按品牌查 SKU + 销售 | `item_v2 WHERE maker = ?` → JOIN `shop_sales` ON jan |
| 按 JAN 查全部 | `item_v2 WHERE jan = ?` → 4 个子表 |
| 按店铺看 SKU 销售 | `shop_sales WHERE shop_id = ?` → JOIN `item_v2` |
| 按品类 / 期间汇总销售 | `item_sales_history WHERE channel LIKE 'shopee_%' GROUP BY jan` |
| 找供应商列表 | `item_supplier_link GROUP BY supplier_name` |
| 按 JAN 找供应商报价 | `item_supplier_link WHERE jan = ? ORDER BY cost_class` |
| 找停产商品 | `discontinue_alerts WHERE acknowledged_at IS NULL` |
| 健康度 D 档 | `health_grade_monthly WHERE grade LIKE '%死钱%'` |

---

## 🔄 写入路径（什么 → 哪张表）

| 输入 | ingest 模块 | 直接写入 | v2 双写（自动） |
|---|---|---|---|
| 在庫数残数 / 通常在庫 .xls | `xls_ingest.IngestInventory` | `inventory_snapshot` + `nst_inventory_snapshot` | `item_inventory_snapshot_v2` |
| 在庫回転率 .xml | `xml_netsuite.TurnoverIngestor` | `nst_turnover` | — |
| 店舗別売上 .xml | `xml_netsuite.StoreSalesIngestor` | `nst_store_sales` | `shop_sales` + `item_sales_history` |
| ASEAN 月度/前日 .xls | `xls_ingest.IngestSalesAsean*` | `sales_line` | `shop_sales` + `item_sales_history` |
| 輸出 アイテム別/店舗別 .xls | `xls_ingest.IngestSalesExport*` | `sales_line` | 同上 |
| アイテム概要 .xls | `xls_ingest.ItemSummaryIngestor` | `nst_item_summary` | `item_v2.avg_cost / std_cost` |
| 订单导出 .xlsx | `xls_ingest.IngestShopeeOrders` | `shopee_orders_raw` | — |
| Shopee 拨款 .xlsx | `xls_ingest.IngestShopeeIncome` | `shopee_income_lines` | — |
| 供应商 .xlsx | `excel_supplier.*` | `supplier_cost` + `supply_cycle` + `supplier_jan_list` | `item_supplier_link` + `item_v2` |
| All Item .xlsx | `excel_unified.*` | `item / item_master / item_master_netsuite / store_monthly` | `item_v2` + `shop_monthly` |
| 旧 CSV item_master | `items.LocalItemMasterIngestor` | `item` | — |

> v2 双写通过 page 99 Tab 1 导入完成后自动调 `tools.migrate_to_v2.run_all()` 实现。
> 即 Boss 上传新 xls → 旧表 + v2 表都自动更新，无需手工触发 ETL。
