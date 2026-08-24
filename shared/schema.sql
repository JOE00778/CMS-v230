-- ── item_v2 / shop_sales とその legacy view は 2026-08-21 に廃止 ────────
-- 単一事実源は nst.item_master_raw / nst.sales_monthly / nst.inventory_snapshot。
-- 本番の実表を DROP しても、ここに定義が残っていると cms_streamlit の
-- 再起動 → 初回ページロード → init_db() で空表として復活してしまうため、
-- 定義ごと削除した（2026-08-21 に実際に復活したのを確認）。
-- 対象: item_v2, shop_sales, v_item_master, v_item_master_netsuite,
--       v_nst_item_summary, v_nst_store_sales, v_sales_line
-- ──────────────────────────────────────────────────────────────────
-- 商品信息管理平台 · 数据仓库 schema
-- 6 张主表（共享）+ 各模块自有表
-- 全表 UPSERT 兼容；时间字段使用 ISO 8601 字符串
-- 设计依据：/Users/joe/.claude/plans/tidy-yawning-pony.md

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
  weight           REAL,
  avg_cost         REAL,
  std_cost         REAL,
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
-- (废弃: sales 表为空，sales_line 替代；由 migrations.py DEPRECATED_TABLES DROP)

-- ============================================================
-- 主表 4：inventory — 库存快照（按时点）
-- ============================================================
CREATE TABLE IF NOT EXISTS inventory (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  po_number        TEXT,
  internal_id      TEXT NOT NULL REFERENCES item(internal_id),
  supplier_id      TEXT REFERENCES supplier(supplier_id),
  ordered_at       TEXT,
  received_at      TEXT,
  qty              INTEGER NOT NULL,
  unit_price       REAL NOT NULL,
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
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
  run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
  export_id        INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  internal_id     TEXT NOT NULL,
  item_code       TEXT NOT NULL,        -- アイテム
  upc             TEXT,                  -- UPCコード = JAN
  display_name    TEXT,
  status          TEXT,                  -- ステータス（通常在庫 等）
  bin_number      TEXT,                  -- 保管棚番号
  location        TEXT,                  -- 場所（仓库）
  handling_status TEXT,                  -- 取扱区分
  qty_on_hand     REAL,                  -- 手持合計
  qty_committed   REAL,                  -- 確保済合計
  qty_backorder   REAL,                  -- バック・オーダー合計
  std_cost        REAL,                  -- アイテム定義原価
  total_amount    REAL,                  -- 合計金額
  avg_cost        REAL,                  -- 平均原価合計
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
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  store               TEXT,              -- nullable（SKU-only 报表无）
  item_code           TEXT NOT NULL,     -- アイテム
  upc                 TEXT,              -- UPCコード（如有）
  display_name        TEXT,
  handling_status     TEXT,              -- 取扱区分
  maker               TEXT,              -- メーカー名（来源 ASEAN 集計専用 R7 第11列）
  rank                TEXT,              -- 商品ランク
  qty_sold            REAL,              -- 販売数量
  unit_purchase_price REAL,              -- 購入価格（単価）（仅 輸出アイテム別 带）
  revenue             REAL,              -- 総収益
  defined_cost        REAL,              -- 定義原価
  gross_profit        REAL,              -- 粗利
  gross_margin        REAL,              -- 粗利率
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
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code         TEXT NOT NULL,        -- アイテム
  description       TEXT,                  -- 説明
  cost              REAL,                  -- 原価
  avg_value         REAL,                  -- 平均値
  turnover_rate     REAL,                  -- 回転率
  avg_days_on_hand  REAL,                  -- 平均手持日数
  period_start      TEXT NOT NULL,
  period_end        TEXT NOT NULL,
  source_file       TEXT,
  imported_at       TEXT NOT NULL,
  UNIQUE(item_code, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_turnover_item  ON inventory_turnover(item_code);
CREATE INDEX IF NOT EXISTS idx_turnover_rate  ON inventory_turnover(turnover_rate);

-- ============================================================
-- 模块 #11：difficult_items — 入荷困難商品（人工录入）
-- ============================================================
CREATE TABLE IF NOT EXISTS difficult_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  item_key     TEXT NOT NULL,           -- ブランド / 商品名 / JAN 自由文本
  reason       TEXT NOT NULL,
  note         TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_diff_items_key  ON difficult_items(item_key);

CREATE TABLE IF NOT EXISTS difficult_items_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
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
  actual_cost      REAL,                   -- 実績原価
  min_cost         REAL,                   -- 最安原価
  case_qty         INTEGER,                -- ケース入数
  order_lot        INTEGER,                -- 発注ロット
  weight           REAL,                   -- 重量
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
  avg_cost         REAL,                   -- 平均原価
  std_cost         REAL,                   -- アイテム定義原価
  last_purchase    REAL,                   -- 前回購入価格
  on_hand          REAL,                   -- 手持
  available        REAL,                   -- 利用可能
  on_order         REAL,                   -- 注文済
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
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  year_month       TEXT NOT NULL,          -- 年月（YYYYMM）
  market           TEXT,                   -- 市場（US/PH 等）
  store_id         TEXT,                   -- 店铺ID
  online_products  INTEGER,                -- 在线产品数
  revenue          REAL,                   -- 营业額
  profit           REAL,                   -- 利潤
  margin_rate      REAL,                   -- 毛利率
  profit_contrib   REAL,                   -- 利潤貢献率
  store_rating     REAL,                   -- 店舗評価
  deduction_total  REAL,                   -- 扣减合計
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
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  department        TEXT,                   -- 部門
  item_code         TEXT NOT NULL,          -- アイテム
  handling_status   TEXT,                   -- 取扱区分
  cost              REAL,                   -- 原価
  avg_value         REAL,                   -- 平均値
  turnover_rate     REAL,                   -- 回転率
  avg_days_on_hand  REAL,                   -- 平均在庫日数
  ingested_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(item_code, department)
);
CREATE INDEX IF NOT EXISTS idx_nst_turnover_item  ON nst_turnover(item_code);
CREATE INDEX IF NOT EXISTS idx_nst_turnover_dept  ON nst_turnover(department);

-- ============================================================
-- 模块 #5：nst_store_sales — 店舗別売上（店舗別売上 导出）
-- ============================================================
CREATE TABLE IF NOT EXISTS nst_store_sales (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  fb_store          TEXT,                   -- FB_店舗
  item_code         TEXT NOT NULL,          -- アイテム
  upc               TEXT,                   -- UPCコード
  handling_status   TEXT,                   -- 取扱区分
  display_name      TEXT,                   -- 表示名
  qty_sold          REAL,                   -- 販売数量
  unit_price        REAL,                   -- 購入価格（単価）
  revenue           REAL,                   -- 総収益
  defined_cost      REAL,                   -- 定義原価
  gross_profit      REAL,                   -- 粗利
  gross_margin      REAL,                   -- 粗利率
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
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  internal_id       TEXT NOT NULL,          -- 内部ID
  item_code         TEXT NOT NULL,          -- アイテム
  upc               TEXT,                   -- UPCコード
  display_name      TEXT,                   -- 表示名
  status            TEXT,                   -- ステータス
  bin_number        TEXT,                   -- 保管棚番号
  location          TEXT,                   -- 場所
  handling_status   TEXT,                   -- 取扱区分
  qty_on_hand       REAL,                   -- 手持合計
  qty_committed     REAL,                   -- 確保済合計
  qty_backorder     REAL,                   -- バック・オーダー合計
  std_cost          REAL,                   -- アイテム定義原価
  total_amount      REAL,                   -- 合計金額
  avg_cost          REAL,                   -- 平均原価合計
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
-- 标准列: JAN / 商品名 / 単価 / ロット / 注文最低金額 / 発注条件 / 納期
-- zone 分类 (Boss 2026-05-11):
--   JD_DIRECT      = JD 千葉直送 (NEW WIND/中央物産/菅野/Maple/五洲/アプライド/王子国際/
--                    ハナモン/HK/オンダ/スケーター/ファイン/新日配/グランジェ/トラスコ/エンパイヤ)
--   BENTEN_TRANSIT = 弁天倉庫経由 (共和/大木) — 中継費 +3%
--   EMERGENCY      = 応急/参考 (SD/ハリマ/カード仕入) — 新商品 or 他になし時
--   PREPAID        = 前払い現金 (流久/富森/風雲商事) — なるべく使わない
-- ============================================================
CREATE TABLE IF NOT EXISTS supplier_quote (
  supplier_name     TEXT NOT NULL,        -- sheet 名そのまま ('中央物産' / 'スケーター' ...)
  jan               TEXT NOT NULL,
  display_name      TEXT,
  unit_price        INTEGER,              -- 単価 (円)
  lot_size          INTEGER DEFAULT 1,    -- ロット (最小発注単位の数量)
  case_qty          INTEGER,              -- 入数 (ハリマ等のみ)
  min_order_amount  INTEGER,              -- 注文最低金額 (この仕入先の最低受注額)
  order_condition   TEXT,                 -- 発注条件 ('ケース単位'/'ロット単位発注'/'2.5w〜')
  lead_time_text    TEXT,                 -- 納期 (生テキスト '2週間' 等)
  zone              TEXT,                 -- 'JD_DIRECT'/'BENTEN_TRANSIT'/'EMERGENCY'/'PREPAID'
  zone_rank         INTEGER,              -- 1=JD直送 / 2=弁天経由 / 3=応急 / 4=前払い
  nst_supplier_code TEXT,                 -- NST 仕入先コード ('0490 NEW WIND株式会社' 等)
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
  end_inventory     REAL,                   -- 月末在库（手持合計 聚合）
  monthly_sales     REAL,                   -- 月销量（販売数量 聚合）
  ratio_months      REAL,                   -- end_inventory / monthly_sales
  calculated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, year_month)
);
CREATE INDEX IF NOT EXISTS idx_stock_sales_ratio_sku     ON stock_sales_ratio_monthly(sku);
CREATE INDEX IF NOT EXISTS idx_stock_sales_ratio_month   ON stock_sales_ratio_monthly(year_month);

CREATE TABLE IF NOT EXISTS cross_ratio_monthly (
  sku               TEXT NOT NULL,
  year_month        TEXT NOT NULL,
  gross_margin      REAL,                   -- 粗利率
  turnover          REAL,                   -- 回転率
  cross_ratio       REAL,                   -- gross_margin * turnover
  calculated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku, year_month)
);
CREATE INDEX IF NOT EXISTS idx_cross_ratio_sku          ON cross_ratio_monthly(sku);
CREATE INDEX IF NOT EXISTS idx_cross_ratio_month        ON cross_ratio_monthly(year_month);

CREATE TABLE IF NOT EXISTS health_grade_monthly (
  sku               TEXT NOT NULL,
  year_month        TEXT NOT NULL,
  bucket            TEXT,                   -- 'short' / 'normal' / 'long'
  threshold         REAL,                   -- THRESHOLD[bucket]
  cross_ratio       REAL,
  grade             TEXT,                   -- '🟢 优秀' / '🟡 健康' / '🟠 注意' / '🔴 死钱'
  dead_money_jpy    REAL,                   -- 仅 '🔴 死钱' 时填（库存数量 × 定义原价）
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
  total_payout     REAL,
  currency         TEXT,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shopee_payouts_seller ON shopee_payouts(seller_account);
CREATE INDEX IF NOT EXISTS idx_shopee_payouts_date   ON shopee_payouts(payout_date);

-- ============================================================
-- 模块 #4：shopee_fees — Shopee 服务费明细
-- ============================================================
CREATE TABLE IF NOT EXISTS shopee_fees (
  fee_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  order_no         TEXT,
  fee_type         TEXT,
  amount           REAL,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shopee_fees_order     ON shopee_fees(order_no);
CREATE INDEX IF NOT EXISTS idx_shopee_fees_type      ON shopee_fees(fee_type);

-- ============================================================
-- 模块 #4：shopee_adjustments — Shopee 调整明细
-- ============================================================
CREATE TABLE IF NOT EXISTS shopee_adjustments (
  adjustment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
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
  qty              REAL,
  unit_price       REAL,
  payment_amount   REAL,
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
  margin_pct       REAL,
  monthly_turnover REAL,
  cross_ratio      REAL,
  margin_lv        TEXT,         -- 低/中/高
  turnover_lv      TEXT,         -- 低/中/高
  advice           TEXT,         -- 🔥重点提价 / 🔥重点降价 / ⬆️/⚠️/⚫/✅
  reason           TEXT,
  inventory_value  REAL,
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
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  jan           TEXT NOT NULL,
  supplier      TEXT,
  order_lot     INTEGER,
  price         REAL,
  source_file   TEXT,
  imported_at   TEXT NOT NULL,
  UNIQUE(jan, supplier, order_lot)
);
CREATE INDEX IF NOT EXISTS idx_purchase_data_jan      ON purchase_data(jan);
CREATE INDEX IF NOT EXISTS idx_purchase_data_supplier ON purchase_data(supplier);

-- 订货历史（已下达过的订货单）
CREATE TABLE IF NOT EXISTS purchase_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
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

-- (废弃: store_profit_daily_lines 无 SELECT 引用，由 migrations.py DEPRECATED_TABLES DROP)

-- ============================================================
-- 定义原价变更历史 · 用于 SKU 级波动图
-- ============================================================
CREATE TABLE IF NOT EXISTS std_cost_history (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  internal_id     TEXT NOT NULL,
  item_code       TEXT,
  display_name    TEXT,
  std_cost_old    REAL,
  std_cost_new    REAL NOT NULL,
  diff            REAL,
  diff_pct        REAL,
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

-- (废弃: store_profit_lines 无 SELECT 引用，由 migrations.py DEPRECATED_TABLES DROP)

-- ============================================================
-- Shopee 财务 v2 · 数据源对齐 Boss 提供的两份原表
-- ============================================================

-- 订单导出 (订单导出-*.xlsx Sheet0, 8 列)
-- A=支付币种 B=单价 C=发货数量 D=本地SKU E=支付金额 F=平台 G=订单号 H=店铺
CREATE TABLE IF NOT EXISTS shopee_orders_raw (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  currency        TEXT,                  -- 支付币种
  unit_price      TEXT,                  -- 单价 (多 SKU 时 \n 分隔)
  ship_qty        TEXT,                  -- 发货数量 (多 SKU 时 \n 分隔)
  local_sku       TEXT,                  -- 本地SKU = jan (多 SKU 时 \n 分隔)
  payment_amount  REAL,                  -- 支付金额
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
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
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
  gross_price              REAL,
  product_discount         REAL,
  refund_amount            REAL,
  shopee_rebate            REAL,
  seller_voucher           REAL,
  seller_voucher_jv        REAL,
  seller_shopee_coin       REAL,
  seller_shopee_coin_jv    REAL,
  buyer_shipping           REAL,
  shopee_shipping_subsidy  REAL,
  seller_shipping          REAL,
  return_shipping          REAL,
  return_to_seller_ship    REAL,
  shipping_insurance_save  REAL,
  affiliate_commission     REAL,
  commission               REAL,
  fbs_overseas_fail        REAL,
  fbs_overseas_return      REAL,
  service_fee              REAL,
  shipping_insurance_fee   REAL,
  transaction_fee          REAL,
  fbs_fee                  REAL,
  payout_amount            REAL,
  promo_code               TEXT,
  loss_compensation        REAL,
  actual_weight            REAL,
  seller_shipping_promo    REAL,
  logistics_carrier        TEXT,
  logistics_name           TEXT,
  refund_cash              REAL,
  prorated_shopee_coin     REAL,
  prorated_shopee_voucher  REAL,
  prorated_bank_promo      REAL,
  prorated_payment_promo   REAL,
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
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code         TEXT NOT NULL,        -- A 列 名前
  upc               TEXT,                  -- B 列 UPCコード
  display_name      TEXT,                  -- C 列 表示名
  handling_status   TEXT,                  -- D 列 取扱区分
  std_cost          REAL,                  -- E 列 アイテム定義原価 (当前)
  available         REAL,                  -- F 列 利用可能
  available_on_hand REAL,                  -- G 列 利用可能な保管棚手持
  avg_cost          REAL,                  -- H 列 平均原価 (新定义原価源)
  source_file       TEXT,
  imported_at       TEXT NOT NULL,
  UNIQUE(item_code)
);
CREATE INDEX IF NOT EXISTS idx_nst_item_summary_upc      ON nst_item_summary(upc);
CREATE INDEX IF NOT EXISTS idx_nst_item_summary_avg      ON nst_item_summary(avg_cost);
CREATE INDEX IF NOT EXISTS idx_nst_item_summary_handling ON nst_item_summary(handling_status);

-- ============================================================
-- automation_runs · CMS → N8N / 影刀 自动化任务跟踪表
-- 业务模块（shopee_mass_upload / nst_发注 / 改廃确认）通过 shared/n8n_client.py
-- 触发任务时落 pending 一行，N8N workflow 回调更新 status / summary。
-- ============================================================
CREATE TABLE IF NOT EXISTS automation_runs (
  run_id           TEXT PRIMARY KEY,         -- uuid4
  module           TEXT NOT NULL,            -- 'shopee_mass_upload' / 'nst_order' / 'discontinue'
  payload          TEXT,                     -- JSON · 触发时的业务参数
  status           TEXT NOT NULL,            -- 'pending' / 'processing' / 'completed' / 'failed'
  summary          TEXT,                     -- JSON · 结果汇总（成功/失败计数等）
  triggered_by     TEXT,                     -- 触发者（用户邮箱或 'system'）
  triggered_at     TEXT NOT NULL,            -- ISO8601
  completed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_module    ON automation_runs(module);
CREATE INDEX IF NOT EXISTS idx_automation_runs_status    ON automation_runs(status);
CREATE INDEX IF NOT EXISTS idx_automation_runs_triggered ON automation_runs(triggered_at);

-- ============================================================
-- v2 数据模型（Phase 3.1, 2026-05-09）· 以 JAN 为核心，item 主表
-- ============================================================
-- 设计原则（Boss 决策）：
--   · item.jan 强制必填（无 JAN 不入 v2 — Q1=A）
--   · 店铺两层建模：market_segment（粗）+ shop（细，账号级）— Q2=C
--   · purchase 全合并按 source 区分 — Q3=A
--   · 去掉 category 字段（Shopee 类目自动填 暂不需要）
--   · 保留 benten_stock / warehouse_stock；废弃 store_profit_lines/_daily_lines（Q4=C）
--
-- 与旧表关系：
--   · 旧 item / item_master / item_master_netsuite / nst_item_summary 数据合并 → item_v2
--   · 旧 nst_inventory_snapshot ↔ item_inventory_snapshot_v2（jan 为 key）
--   · 旧 sales_line / nst_store_sales 拆分 → item_sales_history + shop_sales
--   · 旧 purchase / purchase_data / purchase_history 合并 → item_purchase_history
--   · 旧 store_monthly → shop_monthly
--   · 旧 std_cost_history → item_cost_history（重命名）
-- ============================================================


-- 维度 A · item × 时间序列（4 张子表）
-- ────────────────────────────────────────────────────────────

-- A1. 进货明细
CREATE TABLE IF NOT EXISTS item_purchase_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  jan          TEXT NOT NULL,
  po_number    TEXT,
  supplier     TEXT,
  qty          INTEGER,
  unit_cost    REAL,
  total_cost   REAL,
  ordered_at   TEXT,
  received_at  TEXT,
  source       TEXT,                  -- 'netsuite_po' / 'supplier_csv' / 'manual'
  imported_at  TEXT,
  UNIQUE(po_number, jan, source)
);
CREATE INDEX IF NOT EXISTS idx_iph_jan       ON item_purchase_history(jan);
CREATE INDEX IF NOT EXISTS idx_iph_supplier  ON item_purchase_history(supplier);
CREATE INDEX IF NOT EXISTS idx_iph_ordered   ON item_purchase_history(ordered_at);

-- A2. 销售明细（item 视角）
CREATE TABLE IF NOT EXISTS item_sales_history (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  jan           TEXT NOT NULL,
  period_start  TEXT,
  period_end    TEXT,
  channel       TEXT,                 -- 'shopee_tw' / 'lazada_my' / 'netsuite_store'
  qty_sold      REAL,
  revenue       REAL,
  cost          REAL,
  gross_profit  REAL,
  gross_margin  REAL,
  source        TEXT,                 -- 'nst_store_sales' / 'shopee_orders' / 'asean_monthly' / 'asean_daily'
  imported_at   TEXT,
  UNIQUE(jan, period_start, period_end, channel, source)
);
CREATE INDEX IF NOT EXISTS idx_ish_jan      ON item_sales_history(jan);
CREATE INDEX IF NOT EXISTS idx_ish_channel  ON item_sales_history(channel);
CREATE INDEX IF NOT EXISTS idx_ish_period   ON item_sales_history(period_start);

-- A3. 库存快照（替代 nst_inventory_snapshot · 含全字段方便直查）
CREATE TABLE IF NOT EXISTS item_inventory_snapshot_v2 (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  jan           TEXT NOT NULL,
  -- ID 区（Phase 4 新加，方便不查 item_v2 直接看）
  item_code     TEXT,                   -- アイテム
  internal_id   TEXT,                   -- 内部ID
  display_name  TEXT,                   -- 表示名
  -- 库存维度
  location      TEXT,                   -- 場所（仓库）
  bin_number    TEXT,                   -- 保管棚番号
  snapshot_at   TEXT,                   -- 快照时点
  -- 库存量
  qty_on_hand   REAL,                   -- 手持合計
  qty_committed REAL,                   -- 確保済合計
  qty_backorder REAL,                   -- バック・オーダー合計（旧字段, 复用存 注文待ち 近似）
  qty_on_order  REAL,                   -- 注文済（PO 已下达, 待到货）
  qty_waiting   REAL,                   -- 注文待ち（待 PO 安排）
  qty_in_transit REAL,                  -- 輸送中（运输中）
  -- 价值
  std_cost      REAL,                   -- 定義原価
  avg_cost      REAL,                   -- 平均原価
  total_amount  REAL,                   -- 合計金額（Phase 4 新加）
  -- 业务字段
  handling_status TEXT,                 -- 取扱区分（Phase 4 新加）
  status        TEXT,                   -- ステータス（通常在庫 等，Phase 4 新加）
  owner         TEXT,                   -- 担当者（Phase 4 新加）
  department    TEXT,                   -- 部門（Phase 4 新加）
  imported_at   TEXT,
  UNIQUE(jan, location, bin_number, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_iis2_jan      ON item_inventory_snapshot_v2(jan);
CREATE INDEX IF NOT EXISTS idx_iis2_location ON item_inventory_snapshot_v2(location);
CREATE INDEX IF NOT EXISTS idx_iis2_snapshot ON item_inventory_snapshot_v2(snapshot_at);

-- A4. 原価历史
CREATE TABLE IF NOT EXISTS item_cost_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  jan          TEXT NOT NULL,
  std_cost     REAL,
  avg_cost     REAL,
  changed_by   TEXT,
  changed_at   TEXT NOT NULL,
  reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ich_jan ON item_cost_history(jan);
CREATE INDEX IF NOT EXISTS idx_ich_changed ON item_cost_history(changed_at);

-- 维度 B · shop × item（3 张表 · 两层店铺建模 Q2=C）
-- ────────────────────────────────────────────────────────────

-- B0. market_segment（粗粒度，TW/SG/MY/PH/TH/VN/ID/JP/...）
CREATE TABLE IF NOT EXISTS market_segment (
  market_id    TEXT PRIMARY KEY,        -- 'TW' / 'SG' / ...
  display_name TEXT NOT NULL,           -- '台湾' / 'Singapore'
  currency     TEXT,                    -- TWD / SGD
  active       INTEGER DEFAULT 1
);

-- B1. shop（细粒度，账号级）
CREATE TABLE IF NOT EXISTS shop (
  shop_id      TEXT PRIMARY KEY,        -- 'shopee_tw_smikie_main' 等
  market_id    TEXT NOT NULL,           -- 关联 market_segment
  platform     TEXT NOT NULL,           -- shopee / lazada / amazon / coupang / netsuite
  display_name TEXT NOT NULL,           -- 「Smikie 台湾旗舰店」
  currency     TEXT,
  owner        TEXT,                    -- 运营负责人
  active       INTEGER DEFAULT 1,
  created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_shop_market   ON shop(market_id);
CREATE INDEX IF NOT EXISTS idx_shop_platform ON shop(platform);


-- B3. shop 月度 KPI（替代 store_monthly）
CREATE TABLE IF NOT EXISTS shop_monthly (
  shop_id          TEXT NOT NULL,
  year_month       TEXT NOT NULL,        -- YYYYMM
  gmv              REAL,
  profit           REAL,
  margin_rate      REAL,
  profit_contrib   REAL,
  deduction_total  REAL,
  order_count      INTEGER,
  store_rating     REAL,
  online_products  INTEGER,
  imported_at      TEXT,
  PRIMARY KEY(shop_id, year_month)
);
CREATE INDEX IF NOT EXISTS idx_sm_ym ON shop_monthly(year_month);

-- 元数据 · v2 迁移状态记录
CREATE TABLE IF NOT EXISTS _v2_migration_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  step          TEXT NOT NULL,         -- 'item_v2' / 'shop' / 'item_sales_history' 等
  source_table  TEXT,
  rows_read     INTEGER,
  rows_written  INTEGER,
  errors        INTEGER,
  ran_at        TEXT NOT NULL,
  notes         TEXT
);

-- ============================================================
-- v2 补充表 · Phase 3.6（2026-05-09）
-- 合并 supplier_cost + supplier_jan_list → item_supplier_link
-- benten_stock + warehouse_stock 通过 ETL 写入 item_inventory_snapshot_v2
-- ============================================================
CREATE TABLE IF NOT EXISTS item_supplier_link (
  jan           TEXT NOT NULL,
  supplier_name TEXT NOT NULL,
  cost_class    TEXT,                  -- 'AB' / 'C'（来自 supplier_cost）
  unit_cost     REAL,                  -- 报价（来自 supplier_cost）
  currency      TEXT,                  -- 报价货币
  status        TEXT,                  -- 在该供应商的状态（来自 supplier_jan_list）
  source        TEXT,                  -- 'supplier_cost' / 'supplier_jan_list' / 'merged'
  imported_at   TEXT,
  PRIMARY KEY (jan, supplier_name)
);
CREATE INDEX IF NOT EXISTS idx_isl_jan      ON item_supplier_link(jan);
CREATE INDEX IF NOT EXISTS idx_isl_supplier ON item_supplier_link(supplier_name);
CREATE INDEX IF NOT EXISTS idx_isl_class    ON item_supplier_link(cost_class);

-- ============================================================
-- Phase 4 · 旧表名 → VIEW（桥接 v2，让现有 page SQL 不用改）
-- 设计：
--   · v2 表是唯一真表（item_v2 / shop_sales / item_inventory_snapshot_v2 等）
--   · 旧表名（inventory_snapshot / sales_line / nst_* / item_master*）改成 VIEW
--   · page / module 的 SELECT 不动，自动透传到 v2
--   · ingester 已经直写 v2，旧表名 VIEW 只读
--
-- 执行顺序由 migrations.py PHASE4_VIEWS 控制：先 DROP 旧表 → CREATE VIEW
-- ============================================================

-- inventory_snapshot：原 NetSuite 库存快照（19 列）→ 桥到 item_inventory_snapshot_v2
CREATE VIEW IF NOT EXISTS v_inventory_snapshot AS
SELECT
  internal_id, item_code, jan AS upc, display_name,
  status, bin_number, location, handling_status,
  qty_on_hand, qty_committed, qty_backorder,
  std_cost, total_amount, avg_cost,
  owner, department, snapshot_at,
  '' AS source_file, imported_at
FROM item_inventory_snapshot_v2;

-- nst_inventory_snapshot：内容同 inventory_snapshot
CREATE VIEW IF NOT EXISTS v_nst_inventory_snapshot AS
SELECT * FROM v_inventory_snapshot;







-- ============================================================
-- 月度完売率 · アイテム月完売率300 → 库存健康度 + 订货依据数据源
-- 用于:
--   · sell_through_rate >= 0.9 → 断货风险 → 加大订货
--   · 0.5 <= rate < 0.9         → 正常
--   · rate < 0.5                → 压库存 → 减少订货
-- 来源: 【輸出】アイテム月完売率300.xls (NST 月度报表, 19 字段)
-- ============================================================
CREATE TABLE IF NOT EXISTS item_monthly_turnover (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  item_code         TEXT NOT NULL,           -- アイテム (NetSuite item_code, 例 '01-0641-134' 或 JAN)
  jan               TEXT,                    -- 通过 item_v2 lookup 补齐 (可空)
  location          TEXT,                    -- 場所 (仓库:子库)
  department        TEXT,                    -- 部門
  year_month        TEXT NOT NULL,           -- YYYYMM
  -- 期初
  open_qty          REAL,                    -- 開始時の手持在庫数量
  open_avg_cost    REAL,                    -- 開始平均原価
  open_amount       REAL,                    -- 開始時の手持在庫額
  -- 入库
  qty_received      REAL,                    -- 受領
  qty_other_in      REAL,                    -- その他の在庫入庫
  qty_total_in      REAL,                    -- 合計入庫数量
  manual_input      REAL,                    -- 入力値
  last_received_at  TEXT,                    -- 前回の受領日
  -- 出库
  qty_sold          REAL,                    -- 売上 (出庫数量)
  qty_other_out     REAL,                    -- その他の在庫出庫
  qty_total_out     REAL,                    -- 合計出庫数量
  out_amount        REAL,                    -- 出庫価額
  last_sold_at      TEXT,                    -- 前回の売上日
  -- 期末
  close_qty         REAL,                    -- 終了時の手持在庫数量
  close_avg_cost    REAL,                    -- 期末平均原価
  close_amount      REAL,                    -- 終了時の手持在庫額
  -- 派生指标 (ingest 端计算)
  sell_through_rate REAL,                    -- 完売率 = qty_sold / (open_qty + qty_total_in)
  rank              TEXT,                    -- 商品ランク (从 item_v2 JOIN)
  risk_label        TEXT,                    -- '断货风险' / '正常' / '压库存' / '不参与' / '无数据'
  imported_at       TEXT NOT NULL,
  UNIQUE(item_code, location, year_month)
);
CREATE INDEX IF NOT EXISTS idx_imt_item     ON item_monthly_turnover(item_code);
CREATE INDEX IF NOT EXISTS idx_imt_jan      ON item_monthly_turnover(jan);
CREATE INDEX IF NOT EXISTS idx_imt_ym       ON item_monthly_turnover(year_month);
CREATE INDEX IF NOT EXISTS idx_imt_rate     ON item_monthly_turnover(sell_through_rate);
CREATE INDEX IF NOT EXISTS idx_imt_risk     ON item_monthly_turnover(risk_label);
