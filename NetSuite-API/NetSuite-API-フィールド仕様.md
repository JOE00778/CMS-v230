# NetSuite REST API · データ同期フィールド仕様

> ステータス：v1 · 2026-05-11
> 目的：NetSuite REST/SuiteQL API でデータを直接同期し、現行の「Saved Search エクスポート .xls → 手動アップロード」フローを置き換える。
> 範囲：主要 4 テーブル（商品マスタ / 在庫 / 売上 / 月次完売率）+ 派生 1 テーブル（在庫回転率）。

---

## 0. 接続要件

| 項目 | 値 |
|---|---|
| Endpoint | `https://{accountID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql` |
| 認証方式 | TBA (Token-Based Auth) · OAuth 1.0 HMAC-SHA256 |
| 必須 secrets | `NS_ACCOUNT_ID` / `NS_CONSUMER_KEY` / `NS_CONSUMER_SECRET` / `NS_TOKEN_ID` / `NS_TOKEN_SECRET` |
| リクエストメソッド | `POST`、body: `{ "q": "SELECT ... FROM ..." }` |
| ページング | header `Prefer: transient` + URL `?limit=1000&offset=N`（最大 1000/ページ） |
| レート制限 | アカウント毎 5 req/s · concurrent governance |
| 必要権限 | "Log in using Access Tokens" + "SuiteAnalytics Workbook" + 各テーブルの View 権限 |

⚠️ **Custom field scriptid は未確定**：下記の `custitem_xxx` はすべて例示。実際には NetSuite 管理画面 `Customization → Lists, Records & Fields → Item Fields` で scriptid を確認し、差し替える必要あり。

---

## 1. 商品マスタ → `item_v2`

**SuiteQL メインテーブル**：`item`
**実行頻度**：1 日 1 回（増分は `lastmodifieddate` で判定）

| DB フィールド | SuiteQL フィールド | NetSuite UI 名称 | 種別 |
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

**クエリ例**：
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

## 2. 複数倉庫の在庫スナップショット → `item_inventory_snapshot_v2`

**SuiteQL メインテーブル**：`inventoryitemlocations` × `inventorynumberbin`
**実行頻度**：1 日 1 回（全量上書き、DELETE + INSERT）

| DB フィールド | SuiteQL フィールド | NetSuite UI |
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
| `total_amount` | **計算式** | 下記参照 |
| `handling_status` | `item.custitem_handling_status` | 取扱区分 |
| `status` | `inventoryitemlocations.isinactive` | 有効/無効 |
| `owner` | `item.custitem_owner` | 商品担当者 |
| `department` | `BUILTIN.DF(item.department)` | 部門 |
| `snapshot_at` | API 呼び出し時刻 | — |

**total_amount 計算式（ingest 側で計算）**：
```
弁天在庫金額 = avg_cost × 弁天.手持            # 弁天は在途なし
JD在庫金額   = avg_cost × JD.手持
JD在途金額   = avg_cost × (JD.注文待ち + JD.輸送中)

total_amount = (弁天在庫金額 + JD在庫金額) + JD在途金額
```

**クエリ例**：
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

## 3. 売上明細 → `shop_sales`

**SuiteQL メインテーブル**：`transaction` × `transactionline` × `item` × `customer`/`classification`
**実行頻度**：
- daily：毎日未明に前日分を取得
- monthly：毎月初に前月分を取得

| DB フィールド | SuiteQL フィールド | 説明 |
|---|---|---|
| `period_start` / `period_end` | `transaction.trandate` | daily: 同日；monthly: 月初/月末 |
| `granularity` | (ingest 側で固定) | `'daily'` / `'monthly'` |
| `shop_id` | `BUILTIN.DF(transaction.class)` または `customer.companyname` | 'Shopee BR' / 'Shopee Mall PH' 等 |
| `item_code` | `item.itemid` | アイテム |
| `jan` | `item.upccode` | UPCコード |
| `display_name` | `item.displayname` | 表示名 |
| `qty_sold` | `SUM(transactionline.quantity)` | 販売数量 |
| `unit_price` | `AVG(transactionline.rate)` | 単価 |
| `revenue` | `SUM(transactionline.netamount)` | 総収益（現地通貨） |
| `revenue_jpy` | `SUM(transactionline.fxamount)` | 総収益（JPY 換算） |
| `cost` | `SUM(transactionline.costestimate)` | 定義原価 |
| `gross_profit` | (revenue - cost) | 粗利（派生） |
| `gross_margin` | (gross_profit / revenue) | 粗利率（派生） |
| `handling_status` | `item.custitem_handling_status` | 取扱区分（item_v2 にも書き戻し） |
| `maker` | `item.custitem_maker` | メーカー |
| `rank` | `item.custitem_rank` | 商品ランク |

**Filter**：
```
transaction.type IN ('CustInvc','CashSale','SalesOrd')
AND transaction.status NOT IN ('Closed:Cancelled','SalesOrd:Closed:Cancelled')
AND transaction.trandate BETWEEN :start AND :end
AND transactionline.mainline = 'F'  -- 集計行を除外
AND transactionline.taxline = 'F'   -- 税行を除外
```

**クエリ例（daily）**：
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

## 4. 月次完売率 → `item_monthly_turnover`

NetSuite には **完売率に対応する SuiteQL ビューが存在しない**ため、自前で合成する必要あり。2 つのアプローチ：

### 方式 A：transactionline の月次集計（推奨）

毎月 1 日未明に前月の全 inventory movement を取得：

| DB フィールド | 計算方法 |
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
| `open_qty` | 月初の `inventoryitemlocations.quantityonhand` snapshot（**自前で保管必須**） |
| `close_qty` | 月末も同様 |
| `open_amount` | `open_qty × open_avg_cost` |
| `close_amount` | `close_qty × close_avg_cost` |
| `sell_through_rate` | `qty_sold / (open_qty + qty_total_in)` |
| `last_received_at` | `MAX(t.trandate WHERE type='ItemRcpt')` |
| `last_sold_at` | `MAX(t.trandate WHERE type IN ('CustInvc','CashSale'))` |

**SQL 骨組み**：
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

### 方式 B：既存 Saved Report の RESTlet ラッピング

NetSuite に既に `【輸出】アイテム月完売率300` という saved search があるなら、RESTlet で `search.load()` + `.run()` を呼び出して JSON で返す方法もあり。SuiteQL での自前合成より簡潔。NetSuite 管理画面で 10 行程度の JS をデプロイする必要あり。

⚠️ **期初/期末在庫の制約**：NetSuite SuiteQL からは過去スナップショットを取得できず、現時点の `inventoryitemlocations.quantityonhand` のみ参照可能。そのため**毎月末にスナップショットを必ず DB に保管**し、翌月は `close_qty` として、翌々月は `open_qty` として利用する必要あり。これには毎月末 23:59 に実行する cron job が必須。

---

## 5. 在庫回転率 → `inventory_turnover`

**完全派生テーブル**。NetSuite に対応するエンドポイント無し。ingest 側で #3 + #2 から計算：

```python
# 直近 12 ヶ月の売上
qty_sold_12m = SUM(shop_sales.qty_sold WHERE period_start > NOW - 365d) by jan, location

# 12 ヶ月末在庫の平均
avg_qoh = AVG(item_inventory_snapshot_v2.qty_on_hand
              WHERE snapshot_at = month_end) by jan, location

# 派生計算
turnover_rate      = qty_sold_12m / avg_qoh
avg_inventory_days = 365 / turnover_rate
```

API 呼び出しは不要。

---

## 📦 同期頻度とトリガー一覧

| データ | 頻度 | トリガー | API 呼び出し回数 |
|---|---|---|---|
| `item_v2`（マスタ） | 1 日 1 回 | cron 03:00 JST | 全量 1 回 / 増分 N 回 |
| `item_inventory_snapshot_v2` | 1 日 1 回 | cron 03:05 JST | 全量 1 回 |
| `shop_sales` daily | 1 日 1 回 | cron 03:10 JST（前日分） | 1 回 |
| `shop_sales` monthly | 月 1 回 | cron 毎月 1 日 03:15 JST | 1 回 |
| `item_monthly_turnover` | 月 1 回 | cron 毎月 1 日 03:20 JST + 毎月末 23:59（snapshot） | 2 回 |
| `inventory_turnover` | 週 1 回 | 派生計算（API 不要） | 0 回 |

**1 日あたりの API 呼び出し見込み**：約 5 回。NetSuite の上限（5 req/s × 86400 s = 432K/日）を大幅に下回る。

---

## 🚦 Boss にご協力いただきたい事項

1. **TBA の 5 つの secret**（一度きり）
   - NS Account ID（NetSuite 画面左上の Account フィールド）
   - Consumer Key / Secret（Integration 設定で生成）
   - Token ID / Secret（Access Token 設定で生成）

2. **Custom field の scriptid 一覧**（一度きり）
   - メーカー → scriptid: `custitem_???`
   - 商品ランク → `custitem_???`
   - 取扱区分 → `custitem_???`
   - 商品担当者 → `custitem_???`
   - カートン入数 → `custitem_???`

3. **`inventorynumberbin` テーブルの有効状態**：会社で Bin Management モジュールが未導入の場合、`bin_number` フィールドが取得できないため、Item の `binnumber` または別の custom field から取得する必要あり。

4. **`item.class`（店舗ディメンション）の確認**：売上を店舗別に集計する際、`transaction.class` を使うか `customer.companyname` を使うか。どちらも可能だが、class の方が安定。

---

## ⏭️ 導入ステップ提案

1. **Spike**：Boss から TBA + custom field scriptid を 1 件いただき、`SELECT 5 件の item` で疎通確認
2. **Phase 1**：item_v2 + inventory_snapshot をリリース（既存 2 報表を置き換え）
3. **Phase 2**：shop_sales daily / monthly をリリース（既存 2 報表を置き換え）
4. **Phase 3**：item_monthly_turnover + 月末 snapshot cron をリリース（既存 1 報表 + 派生を置き換え）
5. **Phase 4**：inventory_turnover を API データで再計算（既存 1 報表を置き換え）

全 Phase 完了後、**6 つの .xls アップロードフローを完全廃止**。page 99「データインポートと設定」は「API 同期ステータスダッシュボード」へ転換。
