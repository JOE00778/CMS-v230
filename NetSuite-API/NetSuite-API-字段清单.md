# NetSuite REST API · 数据同步字段清单

> 状态：v1 · 2026-05-11
> 目的：用 NetSuite REST/SuiteQL API 直接同步数据，替代当前「Saved Search 导出 .xls → 手动上传」流程。
> 范围：覆盖 4 大主表（商品主档 / 库存 / 销售 / 月度完売率）+ 1 个派生表（库存周转率）。

---

## 0. 接入要点

| 项 | 值 |
|---|---|
| Endpoint | `https://{accountID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql` |
| 认证 | TBA (Token-Based Auth) · OAuth 1.0 HMAC-SHA256 |
| 必需 secrets | `NS_ACCOUNT_ID` / `NS_CONSUMER_KEY` / `NS_CONSUMER_SECRET` / `NS_TOKEN_ID` / `NS_TOKEN_SECRET` |
| 请求方法 | `POST`，body: `{ "q": "SELECT ... FROM ..." }` |
| 分页 | header `Prefer: transient` + URL `?limit=1000&offset=N`（max 1000/页） |
| 频控 | 每账号 5 req/s · concurrent governance |
| 必要权限 | "Log in using Access Tokens" + "SuiteAnalytics Workbook" + 各表 View 权限 |

⚠️ **Custom field scriptid 未确定**：下方所有 `custitem_xxx` 是示例命名，实际需要 Boss 在 NetSuite 后台 `Customization → Lists, Records & Fields → Item Fields` 查 scriptid 后替换。

---

## 1. 商品主档 → `item_v2`

**SuiteQL 主表**：`item`
**触发频率**：日 1 次（增量按 `lastmodifieddate`）

| DB 字段 | SuiteQL 字段 | NetSuite UI 名 | 类型 |
|---|---|---|---|
| `internal_id` | `id` | 内部ID | Std |
| `item_code` | `itemid` | 名前 | Std |
| `jan` (PK) | `upccode` | UPCコード | Std |
| `display_name` | `displayname` | 表示名 | Std |
| `maker` | `custitem_maker` | メーカー | Custom |
| `rank` | `custitem_rank` | 商品ランク | Custom |
| `handling_status` | `custitem_handling_status` | 取扱区分 | Custom |
| `department` | `department` | 部門 | Std (FK) |
| `owner` | `custitem_owner` | 商品担当者 | Custom |
| `avg_cost` | `averagecost` | 平均原価 | Std |
| `std_cost` | `cost` | アイテム定義原価 | Std |
| `actual_cost` | `lastpurchaseprice` | 前回購入価格 | Std |
| `case_qty` | `custitem_case_qty` | カートン入数 | Custom |
| `weight` | `weight` | 商品重量(g) | Std |

**查询示例**：
```sql
SELECT id, itemid, upccode, displayname,
       custitem_maker, custitem_rank, custitem_handling_status,
       BUILTIN.DF(department) AS department,
       custitem_owner,
       averagecost, cost, lastpurchaseprice,
       custitem_case_qty, weight
FROM item
WHERE isinactive = 'F'
  AND lastmodifieddate > TO_DATE(:last_sync, 'YYYY-MM-DD')
```

---

## 2. 多仓库库存快照 → `item_inventory_snapshot_v2`

**SuiteQL 主表**：`inventoryitemlocations` × `inventorynumberbin`
**触发频率**：日 1 次（全量覆盖，DELETE + INSERT）

| DB 字段 | SuiteQL 字段 | NetSuite UI |
|---|---|---|
| `jan` | `item.upccode` | UPCコード |
| `item_code` | `item.itemid` | 名前 |
| `internal_id` | `item.id` | 内部ID |
| `display_name` | `item.displayname` | 表示名 |
| `location` | `location.name` | 倉庫名 |
| `bin_number` | `bin.binnumber` | 保管棚番号 |
| `qty_on_hand` | `quantityonhand` | 手持 |
| `qty_committed` | `quantitycommitted` | 確保済 |
| `qty_backorder` | `quantitybackordered` | 注文待ち |
| `qty_on_order` | `quantityonorder` | 注文済 |
| `qty_in_transit` | `quantityintransit` | 輸送中 |
| `qty_waiting` | `quantityavailable` | 利用可能 |
| `std_cost` | `item.cost` | 定義原価 |
| `avg_cost` | `item.averagecost` | 平均原価 |
| `total_amount` | **公式** | 见下方 |
| `handling_status` | `item.custitem_handling_status` | 取扱区分 |
| `status` | `inventoryitemlocations.isinactive` | 有効/無効 |
| `owner` | `item.custitem_owner` | 商品担当者 |
| `department` | `BUILTIN.DF(item.department)` | 部門 |
| `snapshot_at` | API 调用时刻 | — |

**total_amount 公式（ingest 端算）**：
```
弁天在庫金額 = avg_cost × 弁天.手持            # 弁天 没在途
JD在庫金額   = avg_cost × JD.手持
JD在途金額   = avg_cost × (JD.注文待ち + JD.輸送中)

total_amount = (弁天在庫金額 + JD在庫金額) + JD在途金額
```

**查询示例**：
```sql
SELECT
  item.upccode AS jan,
  item.itemid AS item_code,
  item.id AS internal_id,
  item.displayname AS display_name,
  location.name AS location,
  bin.binnumber AS bin_number,
  iil.quantityonhand,
  iil.quantitycommitted,
  iil.quantitybackordered,
  iil.quantityonorder,
  iil.quantityintransit,
  iil.quantityavailable,
  item.cost AS std_cost,
  item.averagecost AS avg_cost,
  item.custitem_handling_status,
  item.custitem_owner,
  BUILTIN.DF(item.department) AS department
FROM inventoryitemlocations iil
JOIN item ON item.id = iil.item
JOIN location ON location.id = iil.location
LEFT JOIN inventorynumberbin bin ON bin.item = item.id AND bin.location = iil.location
WHERE iil.isinactive = 'F'
  AND location.name IN ('JD-物流-千葉', '弁天倉庫')
```

---

## 3. 销售明细 → `shop_sales`

**SuiteQL 主表**：`transaction` × `transactionline` × `item` × `customer`/`classification`
**触发频率**：
- daily：每日凌晨拉前一天
- monthly：每月初拉上月

| DB 字段 | SuiteQL 字段 | 说明 |
|---|---|---|
| `period_start` / `period_end` | `transaction.trandate` | daily: 同一天；monthly: 月首/月尾 |
| `granularity` | (ingest 端固定) | `'daily'` / `'monthly'` |
| `shop_id` | `BUILTIN.DF(transaction.class)` 或 `customer.companyname` | 'Shopee BR' / 'Shopee Mall PH' 等 |
| `item_code` | `item.itemid` | アイテム |
| `jan` | `item.upccode` | UPCコード |
| `display_name` | `item.displayname` | 表示名 |
| `qty_sold` | `SUM(transactionline.quantity)` | 販売数量 |
| `unit_price` | `AVG(transactionline.rate)` | 単価 |
| `revenue` | `SUM(transactionline.netamount)` | 総収益（当地币） |
| `revenue_jpy` | `SUM(transactionline.fxamount)` | 総収益（JPY 折算） |
| `cost` | `SUM(transactionline.costestimate)` | 定義原価 |
| `gross_profit` | (revenue - cost) | 粗利（派生） |
| `gross_margin` | (gross_profit / revenue) | 粗利率（派生） |
| `handling_status` | `item.custitem_handling_status` | 取扱区分（也写回 item_v2） |
| `maker` | `item.custitem_maker` | メーカー |
| `rank` | `item.custitem_rank` | 商品ランク |

**Filter**：
```
transaction.type IN ('CustInvc','CashSale','SalesOrd')
AND transaction.status NOT IN ('Closed:Cancelled','SalesOrd:Closed:Cancelled')
AND transaction.trandate BETWEEN :start AND :end
AND transactionline.mainline = 'F'  -- 排除汇总行
AND transactionline.taxline = 'F'   -- 排除税行
```

**查询示例（daily）**：
```sql
SELECT
  BUILTIN.DF(t.class) AS shop_id,
  i.itemid AS item_code,
  i.upccode AS jan,
  i.displayname AS display_name,
  SUM(tl.quantity) AS qty_sold,
  SUM(tl.netamount) AS revenue,
  SUM(tl.costestimate) AS cost,
  i.custitem_maker,
  i.custitem_rank,
  i.custitem_handling_status
FROM transactionline tl
JOIN transaction t ON t.id = tl.transaction
JOIN item i ON i.id = tl.item
WHERE t.type IN ('CustInvc','CashSale')
  AND t.status NOT LIKE '%Cancelled%'
  AND t.trandate = TO_DATE(:target_date, 'YYYY-MM-DD')
  AND tl.mainline = 'F' AND tl.taxline = 'F'
GROUP BY t.class, i.itemid, i.upccode, i.displayname,
         i.custitem_maker, i.custitem_rank, i.custitem_handling_status
```

---

## 4. 月度完売率 → `item_monthly_turnover`

NetSuite **没现成 SuiteQL 视图**，需自己合成。两条路：

### 方案 A：transactionline 月聚合（推荐）

每月 1 号凌晨拉上月所有 inventory movement：

| DB 字段 | 计算方式 |
|---|---|
| `jan` | `item.upccode` |
| `location` | `location.name` |
| `year_month` | `TO_CHAR(t.trandate, 'YYYY-MM')` |
| `qty_received` | `SUM(qty WHERE type IN ('ItemRcpt','InvAdjst+'))` |
| `qty_other_in` | `SUM(qty WHERE type IN ('TrnfrOrd:In','Bldg+'))` |
| `qty_total_in` | `qty_received + qty_other_in` |
| `qty_sold` | `SUM(-qty WHERE type IN ('CustInvc','CashSale'))` |
| `qty_other_out` | `SUM(-qty WHERE type IN ('TrnfrOrd:Out','Bldg-','InvAdjst-'))` |
| `qty_total_out` | `qty_sold + qty_other_out` |
| `open_qty` | 月初 `inventoryitemlocations.quantityonhand` snapshot（**要自己保留**） |
| `close_qty` | 月末同上 |
| `open_amount` | `open_qty × open_avg_cost` |
| `close_amount` | `close_qty × close_avg_cost` |
| `sell_through_rate` | `qty_sold / (open_qty + qty_total_in)` |
| `last_received_at` | `MAX(t.trandate WHERE type='ItemRcpt')` |
| `last_sold_at` | `MAX(t.trandate WHERE type IN ('CustInvc','CashSale'))` |

**SQL 骨架**：
```sql
SELECT
  i.upccode AS jan,
  l.name AS location,
  TO_CHAR(t.trandate, 'YYYY-MM') AS year_month,
  SUM(CASE WHEN t.type = 'ItemRcpt' THEN tl.quantity ELSE 0 END) AS qty_received,
  SUM(CASE WHEN t.type IN ('CustInvc','CashSale') THEN -tl.quantity ELSE 0 END) AS qty_sold,
  MAX(CASE WHEN t.type = 'ItemRcpt' THEN t.trandate END) AS last_received_at,
  MAX(CASE WHEN t.type IN ('CustInvc','CashSale') THEN t.trandate END) AS last_sold_at
FROM transactionline tl
JOIN transaction t ON t.id = tl.transaction
JOIN item i ON i.id = tl.item
JOIN location l ON l.id = tl.location
WHERE t.trandate BETWEEN :month_start AND :month_end
  AND tl.mainline = 'F'
GROUP BY i.upccode, l.name, TO_CHAR(t.trandate, 'YYYY-MM')
```

### 方案 B：RESTlet 包装现有 Saved Report

如果 Boss 在 NetSuite 已有 `【輸出】アイテム月完売率300` 这张 saved search，可以建一个 RESTlet 调 `search.load()` + `.run()` 返回 JSON，绕过 SuiteQL 自己合成的复杂度。要 Boss 协助在 NetSuite 后台部署 RESTlet（10 行 JS）。

⚠️ **期初/期末库存的硬约束**：NetSuite SuiteQL 拿不到历史快照，只能拿当前 `inventoryitemlocations.quantityonhand`。所以**月末必须打一次 snapshot 入库**，下个月当 `close_qty` 用，再下个月当 `open_qty` 用。这需要一个 cron job 每月最后一天 23:59 跑。

---

## 5. 库存周转率 → `inventory_turnover`

**完全派生表**，NetSuite 无对应。ingest 端用 #3 + #2 算：

```python
# 近 12 个月销售
qty_sold_12m = SUM(shop_sales.qty_sold WHERE period_start > NOW - 365d) by jan, location

# 12 个月末库存均值
avg_qoh = AVG(item_inventory_snapshot_v2.qty_on_hand
              WHERE snapshot_at = month_end) by jan, location

# 派生
turnover_rate     = qty_sold_12m / avg_qoh
avg_inventory_days = 365 / turnover_rate
```

不需要单独 API 调用。

---

## 📦 同步频率与触发器总览

| 数据 | 频率 | 触发方式 | API 调用次数 |
|---|---|---|---|
| `item_v2` (主档) | 日 1 次 | cron 03:00 JST | 1 次全量 / N 次增量 |
| `item_inventory_snapshot_v2` | 日 1 次 | cron 03:05 JST | 1 次全量 |
| `shop_sales` daily | 日 1 次 | cron 03:10 JST（拉前一天） | 1 次 |
| `shop_sales` monthly | 月 1 次 | cron 每月 1 号 03:15 JST | 1 次 |
| `item_monthly_turnover` | 月 1 次 | cron 每月 1 号 03:20 JST + 每月最后一天 23:59（snapshot） | 2 次 |
| `inventory_turnover` | 周 1 次 | 派生，无 API | 0 次 |

**预估日均 API 调用量**：~5 次 / 日，远低于 NetSuite 配额（5 req/s × 86400 s = 432K/日）。

---

## 🚦 Boss 需要配合的事

1. **TBA 5 个 secret**（一次性）
   - NS Account ID（左上角 Account 字段）
   - Consumer Key / Secret（Integration 配置生成）
   - Token ID / Secret（Access Token 配置生成）
   
2. **Custom field scriptid 清单**（一次性）
   - メーカー → scriptid: `custitem_???`
   - 商品ランク → `custitem_???`
   - 取扱区分 → `custitem_???`
   - 商品担当者 → `custitem_???`
   - カートン入数 → `custitem_???`
   
3. **`inventorynumberbin` 表是否启用**：如果公司未开 Bin Management 模块, `bin_number` 字段拿不到，需要从 Item 上的 `binnumber` 或自定义字段取。

4. **`item.class`（店铺维度）确认**：销售按店铺切是用 `transaction.class` 还是 `customer.companyname`？两者都可，但 class 更稳定。

---

## ⏭️ 落地步骤建议

1. **Spike**：Boss 提供 TBA + 1 个 custom field scriptid，跑 SELECT 5 行 item 验证连通
2. **Phase 1**：item_v2 + inventory_snapshot 上线（替代 2 份报表）
3. **Phase 2**：shop_sales daily / monthly 上线（替代 2 份报表）
4. **Phase 3**：item_monthly_turnover + 月末 snapshot cron 上线（替代 1 份报表 + 派生）
5. **Phase 4**：inventory_turnover 派生表用 API 数据重算（替代 1 份报表）

全部完成后，**6 份 .xls 上传流程彻底废弃**，page 99「数据导入与设置」改成「API 同步状态面板」。
