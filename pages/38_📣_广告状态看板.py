"""模块 #38 广告状态看板 · T-340(Boss 2026-07-27 指示先行开发).

数据源:元川 PG `marketing` schema 的三个视图 **只读**——
  v_marketing_data_freshness / v_marketing_daily_decision / v_marketing_attribution_gap
连接:独立 `MARKETING_DATABASE_URL`(cms_reader 角色·仅三视图 SELECT),
  不用 shared.db 的超级用户连接;未配置时页面明确提示,不静默空白。
边界(契约 docs/获客/10 §6):页面无外部 API 调用、无任何广告写按钮;
  未接入渠道(Pinterest/Reddit/站内漏斗/Search-term)显示「未接入」,不显示零。
口径:Ads 金额 = cost_micros/1e6(账户币种);Shopify revenue = 各源货币未折算,
  三源转化/订单并列展示、不相加(归因口径不同)。
"""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd
import streamlit as st

from shared.i18n import lang_selector, t, get_lang

st.set_page_config(page_title=t("广告状态看板"), page_icon="📣", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()

_ja = get_lang() == "ja"


def _dl(zh: str, ja: str) -> str:
    return ja if _ja else zh


MONTHLY_AD_BUDGET_JPY = 200_000  # Boss 2026-07:自建站推广月预算(Google+Pinterest+Reddit 合计)


def _connect():
    """cms_reader 只读连接。独立于 shared.db(那是超级用户,本页禁用)。"""
    url = (os.environ.get("MARKETING_DATABASE_URL") or "").strip()
    if not url:
        return None
    import psycopg2
    return psycopg2.connect(url)


@st.cache_data(ttl=300, show_spinner=False)
def _load_frames() -> dict[str, pd.DataFrame] | str | None:
    try:
        conn = _connect()
    except Exception as exc:  # 连接失败(密码/网络)→ 显式提示,不裸抛
        return f"connect: {exc}"
    if conn is None:
        return None
    try:
        fresh = pd.read_sql("SELECT * FROM marketing.v_marketing_data_freshness", conn)
        decision = pd.read_sql("SELECT * FROM marketing.v_marketing_daily_decision", conn)
        gap = pd.read_sql("SELECT * FROM marketing.v_marketing_attribution_gap", conn)
    except Exception as exc:  # 权限/视图缺失 → 同上
        return f"query: {exc}"
    finally:
        try:
            conn.close()
        except Exception:
            pass
    for df in (decision, gap):
        if "report_date" in df.columns:
            df["report_date"] = pd.to_datetime(df["report_date"]).dt.date
    return {"fresh": fresh, "decision": decision, "gap": gap}


st.title(t("📣 广告状态看板"))
st.caption(_dl(
    "只读 marketing 三视图(cms_reader)· 三源归因口径并列展示、不相加 · 页面不含任何广告操作按钮",
    "marketing 3ビュー読取専用(cms_reader)· 3ソースの帰属口径は並列表示・合算しない · 広告操作ボタンは一切なし",
))

frames = _load_frames()
if frames is None:
    st.warning(_dl(
        "MARKETING_DATABASE_URL 未配置——本页需要 cms_reader 只读连接(部署时在 compose 注入),不使用主数据库连接。",
        "MARKETING_DATABASE_URL が未設定です——本ページは cms_reader 読取専用接続が必要(デプロイ時に compose で注入)。メイン DB 接続は使いません。",
    ))
    st.stop()
if isinstance(frames, str):
    st.error(_dl(f"marketing 数据连接失败({frames})——检查 cms_reader 密码/002 迁移是否已应用。",
                 f"marketing データ接続失敗({frames})——cms_reader パスワード/002 マイグレーション適用を確認。"))
    st.stop()

fresh, decision, gap = frames["fresh"], frames["decision"], frames["gap"]
today = dt.date.today()

# ── 1. 数据新鲜度(green=36h内拉取成功可信 / amber=延迟或最近出错 / red=源中断)──
_STATUS_ICON = {"green": "🟢", "amber": "🟡", "red": "🔴"}
_STATUS_TEXT = {
    "green": ("正常", "正常"),
    "amber": ("延迟", "遅延"),
    "red": ("中断", "停止"),
}
_SRC_LABEL = {"google_ads": "Google Ads", "ga4": "GA4", "shopify": "Shopify"}
cols = st.columns(3)
for col, (_, row) in zip(cols, fresh.iterrows()):
    status = row["freshness_status"]
    icon = _STATUS_ICON.get(status, "⚪")
    zh, ja = _STATUS_TEXT.get(status, (status, status))
    ts = row["last_success_at"]
    ts_s = pd.to_datetime(ts).strftime("%m-%d %H:%M") if pd.notna(ts) else _dl("从未成功", "成功なし")
    col.metric(f"{icon} {_SRC_LABEL.get(row['source'], row['source'])}",
               _dl(zh, ja),
               _dl(f"最后成功拉取 {ts_s}", f"最終取得成功 {ts_s}"), delta_color="off")
st.caption(_dl("状态含义:🟢正常=36小时内拉取成功 · 🟡延迟=超36小时或最近一次出错 · 🔴中断=源失败/授权失效(数字不可用)",
               "状態:🟢正常=36時間以内に取得成功 · 🟡遅延=36時間超過/直近エラー · 🔴停止=ソース失敗/認証失効(数字使用不可)"))
if (fresh["freshness_status"] == "red").any():
    st.error(_dl("存在 red 源:数据不完整,本页数字不可用于投放判断。",
                 "red ソースあり:データ不完全。本ページの数字は出稿判断に使用不可。"))

ads = decision[decision["source"] == "google_ads"].copy()
ga4 = decision[decision["source"] == "ga4"].copy()
shp = decision[decision["source"] == "shopify"].copy()
ads["cost_usd"] = ads["cost_micros"].fillna(0) / 1_000_000

# ── 2. 本月 KPI ──────────────────────────────────────────────
month_start = today.replace(day=1)
ads_mtd = ads[ads["report_date"] >= month_start]
shp_mtd = shp[shp["report_date"] >= month_start]
mtd_cost = float(ads_mtd["cost_usd"].sum())
k1, k2, k3, k4 = st.columns(4)
k1.metric(_dl("本月 Google 消耗(USD)", "今月 Google 消化(USD)"), f"${mtd_cost:,.2f}",
          _dl(f"总预算 ¥{MONTHLY_AD_BUDGET_JPY:,}/月(Google+Pin+Reddit 合计·JPY)",
              f"総予算 ¥{MONTHLY_AD_BUDGET_JPY:,}/月(Google+Pin+Reddit 合計·JPY)"), delta_color="off")
k2.metric(_dl("本月 Ads 点击", "今月 Ads クリック"), f"{int(ads_mtd['clicks'].fillna(0).sum()):,}")
k3.metric(_dl("本月 Ads 转化(Ads 口径)", "今月 Ads CV(Ads 口径)"),
          f"{float(ads_mtd['conversions'].fillna(0).sum()):,.1f}")
k4.metric(_dl("本月 Shopify 订单(实绩)", "今月 Shopify 注文(実績)"),
          f"{int(shp_mtd['orders'].fillna(0).sum()):,}")
st.caption(_dl(
    "未接入(不显示零):Pinterest 消耗 · Reddit 消耗 · 站内漏斗(ATC/结账) · Search-term 明细",
    "未接続(ゼロ表示しない):Pinterest 消化 · Reddit 消化 · サイト内ファネル(ATC/チェックアウト) · Search-term 明細",
))

# ── 3. Google campaign 日次(近 30 日)─────────────────────────
st.subheader(_dl("Google campaign 日次(近30日)", "Google campaign 日次(直近30日)"))
a30 = ads[ads["report_date"] >= today - dt.timedelta(days=30)].copy()
if a30.empty:
    st.info(_dl("期间内无 Ads 数据。", "期間内に Ads データなし。"))
else:
    a30["cpc_usd"] = (a30["cost_usd"] / a30["clicks"].replace(0, pd.NA)).astype(float)
    view = a30.sort_values("report_date", ascending=False)[
        ["report_date", "dimension", "cost_usd", "impressions", "clicks", "cpc_usd",
         "conversions", "conversions_value"]]
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "report_date": st.column_config.DateColumn(_dl("日期", "日付")),
        "dimension": "campaign",
        "cost_usd": st.column_config.NumberColumn(_dl("消耗$", "消化$"), format="localized"),
        "impressions": st.column_config.NumberColumn(_dl("展示", "表示"), format="localized"),
        "clicks": st.column_config.NumberColumn(_dl("点击", "クリック"), format="localized"),
        "cpc_usd": st.column_config.NumberColumn("CPC$", format="%.1f"),
        "conversions": st.column_config.NumberColumn(_dl("转化", "CV"), format="%.2f"),
        "conversions_value": st.column_config.NumberColumn(_dl("转化价值", "CV価値"), format="%.0f"),
    })

# ── 4. 三源日次并列(近 14 日)────────────────────────────────
st.subheader(_dl("三源日次并列(近14日 · 各自归因口径,不相加)",
                 "3ソース日次並列(直近14日 · 各帰属口径・合算しない)"))
d14 = today - dt.timedelta(days=14)


def _daily(df: pd.DataFrame, cols_map: dict[str, str]) -> pd.DataFrame:
    sub = df[df["report_date"] >= d14]
    if sub.empty:
        return pd.DataFrame(columns=["report_date", *cols_map.values()])
    g = sub.groupby("report_date").agg({k: "sum" for k in cols_map}).reset_index()
    return g.rename(columns=cols_map)


merged = (
    _daily(ads, {"cost_usd": "ads_cost_usd", "clicks": "ads_clicks", "conversions": "ads_conv"})
    .merge(_daily(ga4, {"sessions": "ga4_sessions", "orders": "ga4_orders"}),
           on="report_date", how="outer")
    .merge(_daily(shp, {"orders": "shopify_orders", "revenue": "shopify_revenue"}),
           on="report_date", how="outer")
    .sort_values("report_date", ascending=False)
)
st.dataframe(merged, hide_index=True, width="stretch", column_config={
    "report_date": st.column_config.DateColumn(_dl("日期", "日付")),
    "ads_cost_usd": st.column_config.NumberColumn(_dl("Ads消耗$", "Ads消化$"), format="localized"),
    "ads_clicks": st.column_config.NumberColumn(_dl("Ads点击", "Adsクリック"), format="localized"),
    "ads_conv": st.column_config.NumberColumn("Ads CV", format="%.2f"),
    "ga4_sessions": st.column_config.NumberColumn("GA4 sessions", format="localized"),
    "ga4_orders": st.column_config.NumberColumn("GA4 orders", format="localized"),
    "shopify_orders": st.column_config.NumberColumn(_dl("Shopify订单", "Shopify注文"), format="localized"),
    "shopify_revenue": st.column_config.NumberColumn(_dl("Shopify收入*", "Shopify売上*"), format="%.1f"),
})
st.caption(_dl("* Shopify 收入为各订单结算货币原值合计,币种未折算(二轮改善);GA4 接入初期历史为 0 属正常。",
               "* Shopify 売上は決済通貨のまま合算・通貨換算なし(第2弾で改善);GA4 は接続初期のため過去分 0 は正常。"))

# ── 5. GA4 × Shopify 归因差异(近 14 日)──────────────────────
st.subheader(_dl("GA4 × Shopify 归因差异(近14日)", "GA4 × Shopify 帰属差異(直近14日)"))
g14 = gap[gap["report_date"] >= d14].sort_values("report_date", ascending=False)
st.dataframe(g14, hide_index=True, width="stretch", column_config={
    "report_date": st.column_config.DateColumn(_dl("日期", "日付")),
    "ga4_transactions": "GA4 transactions",
    "shopify_orders": _dl("Shopify订单", "Shopify注文"),
    "ga4_revenue": "GA4 revenue",
    "shopify_net_revenue": _dl("Shopify净收入", "Shopify純売上"),
    "transaction_gap": st.column_config.NumberColumn(_dl("差(GA4−Shopify)", "差(GA4−Shopify)")),
})
st.caption(_dl("差异是两套归因系统的正常现象,解释它而非强行对齐;持续大差 → 检查埋码/结账域名。",
               "差異は 2 つの帰属システム間で正常。無理に一致させず説明する;継続的な大差 → 計測タグ/チェックアウトドメイン確認。"))
