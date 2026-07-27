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

import altair as alt
import pandas as pd
import streamlit as st

from modules.ads_roi import (
    SAMPLE_MIN_CONVERSIONS,
    add_ratio_columns,
    breakeven_roas,
    resample,
    safe_ratio,
    verdict,
)
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

# 毛利率默认值(doc 23 §3.3 · Boss 2026-07-27 拍板)。保本 ROAS = 1÷0.50 = 2.00x。
# 依据:本地快照 nst_store_sales(2026-05-05)实测全店 55.89% / Shopee PH 55.12%,
# 下调至 50% 因该口径为 NST 定义原価毛利,**不含物流/广告/支付/平台佣金**,且宁可高估保本线。
# 这是**平台店代理值**——自建站数据在 NST 侧尚未就绪(接入进行中)。
# 升级触发:当 nst.sales_daily 出现 Shopify 自建站 shop 记录时,重评 doc 23 §2 方案 C。
DEFAULT_GROSS_MARGIN = 0.50


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

# ── 1. 数据新鲜度:角落只显示拉取时间;异常源才警告(Boss 2026-07-27)──
_ts = pd.to_datetime(fresh["last_success_at"]).max()
_ts_s = _ts.strftime("%m-%d %H:%M") if pd.notna(_ts) else _dl("从未成功", "成功なし")
st.caption(_dl(f"数据更新:{_ts_s}(每日 07:30 自动拉取)", f"データ更新:{_ts_s}(毎日 07:30 自動取得)"))
_bad = fresh[fresh["freshness_status"] != "green"]
if not _bad.empty:
    names = ", ".join(_bad["source"])
    if (_bad["freshness_status"] == "red").any():
        st.error(_dl(f"数据源中断:{names}——本页数字不完整,不可用于投放判断。",
                     f"データソース停止:{names}——本ページの数字は不完全。出稿判断に使用不可。"))
    else:
        st.warning(_dl(f"数据源延迟:{names}(超 36 小时未成功拉取)", f"データソース遅延:{names}(36時間超未取得)"))

ads = decision[decision["source"] == "google_ads"].copy()
ga4 = decision[decision["source"] == "ga4"].copy()
shp = decision[decision["source"] == "shopify"].copy()
ads["cost_usd"] = ads["cost_micros"].fillna(0) / 1_000_000

# ── 毛利率参数(doc 23 §3.3)· 保本线的唯一输入 ──────────────
with st.sidebar:
    st.markdown("---")
    gross_margin = st.number_input(
        _dl("毛利率(用于保本线)", "粗利率(損益分岐用)"),
        min_value=0.01, max_value=1.0, value=DEFAULT_GROSS_MARGIN, step=0.01,
        format="%.2f",
        help=_dl("毛利率 = gross_profit ÷ revenue(NST 定义原価口径,不含物流/广告/支付/平台佣金)",
                 "粗利率 = gross_profit ÷ revenue(NST 定義原価ベース・物流/広告/決済/手数料を含まない)"),
    )
    _be = breakeven_roas(gross_margin)
    st.caption(_dl(f"保本 ROAS = 1÷{gross_margin:.0%} = **{_be:.2f}x**",
                   f"損益分岐 ROAS = 1÷{gross_margin:.0%} = **{_be:.2f}x**"))
    st.caption(_dl(
        "⚠️ 该口径不含物流/广告/平台费,算出的保本线是**乐观下限**,真实盈亏线更高。"
        "默认 0.50 为平台店代理值(自建站数据在 NST 侧尚未就绪),请按 page37 实际毛利校准。",
        "⚠️ 本口径は物流/広告/手数料を含まないため、損益分岐線は**楽観的な下限**。"
        "既定 0.50 はプラットフォーム店の代理値(自社サイトは NST 未対応)。page37 の実績で校正を。",
    ))

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

# ── 2b. 盈亏判定(doc 23 §3.2)· 本页的核心问题:这笔钱赚没赚 ──
_mtd_conv = float(ads_mtd["conversions"].fillna(0).sum())
_mtd_value = float(ads_mtd["conversions_value"].fillna(0).sum())
_mtd_roas = safe_ratio(_mtd_value, mtd_cost)
_mtd_cpa = safe_ratio(mtd_cost, _mtd_conv)
_level, _label = verdict(_mtd_roas, _be, _mtd_conv)

v1, v2, v3 = st.columns(3)
v1.metric(_dl("本月 ROAS(Ads 口径)", "今月 ROAS(Ads 口径)"),
          f"{_mtd_roas:.2f}x" if _mtd_roas is not None else "—",
          _dl(f"保本线 {_be:.2f}x", f"損益分岐 {_be:.2f}x"), delta_color="off")
v2.metric(_dl("本月 CPA", "今月 CPA"),
          f"${_mtd_cpa:,.2f}" if _mtd_cpa is not None else "—",
          _dl("越低越好", "低いほど良い"), delta_color="off")
v3.metric(_dl("本月转化数", "今月 CV 数"), f"{_mtd_conv:,.1f}",
          _dl(f"判定门槛 {SAMPLE_MIN_CONVERSIONS}", f"判定閾値 {SAMPLE_MIN_CONVERSIONS}"),
          delta_color="off")

if _level == "insufficient":
    st.info(_dl(
        f"⚪ **{_label}** —— 本月转化 {_mtd_conv:.0f} < {SAMPLE_MIN_CONVERSIONS},"
        "ROAS/CPA 属统计噪音,**不要据此调预算或出价**。先积累样本,或先修转化跟踪。",
        f"⚪ **サンプル不足・判定不可** —— 今月 CV {_mtd_conv:.0f} < {SAMPLE_MIN_CONVERSIONS}。"
        "ROAS/CPA は統計ノイズ。**これを根拠に予算・入札を動かさないこと**。",
    ))
elif _level == "profit":
    st.success(_dl(f"🟢 **{_label}** —— ROAS {_mtd_roas:.2f}x ≥ 保本线 {_be:.2f}x × 1.2",
                   f"🟢 **黒字** —— ROAS {_mtd_roas:.2f}x ≥ 損益分岐 {_be:.2f}x × 1.2"))
elif _level == "marginal":
    st.warning(_dl(f"🟡 **{_label}** —— ROAS {_mtd_roas:.2f}x 刚过保本线 {_be:.2f}x,缓冲很薄",
                   f"🟡 **損益トントン** —— ROAS {_mtd_roas:.2f}x は損益分岐 {_be:.2f}x 付近"))
elif _level == "loss":
    st.error(_dl(f"🔴 **{_label}** —— ROAS {_mtd_roas:.2f}x < 保本线 {_be:.2f}x;"
                 "按决策链先查数据可信度,再逐环定位,**修复优先于加量**",
                 f"🔴 **赤字** —— ROAS {_mtd_roas:.2f}x < 損益分岐 {_be:.2f}x"))

st.caption(_dl(
    "ROAS/CPA 为 **Ads 归因口径**,受转化跟踪完整性影响;"
    "跟踪缺失会使 ROAS 偏低、CPA 偏高——请对照下方「三源日次并列」的 Shopify 实绩订单交叉验证。",
    "ROAS/CPA は **Ads 帰属ベース**。計測欠損時は ROAS 過小・CPA 過大になる——"
    "下部「3ソース日次並列」の Shopify 実績と突き合わせて確認。",
))

# ── 3-5. 三板块 → 各自 tab(Boss 2026-07-27)─────────────────
tab_camp, tab_trend, tab_multi, tab_gap = st.tabs([
    _dl("📈 Google campaign 日次", "📈 Google campaign 日次"),
    _dl("📉 趋势曲线", "📉 トレンド"),
    _dl("📊 三源日次并列", "📊 3ソース日次並列"),
    _dl("🔀 归因差异", "🔀 帰属差異"),
])

with tab_camp:
    st.caption(_dl("近30日 · 金额为 Ads 账户币种(USD)", "直近30日 · 金額は Ads アカウント通貨(USD)"))
    a30 = ads[ads["report_date"] >= today - dt.timedelta(days=30)].copy()
    if a30.empty:
        st.info(_dl("期间内无 Ads 数据。", "期間内に Ads データなし。"))
    else:
        a30 = add_ratio_columns(a30).rename(columns={"cpc": "cpc_usd"})
        cols_show = ["report_date", "dimension", "cost_usd", "impressions", "clicks",
                     "ctr", "cpc_usd", "conversions", "conversions_value", "roas", "cpa"]
        # 004 扩列后才有的三个展示份额(旧视图无此列时静默降级)
        for c in ("search_impression_share", "search_budget_lost_impression_share",
                  "search_rank_lost_impression_share"):
            if c in a30.columns:
                cols_show.append(c)
        view = a30.sort_values("report_date", ascending=False)[cols_show]
        st.dataframe(view, hide_index=True, width="stretch", column_config={
            "report_date": st.column_config.DateColumn(_dl("日期", "日付")),
            "dimension": "campaign",
            "cost_usd": st.column_config.NumberColumn(_dl("消耗$", "消化$"), format="localized"),
            "impressions": st.column_config.NumberColumn(_dl("展示", "表示"), format="localized"),
            "clicks": st.column_config.NumberColumn(_dl("点击", "クリック"), format="localized"),
            "ctr": st.column_config.NumberColumn("CTR", format="percent"),
            "cpc_usd": st.column_config.NumberColumn("CPC$", format="%.2f"),
            "conversions": st.column_config.NumberColumn(_dl("转化", "CV"), format="%.2f"),
            "conversions_value": st.column_config.NumberColumn(_dl("转化价值", "CV価値"), format="%.0f"),
            "roas": st.column_config.NumberColumn(
                "ROAS", format="%.2f",
                help=_dl(f"转化价值÷消耗(Ads 归因)· 保本线 {_be:.2f}x · 空=当日无消耗",
                         f"CV価値÷消化(Ads帰属)· 損益分岐 {_be:.2f}x")),
            "cpa": st.column_config.NumberColumn(
                "CPA$", format="%.2f",
                help=_dl("消耗÷转化数 · 空=当日零转化 · 需低于单均毛利",
                         "消化÷CV数 · 空=当日CVゼロ")),
            "search_impression_share": st.column_config.NumberColumn(
                _dl("展示份额", "IS"), format="percent",
                help=_dl("参竞率;品牌词应>90%", "参加率;ブランド語は>90%")),
            "search_budget_lost_impression_share": st.column_config.NumberColumn(
                _dl("预算丢失", "予算ロスIS"), format="percent",
                help=_dl(">20-30%且ROAS达标=加预算信号", ">20-30%かつROAS達成=増額シグナル")),
            "search_rank_lost_impression_share": st.column_config.NumberColumn(
                _dl("排名丢失", "順位ロスIS"), format="percent",
                help=_dl("区分「没钱」还是「竞争不过」", "「予算不足」か「競争負け」かの判別")),
        })

with tab_trend:
    # 粒度 × 指标(doc 23 §3.5)· 同一份日数据的三种重采样,不是三套代码
    _GRAINS = {_dl("日", "日次"): "day", _dl("周", "週次"): "week", _dl("月", "月次"): "month"}
    _WINDOW = {"day": 30, "week": 12, "month": 12}
    _METRICS = {
        "ROAS": ("roas", "%.2f"),
        _dl("消耗$", "消化$"): ("cost_usd", "%.2f"),
        "CPA$": ("cpa", "%.2f"),
        _dl("转化数", "CV数"): ("conversions", "%.1f"),
        _dl("点击", "クリック"): ("clicks", "%d"),
        "CTR": ("ctr", "%.4f"),
    }

    c1, c2 = st.columns([1, 2])
    with c1:
        _gl = st.radio(_dl("粒度", "粒度"), list(_GRAINS), horizontal=True)
    with c2:
        _ml = st.selectbox(_dl("指标", "指標"), list(_METRICS))
    grain, (mcol, mfmt) = _GRAINS[_gl], _METRICS[_ml]

    # campaign 维度先按天合并成账户级,再重采样(比率由聚合后的分子分母重算)
    _daily_acct = ads.groupby("report_date", as_index=False)[
        ["cost_usd", "impressions", "clicks", "conversions", "conversions_value"]
    ].sum(min_count=1)
    ts = resample(_daily_acct, grain, today=pd.Timestamp(today))
    ts = ts.tail(_WINDOW[grain])

    if ts.empty or ts[mcol].isna().all():
        st.info(_dl("该粒度下暂无可用数据。", "この粒度では利用可能なデータがありません。"))
    else:
        _n = len(ts)
        _start = pd.to_datetime(_daily_acct["report_date"]).min()
        st.caption(_dl(
            f"数据起始 {_start:%Y-%m-%d} · 当前 {_n} 个数据点"
            + ("" if grain == "day" else _dl(" · 未走完的当期已剔除", " · 進行中の当期は除外")),
            f"データ開始 {_start:%Y-%m-%d} · {_n} ポイント",
        ))
        if _n < 3:
            st.warning(_dl(
                f"⚠️ 仅 {_n} 个数据点,**不足以判读趋势**。marketing 数据自 2026-07 起采集,"
                "月粒度需数月才有意义。",
                f"⚠️ {_n} ポイントのみ。**トレンド判読には不十分**。",
            ))

        plot = ts.copy()
        plot["thin_sample"] = plot["conversions"].fillna(0) < SAMPLE_MIN_CONVERSIONS
        _title = f"{_ml} · {_gl}"
        base = alt.Chart(plot)
        line = base.mark_line(point=False, color="#4C78A8").encode(
            x=alt.X("period:T", title=None),
            y=alt.Y(f"{mcol}:Q", title=_ml, scale=alt.Scale(zero=False)),
        )
        # 样本不足的点视觉降级——不让噪音点和可信点长得一样(doc 23 §3.5)
        pts = base.mark_point(filled=True, size=70).encode(
            x="period:T", y=f"{mcol}:Q",
            color=alt.condition(alt.datum.thin_sample, alt.value("#BBBBBB"), alt.value("#4C78A8")),
            tooltip=[alt.Tooltip("period:T", title=_dl("期间", "期間")),
                     alt.Tooltip(f"{mcol}:Q", title=_ml, format=mfmt.replace("%", "")),
                     alt.Tooltip("conversions:Q", title=_dl("转化", "CV"), format=".1f"),
                     alt.Tooltip("cost_usd:Q", title=_dl("消耗$", "消化$"), format=".2f")],
        )
        layers = [line, pts]
        if mcol == "roas" and _be:
            rule = alt.Chart(pd.DataFrame({"y": [_be]})).mark_rule(
                color="#E45756", strokeDash=[6, 4]).encode(y="y:Q")
            layers.append(rule)
        st.altair_chart(alt.layer(*layers).properties(height=320, title=_title),
                        use_container_width=True)
        if mcol == "roas":
            st.caption(_dl(
                f"红色虚线 = 保本 ROAS {_be:.2f}x(侧栏毛利率决定)· "
                f"灰点 = 该期转化 < {SAMPLE_MIN_CONVERSIONS},判定不可信",
                f"赤破線 = 損益分岐 ROAS {_be:.2f}x · グレー点 = CV < {SAMPLE_MIN_CONVERSIONS}",
            ))
        with st.expander(_dl("查看聚合后数据", "集計データを表示")):
            st.dataframe(ts, hide_index=True, width="stretch")
    st.caption(_dl(
        "比率类指标(ROAS/CPA/CTR)按「Σ分子÷Σ分母」聚合,非日值平均——低消耗日不会被过度加权。",
        "比率指標は「Σ分子÷Σ分母」で集計(日次平均ではない)。",
    ))

d14 = today - dt.timedelta(days=14)


def _daily(df: pd.DataFrame, cols_map: dict[str, str]) -> pd.DataFrame:
    sub = df[df["report_date"] >= d14]
    if sub.empty:
        return pd.DataFrame(columns=["report_date", *cols_map.values()])
    g = sub.groupby("report_date").agg({k: "sum" for k in cols_map}).reset_index()
    return g.rename(columns=cols_map)


with tab_multi:
    st.caption(_dl("近14日 · 各自归因口径,不相加", "直近14日 · 各帰属口径・合算しない"))
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

with tab_gap:
    st.caption(_dl("近14日 · 差异是两套归因系统的正常现象,解释它而非强行对齐;持续大差 → 检查埋码/结账域名",
                   "直近14日 · 差異は 2 つの帰属システム間で正常。継続的な大差 → 計測タグ/チェックアウトドメイン確認"))
    g14 = gap[gap["report_date"] >= d14].sort_values("report_date", ascending=False)
    st.dataframe(g14, hide_index=True, width="stretch", column_config={
        "report_date": st.column_config.DateColumn(_dl("日期", "日付")),
        "ga4_transactions": "GA4 transactions",
        "shopify_orders": _dl("Shopify订单", "Shopify注文"),
        "ga4_revenue": "GA4 revenue",
        "shopify_net_revenue": _dl("Shopify净收入", "Shopify純売上"),
        "transaction_gap": st.column_config.NumberColumn(_dl("差(GA4−Shopify)", "差(GA4−Shopify)")),
    })
