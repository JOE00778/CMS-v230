-- 一元管理系统 V2.3 · Postgres schema（NAS Self-hosted）
-- 来源：data_warehouse/db/schema.sql（SQLite 版） · 自动转换
-- 差异：INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL；REAL → DOUBLE PRECISION
-- 47 张表 / 29 个序列 / 时间字段保持 TEXT (ISO 8601) 兼容现有代码
-- Docker 首次启动时自动执行（docker-entrypoint-initdb.d/01-schema.sql）

-- ============================================================
-- 主表 1：item — 商品主档
-- ============================================================
CREATE TABLE IF NOT EXISTS item (
  internal_id      TEXT PRIMARY KEY,
  item_code        TEXT NOT NULL,
  jan              TEXT,
  display_name     TEXT,
  maker            TEXT,
  rank             TEXT,
  handling_status  TEXT,
  case_qty         INTEGER,
  order_lot        INTEGER,
  weight           DOUBLE PRECISION,
  avg_cost         DOUBLE PRECISION,
  std_cost         DOUBLE PRECISION,
  inactive_flag    INTEGER NOT NULL DEFAULT 0,
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(item_code)
);
CREATE INDEX IF NOT EXISTS idx_item_code           ON item(item_code);
CREATE INDEX IF NOT EXISTS idx_item_maker          ON item(maker);
CREATE INDEX IF NOT EXISTS idx_item_rank           ON item(rank);
CREATE INDEX IF NOT EXISTS idx_item_handling       ON item(handling_status);
CREATE INDEX IF NOT EXISTS idx_item_inactive       ON item(inactive_flag);

-- ============================================================
-- 主表 2：supplier — 供应商主档
-- ============================================================
CREATE TABLE IF NOT EXISTS supplier (
  supplier_id      TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  lead_time_days   INTEGER,
  moq              INTEGER,
  payment_terms    TEXT,
  source_file      TEXT,
  imported_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplier_name       ON supplier(name);

-- ============================================================
-- 主表 3：sales — 销售明细（按订单行）
-- ============================================================
CREATE TABLE IF NOT EXISTS sales (
  id               BIGSERIAL PRIMARY KEY,
  order_id         TEXT,
  internal_id      TEXT NOT NULL REFERENCES item(internal_id),
  store            TEXT,
  sold_at          TEXT NOT NULL,
  qty              INTEGER NOT NULL,
  unit_price       DOUBLE PRECISION,
  unit_cost        DOUBLE PRECISION,
  currency         TEXT,
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(order_id, internal_id, sold_at)  -- 防重复导入
);
CREATE INDEX IF NOT EXISTS idx_sales_internal      ON sales(internal_id);
CREATE INDEX IF NOT EXISTS idx_sales_sold_at       ON sales(sold_at);
CREATE INDEX IF NOT EXISTS idx_sales_store         ON sales(store);

-- ============================================================
-- 主表 4：inventory — 库存快照（按时点）
-- ============================================================
CREATE TABLE IF NOT EXISTS inventory (
  id               BIGSERIAL PRIMARY KEY,
  internal_id      TEXT NOT NULL REFERENCES item(internal_id),
  snapshot_at      TEXT NOT NULL,
  qty_on_hand      INTEGER NOT NULL,
  qty_committed    INTEGER,
  qty_on_order     INTEGER,
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(internal_id, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_inventory_internal  ON inventory(internal_id);
CREATE INDEX IF NOT EXISTS idx_inventory_snapshot  ON inventory(snapshot_at);

-- ============================================================
-- 主表 5：purchase — 采购明细（PO + Receipt）
-- ============================================================
CREATE TABLE IF NOT EXISTS purchase (
  id               BIGSERIAL PRIMARY KEY,
  po_number        TEXT,
  internal_id      TEXT NOT NULL REFERENCES item(internal_id),
  supplier_id      TEXT REFERENCES supplier(supplier_id),
  ordered_at       TEXT,
  received_at      TEXT,
  qty              INTEGER NOT NULL,
  unit_price       DOUBLE PRECISION NOT NULL,
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(po_number, internal_id, ordered_at)
);
CREATE INDEX IF NOT EXISTS idx_purchase_internal   ON purchase(internal_id);
CREATE INDEX IF NOT EXISTS idx_purchase_supplier   ON purchase(supplier_id);
CREATE INDEX IF NOT EXISTS idx_purchase_ordered    ON purchase(ordered_at);

-- ============================================================
-- 主表 6：lot — 批次 / 赏味期限
-- ============================================================
CREATE TABLE IF NOT EXISTS lot (
  id               BIGSERIAL PRIMARY KEY,
  internal_id      TEXT NOT NULL REFERENCES item(internal_id),
  lot_number       TEXT NOT NULL,
  expiry_date      TEXT,
  qty_remaining    INTEGER,
  received_at      TEXT,
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(internal_id, lot_number)
);
CREATE INDEX IF NOT EXISTS idx_lot_internal        ON lot(internal_id);
CREATE INDEX IF NOT EXISTS idx_lot_expiry          ON lot(expiry_date);

-- ============================================================
-- 共享审计表：_ingest_runs — 每次导入操作记录
-- ============================================================
CREATE TABLE IF NOT EXISTS _ingest_runs (
  run_id           BIGSERIAL PRIMARY KEY,
  ingestor         TEXT NOT NULL,        -- ingest/items.py 等模块名
  source_file      TEXT NOT NULL,
  total_rows       INTEGER NOT NULL,
  inserted         INTEGER NOT NULL,
  updated          INTEGER NOT NULL,
  errors           INTEGER NOT NULL,
  run_at           TEXT NOT NULL,
  notes            TEXT
);

-- ============================================================
-- 共享审计表：_ingest_errors — 失败行明细
-- ============================================================
CREATE TABLE IF NOT EXISTS _ingest_errors (
  id               BIGSERIAL PRIMARY KEY,
  run_id           INTEGER NOT NULL REFERENCES _ingest_runs(run_id),
  row_number       INTEGER,
  error_message    TEXT NOT NULL,
  raw_row          TEXT                  -- JSON 序列化的原始行
);
CREATE INDEX IF NOT EXISTS idx_ingest_errors_run   ON _ingest_errors(run_id);

-- ============================================================
-- 共享审计表：_export_runs — 每次回写 NetSuite CSV 生成记录
-- ============================================================
CREATE TABLE IF NOT EXISTS _export_runs (
  export_id        BIGSERIAL PRIMARY KEY,
  exporter         TEXT NOT NULL,        -- exports/cost_update.py 等模块名
  output_file      TEXT NOT NULL,
  row_count        INTEGER NOT NULL,
  run_at           TEXT NOT NULL,
  notes            TEXT
);

-- ============================================================
-- 主表 7：inventory_snapshot — 多仓库库存快照（NetSuite 在库数据导出）
-- 同 SKU × 同 location × 同时间快照唯一
-- ============================================================
CREATE TABLE IF NOT EXISTS inventory_snapshot (
  id              BIGSERIAL PRIMARY KEY,
  internal_id     TEXT NOT NULL,
  item_code       TEXT NOT NULL,        -- アイテム
  upc             TEXT,                  -- UPCコード = JAN
  display_name    TEXT,
  status          TEXT,                  -- ステータス（通常在庫 等）
  bin_number      TEXT,                  -- 保管棚番号
  location        TEXT,                  -- 場所（仓库）
  handling_status TEXT,                  -- 取扱区分
  qty_on_hand     DOUBLE PRECISION,                  -- 手持合計
  qty_committed   DOUBLE PRECISION,                  -- 確保済合計
  qty_backorder   DOUBLE PRECISION,                  -- バック・オーダー合計
  std_cost        DOUBLE PRECISION,                  -- アイテム定義原価
  total_amount    DOUBLE PRECISION,                  -- 合計金額
  avg_cost        DOUBLE PRECISION,                  -- 平均原価合計
  owner           TEXT,                  -- 商品担当者
  department      TEXT,                  -- 部門
  snapshot_at     TEXT NOT NULL,         -- 快照时点（导入时刻或 NetSuite 报告时间）
  source_file     TEXT,
  imported_at     TEXT NOT NULL,
  UNIQUE(internal_id, location, bin_number, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_inv_snap_internal  ON inventory_snapshot(internal_id);
CREATE INDEX IF NOT EXISTS idx_inv_snap_item      ON inventory_snapshot(item_code);
CREATE INDEX IF NOT EXISTS idx_inv_snap_loc       ON inventory_snapshot(location);
CREATE INDEX IF NOT EXISTS idx_inv_snap_at        ON inventory_snapshot(snapshot_at);
CREATE INDEX IF NOT EXISTS idx_inv_snap_handling  ON inventory_snapshot(handling_status);

-- ============================================================
-- 主表 8：sales_line — 销售明细（统一事实表）
-- 4 类销售导出（ASEAN 月度/日度/輸出アイテム別/輸出店舗別）共用
-- 通过 source 字段区分来源
-- ============================================================
CREATE TABLE IF NOT EXISTS sales_line (
  id                  BIGSERIAL PRIMARY KEY,
  store               TEXT,              -- nullable（SKU-only 报表无）
  item_code           TEXT NOT NULL,     -- アイテム
  upc                 TEXT,              -- UPCコード（如有）
  display_name        TEXT,
  handling_status     TEXT,              -- 取扱区分
  maker               TEXT,              -- メーカー名（来源 ASEAN 集計専用 R7 第11列）
  rank                TEXT,              -- 商品ランク
  qty_sold            DOUBLE PRECISION,              -- 販売数量
  unit_purchase_price REAL,              -- 購入価格（単価）（仅 輸出アイテム別 带）
  revenue             DOUBLE PRECISION,              -- 総収益
  defined_cost        DOUBLE PRECISION,              -- 定義原価
  gross_profit        DOUBLE PRECISION,              -- 粗利
  gross_margin        DOUBLE PRECISION,              -- 粗利率
  period_start        TEXT NOT NULL,
  period_end          TEXT NOT NULL,
  source              TEXT NOT NULL,     -- 'asean_monthly' / 'asean_daily' / 'export_item' / 'export_store'
  source_file         TEXT,
  imported_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_line_item    ON sales_line(item_code);
CREATE INDEX IF NOT EXISTS idx_sales_line_store   ON sales_line(store);
CREATE INDEX IF NOT EXISTS idx_sales_line_period  ON sales_line(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_sales_line_source  ON sales_line(source);

-- ============================================================
-- 主表 9：inventory_turnover — 库存周转率（在庫回転率 导出）
-- ============================================================
CREATE TABLE IF NOT EXISTS inventory_turnover (
  id                BIGSERIAL PRIMARY KEY,
  item_code         TEXT NOT NULL,        -- アイテム
  description       TEXT,                  -- 説明
  cost              DOUBLE PRECISION,                  -- 原価
  avg_value         DOUBLE PRECISION,                  -- 平均値
  turnover_rate     DOUBLE PRECISION,                  -- 回転率
  avg_days_on_hand  DOUBLE PRECISION,                  -- 平均手持日数
  period_start      TEXT NOT NULL,
  period_end        TEXT NOT NULL,
  source_file       TEXT,
  imported_at       TEXT NOT NULL,
  UNIQUE(item_code, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_turnover_item  ON inventory_turnover(item_code);
CREATE INDEX IF NOT EXISTS idx_turnover_rate  ON inventory_turnover(turnover_rate);

-- ============================================================
-- 月度完売率 · アイテム月完売率300 → 库存健康度 + 订货依据数据源
-- (新增 2026-05-09; SCHEMA_VERSION 16)
-- ============================================================
CREATE TABLE IF NOT EXISTS item_monthly_turnover (
  id                BIGSERIAL PRIMARY KEY,
  item_code         TEXT NOT NULL,
  jan               TEXT,
  location          TEXT,
  department        TEXT,
  year_month        TEXT NOT NULL,
  open_qty          DOUBLE PRECISION,
  open_avg_cost     DOUBLE PRECISION,
  open_amount       DOUBLE PRECISION,
  qty_received      DOUBLE PRECISION,
  qty_other_in      DOUBLE PRECISION,
  qty_total_in      DOUBLE PRECISION,
  manual_input      DOUBLE PRECISION,
  last_received_at  TEXT,
  qty_sold          DOUBLE PRECISION,
  qty_other_out     DOUBLE PRECISION,
  qty_total_out     DOUBLE PRECISION,
  out_amount        DOUBLE PRECISION,
  last_sold_at      TEXT,
  close_qty         DOUBLE PRECISION,
  close_avg_cost    DOUBLE PRECISION,
  close_amount      DOUBLE PRECISION,
  sell_through_rate DOUBLE PRECISION,
  rank              TEXT,
  risk_label        TEXT,
  imported_at       TEXT NOT NULL,
  UNIQUE(item_code, location, year_month)
);
CREATE INDEX IF NOT EXISTS idx_imt_item ON item_monthly_turnover(item_code);
CREATE INDEX IF NOT EXISTS idx_imt_jan  ON item_monthly_turnover(jan);
CREATE INDEX IF NOT EXISTS idx_imt_ym   ON item_monthly_turnover(year_month);
CREATE INDEX IF NOT EXISTS idx_imt_rate ON item_monthly_turnover(sell_through_rate);
CREATE INDEX IF NOT EXISTS idx_imt_risk ON item_monthly_turnover(risk_label);

-- ============================================================
-- 模块 #11：difficult_items — 入荷困難商品（人工录入）
-- ============================================================
CREATE TABLE IF NOT EXISTS difficult_items (
  id           BIGSERIAL PRIMARY KEY,
  item_key     TEXT NOT NULL,           -- ブランド / 商品名 / JAN 自由文本
  reason       TEXT NOT NULL,
  note         TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_items_key  ON difficult_items(item_key);

CREATE TABLE IF NOT EXISTS difficult_items_history (
  id          BIGSERIAL PRIMARY KEY,
  item_id     INTEGER NOT NULL,
  item_key    TEXT NOT NULL,
  reason      TEXT,
  note        TEXT,
  action      TEXT NOT NULL,            -- insert / update / delete
  action_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_hist_item  ON difficult_items_history(item_id);
CREATE INDEX IF NOT EXISTS idx_diff_hist_at    ON difficult_items_history(action_at);

-- ============================================================
-- ============================================================
-- 模块 #5：item_master — 一元くん sheet 数据（主档扩展）
-- ============================================================
CREATE TABLE IF NOT EXISTS item_master (
  jan              TEXT PRIMARY KEY,       -- JAN コード
  item_code        TEXT,                   -- 商品コード
  rank             TEXT,                   -- ランク
  maker            TEXT,                   -- メーカー名
  display_name     TEXT,                   -- 商品名
  handling_status  TEXT,                   -- 取扱区分
  on_hand          INTEGER,                -- 在庫
  on_order         INTEGER,                -- 発注済
  actual_cost      DOUBLE PRECISION,                   -- 実績原価
  min_cost         DOUBLE PRECISION,                   -- 最安原価
  case_qty         INTEGER,                -- ケース入数
  order_lot        INTEGER,                -- 発注ロット
  weight           DOUBLE PRECISION,                   -- 重量
  source_file      TEXT,
  imported_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_master_code     ON item_master(item_code);
CREATE INDEX IF NOT EXISTS idx_item_master_maker    ON item_master(maker);
CREATE INDEX IF NOT EXISTS idx_item_master_rank     ON item_master(rank);

-- ============================================================
-- 模块 #5：item_master_netsuite — All Item 0405 sheet 数据
-- ============================================================
CREATE TABLE IF NOT EXISTS item_master_netsuite (
  internal_id      TEXT PRIMARY KEY,       -- 内部ID
  upc              TEXT,                   -- UPC
  display_name     TEXT,                   -- 表示名
  avg_cost         DOUBLE PRECISION,                   -- 平均原価
  std_cost         DOUBLE PRECISION,                   -- アイテム定義原価
  last_purchase    DOUBLE PRECISION,                   -- 前回購入価格
  on_hand          DOUBLE PRECISION,                   -- 手持
  available        DOUBLE PRECISION,                   -- 利用可能
  on_order         DOUBLE PRECISION,                   -- 注文済
  department       TEXT,                   -- 部門
  rank             TEXT,                   -- 商品ランク
  sku_id           TEXT,                   -- skuID
  created_at       TEXT,                   -- 作成日
  maker            TEXT,                   -- メーカー名
  source_file      TEXT,
  imported_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_ns_upc       ON item_master_netsuite(upc);
CREATE INDEX IF NOT EXISTS idx_item_ns_name      ON item_master_netsuite(display_name);
CREATE INDEX IF NOT EXISTS idx_item_ns_dept      ON item_master_netsuite(department);

-- ============================================================
-- 模块 #5：store_monthly — 店舗別 sheet 数据（月度 + 店铺维度）
-- ============================================================
CREATE TABLE IF NOT EXISTS store_monthly (
  id               BIGSERIAL PRIMARY KEY,
  year_month       TEXT NOT NULL,          -- 年月（YYYYMM）
  market           TEXT,                   -- 市場（US/PH 等）
  store_id         TEXT,                   -- 店铺ID
  online_products  INTEGER,                -- 在线产品数
  revenue          DOUBLE PRECISION,                   -- 营业額
  profit           DOUBLE PRECISION,                   -- 利潤
  margin_rate      DOUBLE PRECISION,                   -- 毛利率
  profit_contrib   DOUBLE PRECISION,                   -- 利潤貢献率
  store_rating     DOUBLE PRECISION,                   -- 店舗評価
  deduction_total  DOUBLE PRECISION,                   -- 扣减合計
  order_count      INTEGER,                -- 訂單數
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(year_month, market, store_id)
);
CREATE INDEX IF NOT EXISTS idx_store_monthly_ym    ON store_monthly(year_month);
CREATE INDEX IF NOT EXISTS idx_store_monthly_mkt   ON store_monthly(market);
CREATE INDEX IF NOT EXISTS idx_store_monthly_store ON store_monthly(store_id);

-- ============================================================
-- 模块 #5：dead_inventory_monthly — 不动库存分析 sheet 数据（月度滚动）
-- ============================================================
CREATE TABLE IF NOT EXISTS dead_inventory_monthly (
  id               BIGSERIAL PRIMARY KEY,
  jan              TEXT NOT NULL,          -- JAN
  display_name     TEXT,                   -- 表示名
  year_month       TEXT NOT NULL,          -- 年月（YYYYMM）
  status           TEXT,                   -- 状態（3ヶ月滞留 等）
  inventory_amount REAL,                   -- 庫存金額
  source_file      TEXT,
  imported_at      TEXT NOT NULL,
  UNIQUE(jan, year_month, status)
);
CREATE INDEX IF NOT EXISTS idx_dead_jan     ON dead_inventory_monthly(jan);
CREATE INDEX IF NOT EXISTS idx_dead_ym      ON dead_inventory_monthly(year_month);
CREATE INDEX IF NOT EXISTS idx_dead_status  ON dead_inventory_monthly(status);

-- ============================================================
-- 模块 #5：nst_turnover — 库存周转率（在庫回転率 导出）
-- ============================================================
CREATE TABLE IF NOT EXISTS nst_turnover (
  id                BIGSERIAL PRIMARY KEY,
  department        TEXT,                   -- 部門
  item_code         TEXT NOT NULL,          -- アイテム
  handling_status   TEXT,                   -- 取扱区分
  cost              DOUBLE PRECISION,                   -- 原価
  avg_value         DOUBLE PRECISION,                   -- 平均値
  turnover_rate     DOUBLE PRECISION,                   -- 回転率
  avg_days_on_hand  DOUBLE PRECISION,                   -- 平均在庫日数
  ingested_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(item_code, department)
);
CREATE INDEX IF NOT EXISTS idx_nst_turnover_item  ON nst_turnover(item_code);
CREATE INDEX IF NOT EXISTS idx_nst_turnover_dept  ON nst_turnover(department);

-- ============================================================
-- 模块 #5：nst_store_sales — 店舗別売上（店舗別売上 导出）
-- ============================================================
CREATE TABLE IF NOT EXISTS nst_store_sales (
  id                BIGSERIAL PRIMARY KEY,
  fb_store          TEXT,                   -- FB_店舗
  item_code         TEXT NOT NULL,          -- アイテム
  upc               TEXT,                   -- UPCコード
  handling_status   TEXT,                   -- 取扱区分
  display_name      TEXT,                   -- 表示名
  qty_sold          DOUBLE PRECISION,                   -- 販売数量
  unit_price        DOUBLE PRECISION,                   -- 購入価格（単価）
  revenue           DOUBLE PRECISION,                   -- 総収益
  defined_cost      DOUBLE PRECISION,                   -- 定義原価
  gross_profit      DOUBLE PRECISION,                   -- 粗利
  gross_margin      DOUBLE PRECISION,                   -- 粗利率
  rank              TEXT,                   -- 商品ランク
  ingested_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(fb_store, item_code)
);
CREATE INDEX IF NOT EXISTS idx_nst_sales_item   ON nst_store_sales(item_code);
CREATE INDEX IF NOT EXISTS idx_nst_sales_store  ON nst_store_sales(fb_store);

-- ============================================================
-- 模块 #5：nst_inventory_snapshot — 多仓库库存快照（输出在庫数据）
-- 含部门硬过滤（仅 輸出事業*）
-- ============================================================
CREATE TABLE IF NOT EXISTS nst_inventory_snapshot (
  id                BIGSERIAL PRIMARY KEY,
  internal_id       TEXT NOT NULL,          -- 内部ID
  item_code         TEXT NOT NULL,          -- アイテム
  upc               TEXT,                   -- UPCコード
  display_name      TEXT,                   -- 表示名
  status            TEXT,                   -- ステータス
  bin_number        TEXT,                   -- 保管棚番号
  location          TEXT,                   -- 場所
  handling_status   TEXT,                   -- 取扱区分
  qty_on_hand       DOUBLE PRECISION,                   -- 手持合計
  qty_committed     DOUBLE PRECISION,                   -- 確保済合計
  qty_backorder     DOUBLE PRECISION,                   -- バック・オーダー合計
  std_cost          DOUBLE PRECISION,                   -- アイテム定義原価
  total_amount      DOUBLE PRECISION,                   -- 合計金額
  avg_cost          DOUBLE PRECISION,                   -- 平均原価合計
  owner             TEXT,                   -- 商品担当者
  department        TEXT,                   -- 部門
  ingested_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(internal_id, location, bin_number)
);
CREATE INDEX IF NOT EXISTS idx_nst_inv_internal  ON nst_inventory_snapshot(internal_id);
CREATE INDEX IF NOT EXISTS idx_nst_inv_item      ON nst_inventory_snapshot(item_code);
CREATE INDEX IF NOT EXISTS idx_nst_inv_loc       ON nst_inventory_snapshot(location);
CREATE INDEX IF NOT EXISTS idx_nst_inv_dept      ON nst_inventory_snapshot(department);

-- ============================================================
-- 模块 #2/#5：supplier_cost — 供应商成本管理（仕入先管理リスト）
-- ============================================================
CREATE TABLE IF NOT EXISTS supplier_cost (
  jan TEXT NOT NULL,
  supplier_name TEXT NOT NULL,
  cost_class TEXT,                    -- 'AB' or 'C'
  unit_cost REAL,
  currency TEXT DEFAULT 'PHP',
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (jan, supplier_name)
);
CREATE INDEX IF NOT EXISTS idx_supplier_cost_jan       ON supplier_cost(jan);
CREATE INDEX IF NOT EXISTS idx_supplier_cost_supplier  ON supplier_cost(supplier_name);
CREATE INDEX IF NOT EXISTS idx_supplier_cost_class     ON supplier_cost(cost_class);

-- ============================================================
-- 模块 #2：supply_cycle — 商品进货周期（AB 商品进货周期 sheet）
-- 关键字段用于 模块 #2 健康度计算（3 桶分类）
-- ============================================================
CREATE TABLE IF NOT EXISTS supply_cycle (
  jan TEXT PRIMARY KEY,
  lead_time_days INTEGER,
  bucket TEXT,                        -- 'short' / 'normal' / 'long'
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_supply_cycle_bucket     ON supply_cycle(bucket);

-- ============================================================
-- 模块 #2/#5：supplier_jan_list — 供应商商品清单（各供应商 sheet）
-- 从 NEW WIND, 中央物産, 菅野, Maple 等 sheet 提取
-- ============================================================
CREATE TABLE IF NOT EXISTS supplier_jan_list (
  jan TEXT NOT NULL,
  supplier_name TEXT NOT NULL,
  status TEXT,                        -- 供应商内商品状态（可选）
  ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (jan, supplier_name)
);
CREATE INDEX IF NOT EXISTS idx_supplier_jan_list_jan        ON supplier_jan_list(jan);
CREATE INDEX IF NOT EXISTS idx_supplier_jan_list_supplier   ON supplier_jan_list(supplier_name);

-- ============================================================
-- 模块 #25：supplier_quote — 仕入先報価単（完整版）
-- 来源: 仕入先管理リスト.xlsx 的 28 个仕入先 sheet
-- zone: JD_DIRECT(1) / BENTEN_TRANSIT(2,+3%) / EMERGENCY(3) / PREPAID(4) / OTHER(9)
-- ============================================================
CREATE TABLE IF NOT EXISTS supplier_quote (
  supplier_name     TEXT NOT NULL,
  jan               TEXT NOT NULL,
  display_name      TEXT,
  unit_price        INTEGER,
  lot_size          INTEGER DEFAULT 1,
  case_qty          INTEGER,
  min_order_amount  INTEGER,
  order_condition   TEXT,
  lead_time_text    TEXT,
  zone              TEXT,
  zone_rank         INTEGER,
  nst_supplier_code TEXT,
  source_sheet      TEXT,
  imported_at       TEXT NOT NULL,
  PRIMARY KEY (supplier_name, jan)
);
CREATE INDEX IF NOT EXISTS idx_sq_jan       ON supplier_quote(jan);
CREATE INDEX IF NOT EXISTS idx_sq_supplier  ON supplier_quote(supplier_name);
CREATE INDEX IF NOT EXISTS idx_sq_zone      ON supplier_quote(zone);

-- ============================================================
-- 模块 #2：inventory_health — 库存健康度指标
-- ============================================================
CREATE TABLE IF NOT EXISTS stock_sales_ratio_monthly (
  sku               TEXT NOT NULL,
  year_month        TEXT NOT NULL,
  end_inventory     DOUBLE PRECISION,                   -- 月末在库（手持合計 聚合）
  monthly_sales     DOUBLE PRECISION,                   -- 月销量（販売数量 聚合）
  ratio_months      DOUBLE PRECISION,                   -- end_inventory / monthly_sales
  calculated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, year_month)
);
CREATE INDEX IF NOT EXISTS idx_stock_sales_ratio_sku     ON stock_sales_ratio_monthly(sku);
CREATE INDEX IF NOT EXISTS idx_stock_sales_ratio_month   ON stock_sales_ratio_monthly(year_month);

CREATE TABLE IF NOT EXISTS cross_ratio_monthly (
  sku               TEXT NOT NULL,
  year_month        TEXT NOT NULL,
  gross_margin      DOUBLE PRECISION,                   -- 粗利率
  turnover          DOUBLE PRECISION,                   -- 回転率
  cross_ratio       DOUBLE PRECISION,                   -- gross_margin * turnover
  calculated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, year_month)
);
CREATE INDEX IF NOT EXISTS idx_cross_ratio_sku          ON cross_ratio_monthly(sku);
CREATE INDEX IF NOT EXISTS idx_cross_ratio_month        ON cross_ratio_monthly(year_month);

CREATE TABLE IF NOT EXISTS health_grade_monthly (
  sku               TEXT NOT NULL,
  year_month        TEXT NOT NULL,
  bucket            TEXT,                   -- 'short' / 'normal' / 'long'
  threshold         DOUBLE PRECISION,                   -- THRESHOLD[bucket]
  cross_ratio       DOUBLE PRECISION,
  grade             TEXT,                   -- '🟢 优秀' / '🟡 健康' / '🟠 注意' / '🔴 死钱'
  dead_money_jpy    DOUBLE PRECISION,                   -- 仅 '🔴 死钱' 时填（库存数量 × 定义原价）
  calculated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, year_month)
);
CREATE INDEX IF NOT EXISTS idx_health_grade_sku         ON health_grade_monthly(sku);
CREATE INDEX IF NOT EXISTS idx_health_grade_month       ON health_grade_monthly(year_month);
CREATE INDEX IF NOT EXISTS idx_health_grade_grade       ON health_grade_monthly(grade);

-- ============================================================
-- 模块 #1：rank_history — 等级变化历史（T-016）
-- ============================================================
CREATE TABLE IF NOT EXISTS rank_history (
  sku               TEXT NOT NULL,
  quarter           TEXT NOT NULL,          -- e.g. '2026-Q1'
  old_rank          TEXT,
  new_rank          TEXT,
  changed_by        TEXT,                   -- e.g. 'system' / 'admin'
  changed_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, quarter)
);
CREATE INDEX IF NOT EXISTS idx_rank_history_sku      ON rank_history(sku);
CREATE INDEX IF NOT EXISTS idx_rank_history_quarter  ON rank_history(quarter);

-- ============================================================
-- 模块 #3：discontinue_alerts — 停产监控警报
-- ============================================================
CREATE TABLE IF NOT EXISTS discontinue_alerts (
  jan              TEXT NOT NULL,
  sku              TEXT,
  source           TEXT NOT NULL,          -- 'netdeoroshi' / 'supplier_list'
  signal_type      TEXT NOT NULL,          -- '販売終了' / '削除' / 'NEW' 等
  detected_at      TEXT NOT NULL,          -- ISO 8601
  acknowledged_by  TEXT,
  acknowledged_at  TEXT,
  action           TEXT,                   -- 确认后的处置措施
  PRIMARY KEY (jan, source, signal_type, detected_at)
);
CREATE INDEX IF NOT EXISTS idx_discontinue_jan        ON discontinue_alerts(jan);
CREATE INDEX IF NOT EXISTS idx_discontinue_source     ON discontinue_alerts(source);
CREATE INDEX IF NOT EXISTS idx_discontinue_detected   ON discontinue_alerts(detected_at);
CREATE INDEX IF NOT EXISTS idx_discontinue_acked      ON discontinue_alerts(acknowledged_at);

-- Schema 版本表（用于将来迁移）
-- ============================================================
CREATE TABLE IF NOT EXISTS _schema_version (
  version          INTEGER PRIMARY KEY,
  applied_at       TEXT NOT NULL
);

-- ============================================================
-- 模块 #4：shopee_payouts — Shopee 拨款主档
-- ============================================================
CREATE TABLE IF NOT EXISTS shopee_payouts (
  payout_id        TEXT PRIMARY KEY,
  seller_account   TEXT,
  channel          TEXT,
  payout_date      DATE,
  total_payout     DOUBLE PRECISION,
  currency         TEXT,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shopee_payouts_seller ON shopee_payouts(seller_account);
CREATE INDEX IF NOT EXISTS idx_shopee_payouts_date   ON shopee_payouts(payout_date);

-- ============================================================
-- 模块 #4：shopee_fees — Shopee 服务费明细
-- ============================================================
CREATE TABLE IF NOT EXISTS shopee_fees (
  fee_id           BIGSERIAL PRIMARY KEY,
  order_no         TEXT,
  fee_type         TEXT,
  amount           DOUBLE PRECISION,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shopee_fees_order     ON shopee_fees(order_no);
CREATE INDEX IF NOT EXISTS idx_shopee_fees_type      ON shopee_fees(fee_type);

-- ============================================================
-- 模块 #4：shopee_adjustments — Shopee 调整明细
-- ============================================================
CREATE TABLE IF NOT EXISTS shopee_adjustments (
  adjustment_id    BIGSERIAL PRIMARY KEY,
  seller_account   TEXT,
  payout_id        TEXT,
  payout_date      DATE,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shopee_adj_seller     ON shopee_adjustments(seller_account);
CREATE INDEX IF NOT EXISTS idx_shopee_adj_payout     ON shopee_adjustments(payout_id);

-- ============================================================
-- 模块 #4：shopee_orders — Shopee 订单明细
-- ============================================================
CREATE TABLE IF NOT EXISTS shopee_orders (
  order_no         TEXT,
  sku_or_jan       TEXT,
  qty              DOUBLE PRECISION,
  unit_price       DOUBLE PRECISION,
  payment_amount   DOUBLE PRECISION,
  currency         TEXT,
  platform         TEXT,
  shop_name        TEXT,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (order_no, sku_or_jan)
);
CREATE INDEX IF NOT EXISTS idx_shopee_orders_sku     ON shopee_orders(sku_or_jan);
CREATE INDEX IF NOT EXISTS idx_shopee_orders_order   ON shopee_orders(order_no);
CREATE INDEX IF NOT EXISTS idx_shopee_orders_date    ON shopee_orders(ingested_at);

-- ============================================================
-- 模块 #2 v3：operation_advice_monthly — 运营调整建议（毛利×周转双轴）
-- ============================================================
CREATE TABLE IF NOT EXISTS operation_advice_monthly (
  sku              TEXT,
  year_month       TEXT,
  rank             TEXT,
  margin_pct       DOUBLE PRECISION,
  monthly_turnover REAL,
  cross_ratio      DOUBLE PRECISION,
  margin_lv        TEXT,         -- 低/中/高
  turnover_lv      TEXT,         -- 低/中/高
  advice           TEXT,         -- 🔥重点提价 / 🔥重点降价 / ⬆️/⚠️/⚫/✅
  reason           TEXT,
  inventory_value  DOUBLE PRECISION,
  calculated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, year_month)
);
CREATE INDEX IF NOT EXISTS idx_op_advice_advice   ON operation_advice_monthly(advice);
CREATE INDEX IF NOT EXISTS idx_op_advice_rank     ON operation_advice_monthly(rank);
CREATE INDEX IF NOT EXISTS idx_op_advice_value    ON operation_advice_monthly(inventory_value DESC);

-- ============================================================
-- 订货模块 6 张新表（v3.1 from order-management-app）
-- ============================================================

-- 进货价目（jan × supplier × order_lot → price）
CREATE TABLE IF NOT EXISTS purchase_data (
  id            BIGSERIAL PRIMARY KEY,
  jan           TEXT NOT NULL,
  supplier      TEXT,
  order_lot     INTEGER,
  price         DOUBLE PRECISION,
  source_file   TEXT,
  imported_at   TEXT NOT NULL,
  UNIQUE(jan, supplier, order_lot)
);
CREATE INDEX IF NOT EXISTS idx_purchase_data_jan      ON purchase_data(jan);
CREATE INDEX IF NOT EXISTS idx_purchase_data_supplier ON purchase_data(supplier);

-- 订货历史（已下达过的订货单）
CREATE TABLE IF NOT EXISTS purchase_history (
  id            BIGSERIAL PRIMARY KEY,
  jan           TEXT NOT NULL,
  quantity      INTEGER NOT NULL,
  memo          TEXT,
  order_date    TEXT,
  order_id      TEXT,
  source_file   TEXT,
  imported_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purchase_history_jan       ON purchase_history(jan);
CREATE INDEX IF NOT EXISTS idx_purchase_history_orderdate ON purchase_history(order_date);
CREATE INDEX IF NOT EXISTS idx_purchase_history_orderid   ON purchase_history(order_id);

-- JD 仓库库存（product_code = jan）
CREATE TABLE IF NOT EXISTS warehouse_stock (
  id              BIGSERIAL PRIMARY KEY,
  product_code    TEXT NOT NULL,
  jan             TEXT,
  stock_available INTEGER NOT NULL DEFAULT 0,
  snapshot_at     TEXT,
  source_file     TEXT,
  imported_at     TEXT NOT NULL,
  UNIQUE(product_code, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_warehouse_stock_jan ON warehouse_stock(jan);

-- 弁天仓库库存
CREATE TABLE IF NOT EXISTS benten_stock (
  id            BIGSERIAL PRIMARY KEY,
  jan           TEXT NOT NULL,
  stock         INTEGER NOT NULL DEFAULT 0,
  snapshot_at   TEXT,
  source_file   TEXT,
  imported_at   TEXT NOT NULL,
  UNIQUE(jan, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_benten_stock_jan ON benten_stock(jan);

-- 保质期管理（来自 Lark 同步）
CREATE TABLE IF NOT EXISTS item_expiry (
  jan           TEXT PRIMARY KEY,
  name          TEXT,
  expiry_1      TEXT,
  expiry_2      TEXT,
  expiry_3      TEXT,
  expiry_4      TEXT,
  expiry_5      TEXT,
  expiry_min    TEXT,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_expiry_min ON item_expiry(expiry_min);

-- 每日店铺销售明细
CREATE TABLE IF NOT EXISTS store_profit_daily_lines (
  id            BIGSERIAL PRIMARY KEY,
  report_date   TEXT NOT NULL,
  line_type     TEXT NOT NULL,    -- detail / 合計 / 総計 等
  store         TEXT NOT NULL,
  item          TEXT,
  item_name     TEXT,
  qty           INTEGER DEFAULT 0,
  revenue       INTEGER DEFAULT 0,
  defined_cost  INTEGER DEFAULT 0,
  gross_profit  INTEGER DEFAULT 0,
  source_file   TEXT,
  imported_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spdl_date  ON store_profit_daily_lines(report_date);
CREATE INDEX IF NOT EXISTS idx_spdl_store ON store_profit_daily_lines(store);
CREATE INDEX IF NOT EXISTS idx_spdl_type  ON store_profit_daily_lines(line_type);

-- ============================================================
-- 定义原价变更历史 · 用于 SKU 级波动图
-- ============================================================
CREATE TABLE IF NOT EXISTS std_cost_history (
  id              BIGSERIAL PRIMARY KEY,
  internal_id     TEXT NOT NULL,
  item_code       TEXT,
  display_name    TEXT,
  std_cost_old    DOUBLE PRECISION,
  std_cost_new    DOUBLE PRECISION NOT NULL,
  diff            DOUBLE PRECISION,
  diff_pct        DOUBLE PRECISION,
  changed_at      TEXT NOT NULL,
  changed_by      TEXT,
  source          TEXT,    -- avg-driven / manual-override / batch
  notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_stdcost_hist_iid     ON std_cost_history(internal_id);
CREATE INDEX IF NOT EXISTS idx_stdcost_hist_changed ON std_cost_history(changed_at);

-- (移除: 之前曾追加重名的 sales 表(jan/quantity_sold), 跟 line 50 主表 sales(internal_id/sold_at) 冲突
--  导致 CREATE TABLE IF NOT EXISTS 跳过 + 后续 idx_sales_jan 索引失败 → executescript 崩溃 → 全 page 挂掉
--  page 04 已经改回用 sales_line 聚合, 不再需要这张表)

-- ============================================================
-- 店舗別粗利月度（对齐原 store_profit_lines 表）
-- ============================================================
CREATE TABLE IF NOT EXISTS store_profit_lines (
  id              BIGSERIAL PRIMARY KEY,
  report_period   TEXT NOT NULL,        -- e.g. "2026-04"
  line_type       TEXT NOT NULL,        -- detail / 合計 / 総計
  store           TEXT,
  qty             INTEGER DEFAULT 0,
  revenue         INTEGER DEFAULT 0,
  defined_cost    INTEGER DEFAULT 0,
  gross_profit    INTEGER DEFAULT 0,
  original_line   TEXT,
  source_file     TEXT,
  imported_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spl_period ON store_profit_lines(report_period);
CREATE INDEX IF NOT EXISTS idx_spl_store  ON store_profit_lines(store);

-- ============================================================
-- Shopee 财务 v2 · 数据源对齐 Boss 提供的两份原表
-- ============================================================

-- 订单导出 (订单导出-*.xlsx Sheet0, 8 列)
-- A=支付币种 B=单价 C=发货数量 D=本地SKU E=支付金额 F=平台 G=订单号 H=店铺
CREATE TABLE IF NOT EXISTS shopee_orders_raw (
  id              BIGSERIAL PRIMARY KEY,
  currency        TEXT,                  -- 支付币种
  unit_price      TEXT,                  -- 单价 (多 SKU 时 \n 分隔)
  ship_qty        TEXT,                  -- 发货数量 (多 SKU 时 \n 分隔)
  local_sku       TEXT,                  -- 本地SKU = jan (多 SKU 时 \n 分隔)
  payment_amount  DOUBLE PRECISION,                  -- 支付金额
  platform        TEXT,                  -- 平台 (Shopee 等)
  order_no        TEXT NOT NULL,         -- 订单号
  shop_name       TEXT,                  -- 店铺
  source_file     TEXT,
  imported_at     TEXT NOT NULL,
  UNIQUE(order_no)
);
CREATE INDEX IF NOT EXISTS idx_shopee_orders_raw_orderno ON shopee_orders_raw(order_no);
CREATE INDEX IF NOT EXISTS idx_shopee_orders_raw_shop    ON shopee_orders_raw(shop_name);

-- 拨款明细 (ph.mtkshop.ph.income.*.xlsx Income sheet, R6 表头, 46 列)
CREATE TABLE IF NOT EXISTS shopee_income_lines (
  id                       BIGSERIAL PRIMARY KEY,
  seq                      INTEGER,
  order_no                 TEXT NOT NULL,
  refund_id                TEXT,
  buyer_account            TEXT,
  order_created_at         TEXT,
  payment_method           TEXT,
  hot_listing              TEXT,
  payment_method_detail    TEXT,
  installment_plan         TEXT,
  installment_rate         TEXT,
  payout_completed_at      TEXT,
  gross_price              DOUBLE PRECISION,
  product_discount         DOUBLE PRECISION,
  refund_amount            DOUBLE PRECISION,
  shopee_rebate            DOUBLE PRECISION,
  seller_voucher           DOUBLE PRECISION,
  seller_voucher_jv        DOUBLE PRECISION,
  seller_shopee_coin       DOUBLE PRECISION,
  seller_shopee_coin_jv    DOUBLE PRECISION,
  buyer_shipping           DOUBLE PRECISION,
  shopee_shipping_subsidy  DOUBLE PRECISION,
  seller_shipping          DOUBLE PRECISION,
  return_shipping          DOUBLE PRECISION,
  return_to_seller_ship    DOUBLE PRECISION,
  shipping_insurance_save  DOUBLE PRECISION,
  affiliate_commission     DOUBLE PRECISION,
  commission               DOUBLE PRECISION,
  fbs_overseas_fail        DOUBLE PRECISION,
  fbs_overseas_return      DOUBLE PRECISION,
  service_fee              DOUBLE PRECISION,
  shipping_insurance_fee   DOUBLE PRECISION,
  transaction_fee          DOUBLE PRECISION,
  fbs_fee                  DOUBLE PRECISION,
  payout_amount            DOUBLE PRECISION,
  promo_code               TEXT,
  loss_compensation        DOUBLE PRECISION,
  actual_weight            DOUBLE PRECISION,
  seller_shipping_promo    DOUBLE PRECISION,
  logistics_carrier        TEXT,
  logistics_name           TEXT,
  refund_cash              DOUBLE PRECISION,
  prorated_shopee_coin     DOUBLE PRECISION,
  prorated_shopee_voucher  DOUBLE PRECISION,
  prorated_bank_promo      DOUBLE PRECISION,
  prorated_payment_promo   DOUBLE PRECISION,
  seller_account           TEXT,
  payout_date              TEXT,
  source_file              TEXT,
  imported_at              TEXT NOT NULL,
  UNIQUE(order_no, refund_id)
);
CREATE INDEX IF NOT EXISTS idx_shopee_income_orderno     ON shopee_income_lines(order_no);
CREATE INDEX IF NOT EXISTS idx_shopee_income_payoutdate  ON shopee_income_lines(payout_date);
CREATE INDEX IF NOT EXISTS idx_shopee_income_seller      ON shopee_income_lines(seller_account);

-- ============================================================
-- nst_item_summary · NetSuite アイテム概要 (アイテム.xls 8 列)
-- A=名前 B=UPCコード C=表示名 D=取扱区分 E=アイテム定義原価
-- F=利用可能 G=利用可能な保管棚手持 H=平均原価
-- 用途: page 03 定義原価編集 直接拉 H 列「平均原価」做 ceil 判定
-- ============================================================
CREATE TABLE IF NOT EXISTS nst_item_summary (
  id                BIGSERIAL PRIMARY KEY,
  item_code         TEXT NOT NULL,        -- A 列 名前
  upc               TEXT,                  -- B 列 UPCコード
  display_name      TEXT,                  -- C 列 表示名
  handling_status   TEXT,                  -- D 列 取扱区分
  std_cost          DOUBLE PRECISION,                  -- E 列 アイテム定義原価 (当前)
  available         DOUBLE PRECISION,                  -- F 列 利用可能
  available_on_hand REAL,                  -- G 列 利用可能な保管棚手持
  avg_cost          DOUBLE PRECISION,                  -- H 列 平均原価 (新定义原価源)
  source_file       TEXT,
  imported_at       TEXT NOT NULL,
  UNIQUE(item_code)
);
CREATE INDEX IF NOT EXISTS idx_nst_item_summary_upc      ON nst_item_summary(upc);
CREATE INDEX IF NOT EXISTS idx_nst_item_summary_avg      ON nst_item_summary(avg_cost);
CREATE INDEX IF NOT EXISTS idx_nst_item_summary_handling ON nst_item_summary(handling_status);

-- ============================================================
-- automation_runs · CMS → N8N / 影刀 自动化任务跟踪表
-- ============================================================
CREATE TABLE IF NOT EXISTS automation_runs (
  run_id           TEXT PRIMARY KEY,
  module           TEXT NOT NULL,
  payload          TEXT,
  status           TEXT NOT NULL,
  summary          TEXT,
  triggered_by     TEXT,
  triggered_at     TEXT NOT NULL,
  completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_module    ON automation_runs(module);
CREATE INDEX IF NOT EXISTS idx_automation_runs_status    ON automation_runs(status);
CREATE INDEX IF NOT EXISTS idx_automation_runs_triggered ON automation_runs(triggered_at);

-- ============================================================
-- v2 数据模型（Phase 3.1, 2026-05-09）· 以 JAN 为核心
-- 详细注释见 data_warehouse/db/schema.sql 末尾
-- ============================================================

CREATE TABLE IF NOT EXISTS item_v2 (
  jan              TEXT PRIMARY KEY,
  item_code        TEXT,
  internal_id      TEXT,
  upc              TEXT,
  display_name     TEXT,
  maker            TEXT,
  rank             TEXT,
  handling_status  TEXT,
  department       TEXT,
  owner            TEXT,
  std_cost         DOUBLE PRECISION,
  avg_cost         DOUBLE PRECISION,
  actual_cost      DOUBLE PRECISION,
  min_cost         DOUBLE PRECISION,
  case_qty         INTEGER,
  order_lot        INTEGER,
  weight           DOUBLE PRECISION,
  supplier_default TEXT,
  supply_cycle_days INTEGER,
  bucket           TEXT,
  on_hand_total       DOUBLE PRECISION,
  on_order_total      DOUBLE PRECISION,
  qty_committed_total DOUBLE PRECISION,
  total_amount        DOUBLE PRECISION,
  source_priority  TEXT,
  imported_at      TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_v2_code     ON item_v2(item_code);
CREATE INDEX IF NOT EXISTS idx_item_v2_internal ON item_v2(internal_id);
CREATE INDEX IF NOT EXISTS idx_item_v2_maker    ON item_v2(maker);
CREATE INDEX IF NOT EXISTS idx_item_v2_rank     ON item_v2(rank);
CREATE INDEX IF NOT EXISTS idx_item_v2_status   ON item_v2(handling_status);

CREATE TABLE IF NOT EXISTS item_purchase_history (
  id           BIGSERIAL PRIMARY KEY,
  jan          TEXT NOT NULL,
  po_number    TEXT,
  supplier     TEXT,
  qty          INTEGER,
  unit_cost    DOUBLE PRECISION,
  total_cost   DOUBLE PRECISION,
  ordered_at   TEXT,
  received_at  TEXT,
  source       TEXT,
  imported_at  TEXT,
  UNIQUE(po_number, jan, source)
);
CREATE INDEX IF NOT EXISTS idx_iph_jan       ON item_purchase_history(jan);
CREATE INDEX IF NOT EXISTS idx_iph_supplier  ON item_purchase_history(supplier);
CREATE INDEX IF NOT EXISTS idx_iph_ordered   ON item_purchase_history(ordered_at);

CREATE TABLE IF NOT EXISTS item_sales_history (
  id            BIGSERIAL PRIMARY KEY,
  jan           TEXT NOT NULL,
  period_start  TEXT,
  period_end    TEXT,
  channel       TEXT,
  qty_sold      DOUBLE PRECISION,
  revenue       DOUBLE PRECISION,
  cost          DOUBLE PRECISION,
  gross_profit  DOUBLE PRECISION,
  gross_margin  DOUBLE PRECISION,
  source        TEXT,
  imported_at   TEXT,
  UNIQUE(jan, period_start, period_end, channel, source)
);
CREATE INDEX IF NOT EXISTS idx_ish_jan      ON item_sales_history(jan);
CREATE INDEX IF NOT EXISTS idx_ish_channel  ON item_sales_history(channel);
CREATE INDEX IF NOT EXISTS idx_ish_period   ON item_sales_history(period_start);

CREATE TABLE IF NOT EXISTS item_inventory_snapshot_v2 (
  id            BIGSERIAL PRIMARY KEY,
  jan           TEXT NOT NULL,
  item_code     TEXT,
  internal_id   TEXT,
  display_name  TEXT,
  location      TEXT,
  bin_number    TEXT,
  snapshot_at   TEXT,
  qty_on_hand   DOUBLE PRECISION,
  qty_committed DOUBLE PRECISION,
  qty_backorder DOUBLE PRECISION,
  qty_on_order  DOUBLE PRECISION,
  qty_waiting   DOUBLE PRECISION,
  qty_in_transit DOUBLE PRECISION,
  std_cost      DOUBLE PRECISION,
  avg_cost      DOUBLE PRECISION,
  total_amount  DOUBLE PRECISION,
  handling_status TEXT,
  status        TEXT,
  owner         TEXT,
  department    TEXT,
  imported_at   TEXT,
  UNIQUE(jan, location, bin_number, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_iis2_jan      ON item_inventory_snapshot_v2(jan);
CREATE INDEX IF NOT EXISTS idx_iis2_location ON item_inventory_snapshot_v2(location);
CREATE INDEX IF NOT EXISTS idx_iis2_snapshot ON item_inventory_snapshot_v2(snapshot_at);

CREATE TABLE IF NOT EXISTS item_cost_history (
  id           BIGSERIAL PRIMARY KEY,
  jan          TEXT NOT NULL,
  std_cost     DOUBLE PRECISION,
  avg_cost     DOUBLE PRECISION,
  changed_by   TEXT,
  changed_at   TEXT NOT NULL,
  reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ich_jan ON item_cost_history(jan);
CREATE INDEX IF NOT EXISTS idx_ich_changed ON item_cost_history(changed_at);

CREATE TABLE IF NOT EXISTS market_segment (
  market_id    TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  currency     TEXT,
  active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS shop (
  shop_id      TEXT PRIMARY KEY,
  market_id    TEXT NOT NULL,
  platform     TEXT NOT NULL,
  display_name TEXT NOT NULL,
  currency     TEXT,
  owner        TEXT,
  active       INTEGER DEFAULT 1,
  created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_shop_market   ON shop(market_id);
CREATE INDEX IF NOT EXISTS idx_shop_platform ON shop(platform);

CREATE TABLE IF NOT EXISTS shop_sales (
  id            BIGSERIAL PRIMARY KEY,
  shop_id       TEXT NOT NULL,
  jan           TEXT NOT NULL,
  granularity   TEXT NOT NULL DEFAULT 'monthly',
  period_start  TEXT NOT NULL,
  period_end    TEXT NOT NULL,
  qty_sold      DOUBLE PRECISION,
  unit_price    DOUBLE PRECISION,
  revenue       DOUBLE PRECISION,
  revenue_jpy   DOUBLE PRECISION,
  cost          DOUBLE PRECISION,
  gross_profit  DOUBLE PRECISION,
  gross_margin  DOUBLE PRECISION,
  rank          TEXT,
  source        TEXT,
  imported_at   TEXT,
  UNIQUE(shop_id, jan, granularity, period_start, period_end, source)
);
CREATE INDEX IF NOT EXISTS idx_ss_shop        ON shop_sales(shop_id);
CREATE INDEX IF NOT EXISTS idx_ss_jan         ON shop_sales(jan);
CREATE INDEX IF NOT EXISTS idx_ss_granularity ON shop_sales(granularity);
CREATE INDEX IF NOT EXISTS idx_ss_period ON shop_sales(period_start);

CREATE TABLE IF NOT EXISTS shop_monthly (
  shop_id          TEXT NOT NULL,
  year_month       TEXT NOT NULL,
  gmv              DOUBLE PRECISION,
  profit           DOUBLE PRECISION,
  margin_rate      DOUBLE PRECISION,
  profit_contrib   DOUBLE PRECISION,
  deduction_total  DOUBLE PRECISION,
  order_count      INTEGER,
  store_rating     DOUBLE PRECISION,
  online_products  INTEGER,
  imported_at      TEXT,
  PRIMARY KEY(shop_id, year_month)
);
CREATE INDEX IF NOT EXISTS idx_sm_ym ON shop_monthly(year_month);

CREATE TABLE IF NOT EXISTS _v2_migration_runs (
  id            BIGSERIAL PRIMARY KEY,
  step          TEXT NOT NULL,
  source_table  TEXT,
  rows_read     INTEGER,
  rows_written  INTEGER,
  errors        INTEGER,
  ran_at        TEXT NOT NULL,
  notes         TEXT
);

-- v2 补充表 · Phase 3.6
CREATE TABLE IF NOT EXISTS item_supplier_link (
  jan           TEXT NOT NULL,
  supplier_name TEXT NOT NULL,
  cost_class    TEXT,
  unit_cost     DOUBLE PRECISION,
  currency      TEXT,
  status        TEXT,
  source        TEXT,
  imported_at   TEXT,
  PRIMARY KEY (jan, supplier_name)
);
CREATE INDEX IF NOT EXISTS idx_isl_jan      ON item_supplier_link(jan);
CREATE INDEX IF NOT EXISTS idx_isl_supplier ON item_supplier_link(supplier_name);
CREATE INDEX IF NOT EXISTS idx_isl_class    ON item_supplier_link(cost_class);

-- ============================================================
-- Phase 4 · 桥接 VIEW（旧表名透传到 v2）
-- 让 page / module 的 SELECT 不用改也能读到数据
-- ============================================================
CREATE OR REPLACE VIEW v_inventory_snapshot AS
SELECT
  internal_id, item_code, jan AS upc, display_name,
  status, bin_number, location, handling_status,
  qty_on_hand, qty_committed, qty_backorder,
  std_cost, total_amount, avg_cost,
  owner, department, snapshot_at,
  ''::TEXT AS source_file, imported_at
FROM item_inventory_snapshot_v2;

CREATE OR REPLACE VIEW v_nst_inventory_snapshot AS
SELECT * FROM v_inventory_snapshot;

CREATE OR REPLACE VIEW v_sales_line AS
SELECT
  id, shop_id AS store,
  jan AS item_code, jan AS upc,
  ''::TEXT AS display_name, ''::TEXT AS handling_status, ''::TEXT AS maker,
  rank, qty_sold, unit_price AS unit_purchase_price,
  revenue, cost AS defined_cost,
  gross_profit, gross_margin,
  period_start, period_end, source,
  ''::TEXT AS source_file, imported_at
FROM shop_sales;

CREATE OR REPLACE VIEW v_nst_store_sales AS
SELECT
  id, shop_id AS fb_store,
  jan AS item_code, jan AS upc,
  ''::TEXT AS handling_status, ''::TEXT AS display_name,
  qty_sold, unit_price, revenue,
  cost AS defined_cost, gross_profit, gross_margin,
  rank, imported_at AS ingested_at
FROM shop_sales;

CREATE OR REPLACE VIEW v_nst_item_summary AS
SELECT
  jan AS upc, item_code, display_name, handling_status,
  std_cost,
  NULL::DOUBLE PRECISION AS available, NULL::DOUBLE PRECISION AS available_on_hand,
  avg_cost,
  ''::TEXT AS source_file, imported_at
FROM item_v2;

CREATE OR REPLACE VIEW v_item_master_netsuite AS
SELECT
  internal_id, jan AS upc, display_name,
  avg_cost, std_cost,
  NULL::DOUBLE PRECISION AS last_purchase,
  on_hand_total AS on_hand,
  NULL::DOUBLE PRECISION AS available,
  on_order_total AS on_order,
  department, rank,
  NULL::TEXT AS sku_id, NULL::TEXT AS created_at,
  maker,
  ''::TEXT AS source_file, imported_at
FROM item_v2;

CREATE OR REPLACE VIEW v_item_master AS
SELECT
  jan, item_code, rank, maker, display_name, handling_status,
  on_hand_total AS on_hand,
  on_order_total AS on_order,
  actual_cost, min_cost,
  case_qty, order_lot, weight,
  ''::TEXT AS source_file, imported_at
FROM item_v2;

-- ============================================================
-- ECMS 发货（2026-08-28）· 与 shared/schema.sql 同步，字段全 TEXT
-- ============================================================
CREATE TABLE IF NOT EXISTS ecms_shipment (
  reference_code    TEXT NOT NULL,
  env               TEXT NOT NULL,
  shipment_id       TEXT,
  tracking_no       TEXT,
  status            TEXT NOT NULL,
  receiver_name     TEXT,
  receiver_country  TEXT,
  label_url         TEXT,
  request_json      TEXT,
  response_json     TEXT,
  created_by        TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT,
  PRIMARY KEY (reference_code, env)         -- UAT 与 PRO 同号是两条记录，不能互相覆盖
);
CREATE INDEX IF NOT EXISTS idx_ecms_ship_tracking ON ecms_shipment(tracking_no);
CREATE INDEX IF NOT EXISTS idx_ecms_ship_status   ON ecms_shipment(status);

CREATE TABLE IF NOT EXISTS ecms_tracking_event (
  tracking_no   TEXT NOT NULL,
  event_code    TEXT NOT NULL,
  reason_code   TEXT,
  event_time    TEXT NOT NULL,
  description   TEXT,
  remark        TEXT,
  location      TEXT,
  fetched_at    TEXT NOT NULL,
  PRIMARY KEY (tracking_no, event_code, event_time)
);
CREATE INDEX IF NOT EXISTS idx_ecms_evt_tracking ON ecms_tracking_event(tracking_no);

-- ============================================================
-- Coupang → ECMS 发货（2026-08-30）· shared/schema.sql と同期
-- queue は PII を持つ。7 日で自動削除
-- ============================================================
CREATE TABLE IF NOT EXISTS coupang_shipment_queue (
  order_id          TEXT NOT NULL,
  shipment_box_id   TEXT NOT NULL,
  ordered_at        TEXT,
  coupang_status    TEXT,
  receiver_name     TEXT,
  receiver_phone    TEXT,
  receiver_postcode TEXT,
  receiver_addr     TEXT,
  addr_sido         TEXT,
  addr_sigungu      TEXT,
  addr_detail       TEXT,
  pccc              TEXT,
  pccc_kind         TEXT,
  items_json        TEXT,
  total_krw         DOUBLE PRECISION,
  total_usd         DOUBLE PRECISION,
  weight_kg         DOUBLE PRECISION,
  fx_rate           DOUBLE PRECISION,
  ecms_status       TEXT NOT NULL,
  ecms_reference    TEXT,
  note              TEXT,
  pulled_at         TEXT NOT NULL,
  updated_at        TEXT,
  PRIMARY KEY (order_id, shipment_box_id)
);
CREATE INDEX IF NOT EXISTS idx_cpq_status ON coupang_shipment_queue(ecms_status);
CREATE INDEX IF NOT EXISTS idx_cpq_pulled ON coupang_shipment_queue(pulled_at);

CREATE TABLE IF NOT EXISTS coupang_product_info (
  jan            TEXT PRIMARY KEY,
  name_en        TEXT,
  hscode         TEXT,
  weight_g       DOUBLE PRECISION,
  product_id     TEXT,
  option_id      TEXT,
  url            TEXT,
  updated_at     TEXT
);
