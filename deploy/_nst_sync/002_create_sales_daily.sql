-- nst.sales_daily · 店舗別売上 日次（2026-05-20 追加）
--   schema 000 の TODO で予定されていた sales_daily を正式に落とす。
--   sales_monthly と同じ抽出ロジック（【輸出】店舗別売上_JO 再現）だが trandate を日別に保持。
--   pull_sales.py が日次で UPSERT し、sales_monthly は本表から再集計（単一事実源 = sales_daily）。
-- 在 PG 上执行：psql $DATABASE_URL -f 002_create_sales_daily.sql / 幂等。

CREATE TABLE IF NOT EXISTS nst.sales_daily (
    shop              TEXT NOT NULL,           -- BUILTIN.DF(custbody_fb_ne_ro_shop)
    item_internal_id  TEXT NOT NULL,
    sale_date         DATE NOT NULL,
    qty_sold          NUMERIC(14,2),
    revenue           NUMERIC(16,2),           -- JPY 換算後（foreignamount × exchangerate）
    pulled_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (shop, item_internal_id, sale_date)
);
CREATE INDEX IF NOT EXISTS idx_sales_daily_date      ON nst.sales_daily (sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_daily_item      ON nst.sales_daily (item_internal_id);
CREATE INDEX IF NOT EXISTS idx_sales_daily_shop_date ON nst.sales_daily (shop, sale_date);
COMMENT ON TABLE nst.sales_daily IS '店舗別売上 日次 · レポート【輸出】店舗別売上_JO 再現（顧客 fb_dept=4）· sales_monthly の再集計元';

-- 定时取得スケジュール seed（冪等・DO NOTHING で既存設定を尊重）: 売上 日次 07:00
-- daily_pull --domains sales は --since 当月を日別取得し sales_monthly を再集計。
INSERT INTO nst.pull_schedule (job_key, category, frequency, domains, run_time, description)
VALUES ('sales_daily', 'sales', 'daily', 'sales', '07:00',
        '店舗別売上：販売数量・総収益（日次・輸出事業 fb_dept=4 / JPY 換算）')
ON CONFLICT (job_key) DO NOTHING;
