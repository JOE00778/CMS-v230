"""模块 #6 店铺毛利 · NST API 売上データ（nst.sales_daily）ベース.

2026-05-20 全面改写：旧 sales_line（手動 Excel 時代）→ NST API 日次データへ。
店舗 × 月 の粗利を集計し、月内の日次推移を曲線で可視化（Boss 2026-05-20 依頼）。

データ源:
- nst.sales_daily      店舗 × SKU × 日 の売上（販売数量 / 総収益 JPY）
- nst.item_master_raw  商品マスタ（定義原価 cost_estimate / 表示名 / メーカー / ランク）

表示:
- 月選択 + 市場フィルタ
- 月内日次の粗利推移（曲線図）+ 店舗別 / 市場別 / TOP SKU 集計
- 粗利 = 総収益 − 定義原価(=cost_estimate×数量) / 粗利率 = 粗利/総収益
"""
from __future__ import annotations

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.editor_state import lang_stable_editor
from shared.i18n import lang_selector, t, get_lang
from shared.markets import ALL_MARKETS, add_market_column
from shared.owners import (OWNER_NONE, add_owner_column, has_owner,
                          load_owner_map)
from shared import price_alert as pa

st.set_page_config(page_title=t(PAGE_TITLE), page_icon=PAGE_ICON, layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme, html_table
require_password()
inject_theme()
lang_selector()
conn = get_readonly_connection()

# 担当者マッピングは「設定 UI から書く」ため書き込み接続が別に要る。
# 売上等の参照は上の cms_reader のまま（Boss 2026-07-27 収権指示を崩さない）。
from shared.db import get_connection
from shared.owners import OWNER_TABLE as _OWNER_TBL
conn_w = get_connection()


def _ensure_owner_schema() -> str | None:
    """ops.shop_owner 幂等建表（PG 前提 · 本地 SQLite では失敗して基線運用に落ちる）。"""
    try:
        conn_w.execute("CREATE SCHEMA IF NOT EXISTS ops")
        conn_w.execute(
            "CREATE TABLE IF NOT EXISTS ops.shop_owner ("
            "shop TEXT NOT NULL, "
            "effective_ym TEXT NOT NULL, "        # 'YYYY-MM' 発効開始月
            "owner TEXT, "                        # 空 = その月から担当者なし
            "updated_at TIMESTAMPTZ DEFAULT NOW(), "
            "PRIMARY KEY (shop, effective_ym))")
        conn_w.commit()
        return None
    except Exception as e:  # noqa: BLE001
        try:
            conn_w.rollback()
        except Exception:
            pass
        return str(e)


_owner_schema_err = _ensure_owner_schema()


def _read_w(sql: str, params: tuple = ()) -> pd.DataFrame:
    """書き込み接続側の読み（ops.shop_owner 用）。失敗は空 DF + rollback。"""
    try:
        cur = conn_w.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame([tuple(r) for r in rows], columns=cols)
    except Exception:
        try:
            conn_w.rollback()
        except Exception:
            pass
        return pd.DataFrame()


st.title(t("🏪 店铺毛利"))
# データ更新日時（sales_daily 最新 pulled_at · UTC→JST）
try:
    _u = conn.execute("SELECT max(pulled_at) AS u FROM nst.sales_daily").fetchone()
    _uval = _u["u"] if _u else None
except Exception:
    try:
        conn.rollback()
    except Exception:
        pass
    _uval = None
if _uval is not None:
    _uval = _uval.astimezone(dt.timezone(dt.timedelta(hours=9)))
    _upd_str = _uval.strftime("%Y-%m-%d %H:%M")
else:
    _upd_str = "—"
_upd_lbl = "データ更新" if get_lang() == "ja" else "数据更新"
st.caption(t(
    "NST API 売上データ（日次）· 店舗×月の粗利集計 + 月内日次推移 曲線 · "
    "粗利/粗利率 自動計算（定義原価ベース）· "
    "CM = 粗利 − 手数料（注文突合済控除）− 広告費（Shopee Ads 消耗自動 + 手入力）"
) + f" · {_upd_lbl}: {_upd_str}")

# 列見出し: (中文, 日本語) — UI 言語追従
_LBL = {
    "shop":          ("店铺", "FB_店舗"),
    "owner":         ("店铺负责人", "担当者"),
    "market":        ("市场", "市場"),
    "sale_date":     ("日期", "日付"),
    "display_name":  ("显示名", "表示名"),
    "maker":         ("厂商", "メーカー名"),
    "item_rank":     ("商品等级", "商品ランク"),
    "qty":           ("销售数量", "販売数量"),
    "revenue":       ("总收益(NST)", "総収益(NST)"),
    "cancel_est":    ("取消冲减估", "取消控除見込"),
    "revenue_adj":   ("调整后营业额", "調整後売上"),
    "defined_cost":  ("定义原价", "定義原価"),
    "gross_profit":  ("毛利", "粗利"),
    "gross_margin":  ("毛利率", "粗利率"),
    "n_shop":        ("店铺数", "店舗数"),
    "n_sku":         ("SKU数", "SKU数"),
    "fee":           ("手续费", "手数料"),
    "ad":            ("广告费", "広告費"),
    "cm":            ("CM", "CM"),
    "cm_rate":       ("CM率", "CM率"),
    "cm_est":        ("CM(日估)", "CM(日次推定)"),
    "fee_match":     ("费用匹配率", "突合率"),
    "payout_rate":   ("回款率", "入金率"),
    # 業績タブ（2026-09-14 JO · 財務の statement 表と同じ費目名）
    "commission":    ("佣金", "販売手数料"),
    "service":       ("服务费", "サービス費"),
    "transaction":   ("交易费", "取引手数料"),
    "ams":           ("AMS佣金", "AMS 手数料"),
    "voucher":       ("卖家优惠券", "セラークーポン"),
    "ship3pl":       ("第三方物流费", "第三者物流費"),
    "buyer_ship":    ("买家支付运费(−)", "買い手支払運費(−)"),
    "rebate":        ("Shopee运费回扣(−)", "Shopee 運費回扣(−)"),
    "other_ded":     ("其他(税费·退货运费)", "その他(税・返品運賃)"),
    "ded_total":     ("扣减合计", "控除合計"),
    "shop_gp":       ("店铺毛利", "店舗粗利"),
    "jd":            ("京东费用", "JD 費用"),
    "ecms":          ("ECMS运费", "ECMS 輸送費"),
    "perf":          ("运营绩效金额", "運営業績額"),
    "perf_rate":     ("绩效率", "業績率"),
    "refund":        ("退款金额(单列)", "返金額(別掲)"),
    "offline":       ("线下调整(单列)", "線下調整(別掲)"),
}

# ============================================================
# 平台分类（Boss 2026-09-07: 筛选加「按平台」）
#   店名から解析。v_shipped_settlement の platform より細かく
#   （Amazon / Yahoo / Shopify も分ける）。
# ============================================================
ALL_PLATFORMS = ["Shopee", "Lazada", "Coupang", "Amazon", "Yahoo",
                 "Shopify", "Other"]


def _classify_platform(shop) -> str:
    s = str(shop or "").strip()
    sl = s.lower()
    if "coupang" in sl:
        return "Coupang"
    if s.startswith("Shopee"):
        return "Shopee"
    if s.startswith("Lazada"):
        return "Lazada"
    if sl.startswith("shopify"):
        return "Shopify"
    if "amazon" in sl:
        return "Amazon"
    if "ヤフー" in s or "yahoo" in sl:
        return "Yahoo"
    return "Other"


_MONEY = {"revenue", "defined_cost", "gross_profit", "fee", "ad", "cm",
          "cancel_est", "revenue_adj", "cm_est",
          "commission", "service", "transaction", "ams", "voucher", "ship3pl",
          "buyer_ship", "rebate", "other_ded", "ded_total", "shop_gp", "jd", "ecms",
          "perf", "refund", "offline"}
_PCT = {"gross_margin", "cm_rate", "perf_rate"}
_INT = {"qty", "n_shop", "n_sku"}
# 环比対象（相対%）。gross_margin は _PCT で百分点(pp)环比、n_shop/n_sku は構造カウントで环比なし。
_MOM_VALUE_COLS = {"qty", "revenue", "defined_cost", "gross_profit"}
# _PCT のうち环比(pp)を付ける列。cm_rate は上月側の再計算が要るため対象外（第1版）。
_MOM_PCT_COLS = {"gross_margin"}


def _col(key: str) -> str:
    return _LBL[key][1] if get_lang() == "ja" else _LBL[key][0]


def _mom_suffix(cur, key, metric, prev_idx, *, pp: bool = False) -> str:
    """整月环比サフィックス「(±X.X%)」。金額の下段に改行表示（Boss 2026-09-07）。

    上月データなし/0 は「(—)」。pp=True は百分点(pp)差。
    改行 \\n は html_table 側で <br> に変換される。"""
    if prev_idx is None or key not in prev_idx.index:
        return "\n(—)"
    prev = prev_idx.loc[key, metric]
    if pd.isna(prev) or prev == 0:
        return "\n(—)"
    if pp:
        return f"\n({cur - prev:+.1f}pp)"
    return f"\n({(cur - prev) / abs(prev) * 100:+.1f}%)"


def _disp(g: pd.DataFrame, cols: tuple, *, mom_prev=None, dim: str = None) -> pd.DataFrame:
    """聚合 DataFrame → 双语列名 + 金额/比率/整数格式化（表示専用）。

    mom_prev(index=dim の上月 dim 别集計) を渡すと、各値指標に「 (±%)」整月环比を内嵌
    （金额/数量=相対%、毛利率=百分点pp）。num 列右对齐は html_table 側で処理。
    """
    d = g[list(cols)].copy()
    keys = list(g[dim]) if (mom_prev is not None and dim) else None
    if "market" in d.columns:  # 市場名（shared.markets の中文定数）は表示だけ訳す
        d["market"] = d["market"].map(t)
    for c in cols:
        if c in _MONEY:
            if keys is not None and c in _MOM_VALUE_COLS:
                d[c] = [f"¥{v:,.0f}{_mom_suffix(v, k, c, mom_prev)}"
                        for v, k in zip(d[c], keys)]
            else:
                d[c] = d[c].apply(lambda x: f"¥{x:,.0f}")
        elif c in _PCT:
            if keys is not None and c in _MOM_PCT_COLS:
                d[c] = [f"{v:.2f}%{_mom_suffix(v, k, c, mom_prev, pp=True)}"
                        for v, k in zip(d[c], keys)]
            else:
                d[c] = d[c].apply(lambda x: f"{x:.2f}%")
        elif c in _INT:
            if keys is not None and c in _MOM_VALUE_COLS:
                d[c] = [f"{int(v):,}{_mom_suffix(v, k, c, mom_prev)}"
                        for v, k in zip(d[c], keys)]
            else:
                d[c] = d[c].apply(lambda x: f"{int(x):,}")
    d.columns = [_col(c) for c in cols]
    return d


# 柱状图字号（全页统一·缩小）。st.bar_chart 不支持字号参数，横向粗利图改用 Altair 才能控字号。
_CHART_LABEL_FS = 10
_CHART_TITLE_FS = 11


def _hbar(g: pd.DataFrame, dim: str):
    """gross_profit 横向条形图（小字号·主题靛蓝·按值降序），替代 st.bar_chart 以控字号。"""
    gp_lbl = _col("gross_profit")
    h = max(160, len(g) * 24)
    return (
        alt.Chart(g.assign(market=g["market"].map(t)) if dim == "market" else g)
        .mark_bar(color="#4F46E5")
        .encode(
            x=alt.X("gross_profit:Q", title=None, axis=None),  # 底部数值刻度删除(表格/tooltip 已有精确值)
            y=alt.Y(f"{dim}:N", title=None, sort="-x"),
            tooltip=[
                alt.Tooltip(f"{dim}:N", title=_col(dim)),
                alt.Tooltip("gross_profit:Q", title=gp_lbl, format=",.0f"),
            ],
        )
        .properties(height=h)
        .configure_axis(labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
    )


def _prev_month(ym: str) -> str:
    """'YYYY-MM' → 上一个月 'YYYY-MM'。"""
    cur = dt.datetime.strptime(ym, "%Y-%m")
    prev = cur.replace(day=1) - dt.timedelta(days=1)
    return prev.strftime("%Y-%m")


def _agg_prev(prev_ym: str, mk_sel: str, dim: str, max_day: int = 31,
              owner_map: dict | None = None,
              pf_sel: list | None = None) -> pd.DataFrame:
    """上月 dim别 集計（index=dim）。环比の分母用。dim∈{'owner','shop'}。空なら空DF。

    环比口径(Boss 2026-05-25):
      - 完结の過去月 → max_day=31 で整月対整月。
      - 当月(未完结) → max_day=当月の最終データ日(=前一日) で同日範囲対比。
    """
    dfp, _ = _query(
        "SELECT trim(s.shop) AS shop, s.sale_date, s.qty_sold, s.revenue, s.gross_profit "
        "FROM nst.sales_daily s "
        "WHERE to_char(s.sale_date,'YYYY-MM') = ?",
        (prev_ym,),
    )
    if dfp is None or dfp.empty:
        return pd.DataFrame()
    dfp = dfp[pd.to_datetime(dfp["sale_date"]).dt.day <= max_day]
    if dfp.empty:
        return pd.DataFrame()
    for c in ("qty_sold", "revenue", "gross_profit"):
        dfp[c] = dfp[c].astype(float)
    dfp["defined_cost"] = dfp["revenue"] - dfp["gross_profit"]
    dfp = add_market_column(dfp, store_col="shop")
    # 環比の分母も「その月の担当者」で引く（当月の変更が過去月を動かさない）
    dfp = add_owner_column(dfp, shop_col="shop", owner_map=owner_map)
    if mk_sel:
        dfp = dfp[dfp["market"].isin(mk_sel)]
    if pf_sel:
        dfp = dfp[dfp["shop"].map(_classify_platform).isin(pf_sel)]
    dfp = dfp[dfp["owner"].map(has_owner)]   # 担当者未設定の店舗は集計対象外
    if dfp.empty:
        return pd.DataFrame()
    g = dfp.groupby(dim, as_index=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
    )
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    return g.set_index(dim)


def _rhu(v: float) -> float:
    """円丸め HALF_UP(away from zero)= NST 表示丸め。Python round は banker's のため不可。"""
    import math
    return float(math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5))


def _ns_round_money(g: pd.DataFrame) -> pd.DataFrame:
    """NST 報表の表示丸め順を再現:収益/定義原価を先に円丸め → 粗利=差。

    粗利を直接丸めると原価合計が半円(.5)境界の時に NST と ±1 円ズレる
    (2026-06 Shopee SG で実証·Boss 100%一致指示)。"""
    g = g.copy()
    if "defined_cost" not in g.columns:
        g["defined_cost"] = g["revenue"] - g["gross_profit"]
    g["revenue"] = g["revenue"].map(_rhu)
    g["defined_cost"] = g["defined_cost"].map(_rhu)
    g["gross_profit"] = g["revenue"] - g["defined_cost"]
    return g


def _query(sql: str, params: tuple = ()):
    try:
        from shared.cache import cached_df, data_version
        return cached_df(conn, sql, params or None, ver=data_version("basic", "sales")), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


# ============================================================
# データ有無チェック
# ============================================================
months_df, err = _query(
    "SELECT DISTINCT to_char(sale_date,'YYYY-MM') AS ym "
    "FROM nst.sales_daily ORDER BY ym DESC"
)
if err:
    st.error(t("売上テーブル未取得 or 接続エラー: ") + err)
    st.info(t("page 27「📥 NST 取得データ」→ 手動更新 で sales を実行してください。"))
    st.stop()
if months_df is None or months_df.empty:
    st.warning(t("⚠️ 日次売上データ未取得。page 27「📥 NST 取得データ」で sales ジョブを実行してください。"))
    st.stop()

# ============================================================
# フィルタ
# ============================================================
c1, c2, c3 = st.columns([1, 1.5, 1.5])
ym = c1.selectbox(t("対象月"), months_df["ym"].tolist())
mk = c2.multiselect(t("市場"), ALL_MARKETS, placeholder=t("全部市场"), format_func=t)  # 空選＝全部市场
pf = c3.multiselect("プラットフォーム" if get_lang() == "ja" else "平台",
                    ALL_PLATFORMS,
                    placeholder="全プラットフォーム" if get_lang() == "ja" else "全部平台")  # 空選＝全部

# ============================================================
# クエリ（当月の日次明細 + 商品マスタ join）
# ============================================================
df, e2 = _query(
    "SELECT trim(s.shop) AS shop, s.sale_date, s.item_internal_id, "
    "im.display_name, im.maker, im.item_rank, "
    "s.qty_sold, s.revenue, "
    "s.gross_profit "
    "FROM nst.sales_daily s "
    "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
    "WHERE to_char(s.sale_date,'YYYY-MM') = ? "
    "ORDER BY s.sale_date",
    (ym,),
)
if e2:
    st.error(e2)
    st.stop()
if df is None or df.empty:
    st.info(t("この条件のデータがありません"))
    st.stop()

df["qty_sold"] = df["qty_sold"].astype(float)
df["revenue"] = df["revenue"].astype(float)
df["gross_profit"] = df["gross_profit"].astype(float)    # 報表口径 粗利(=売上−行級定義原価·2026-07-10)
df["defined_cost"] = df["revenue"] - df["gross_profit"]  # NetSuite 口径の原価（売上−毛利）
# 未来日付除外 + 当日は未確定（07:00 に前日分取得）のため除外：前日まで
_today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
_cutoff = _today - dt.timedelta(days=1)
df = df[df["sale_date"] <= _cutoff]
if df.empty:
    # 当月初日など、確定データ（前日分まで）がまだ無いケース
    st.info(t("確定済みの売上データがまだありません（前日分は翌07:00に取得）"))
    st.stop()
df = add_market_column(df, store_col="shop")

# 担当者マッピング（対象月時点 / 上月時点）。設定は発効月以降だけに効く。
_omap = load_owner_map(conn_w, ym)
df = add_owner_column(df, shop_col="shop", owner_map=_omap)

# ★ 担当者が設定されていない店舗は、このページの全計算から除外する
#   （Boss 2026-08-27: 総収益 KPI・市場統計・日次推移・TOP SKU・環比、いずれにも入れない）
_no_owner = sorted(df.loc[~df["owner"].map(has_owner), "shop"].unique())
# 除外分は捨てずに保持 → 市場タブ最下部で「担当者なし」として一本に集計して見せる
# （数字が消えるのではなく、主計算に混ざらないだけ · Boss 2026-08-27）
# 市場フィルタは掛けない：市場統計から外した店舗なので市場で絞る意味が無い
_df_noowner = df[~df["owner"].map(has_owner)].copy()
df = df[df["owner"].map(has_owner)]
if df.empty:
    st.warning(t("⚠️ 担当者が設定されている店舗の売上データがありません。"
                 "下の「👤 店铺负责人」タブ最下部の設定から担当者を割り当ててください。"))
    st.stop()

df["platform"] = df["shop"].map(_classify_platform)
if mk:
    df = df[df["market"].isin(mk)]
    if df.empty:
        st.info(t("この市場のデータがありません"))
        st.stop()
if pf:
    df = df[df["platform"].isin(pf)]
    if df.empty:
        st.info(("このプラットフォームのデータがありません" if get_lang() == "ja"
                 else "该平台筛选下无数据"))
        st.stop()

# 上月集計（环比の分母）· 担当者別 / 店舗別
# 当月(=今の暦月)は未完结 → 上月も「当月の最終データ日(前一日)」まで同日範囲で対比。
# 完结の過去月は整月対整月(max_day=31)。
_real_cur_ym = _today.strftime("%Y-%m")
_max_day = int(pd.to_datetime(df["sale_date"]).dt.day.max()) if ym == _real_cur_ym else 31
_prev_ym = _prev_month(ym)
_omap_prev = load_owner_map(conn_w, _prev_ym)
_prev_owner = _agg_prev(_prev_ym, mk, "owner", _max_day, owner_map=_omap_prev,
                        pf_sel=pf)
_prev_shop = _agg_prev(_prev_ym, mk, "shop", _max_day, owner_map=_omap_prev,
                       pf_sel=pf)

# ============================================================
# CM（Boss 2026-09-07 考核口径）
#   CM = 粗利（NST 売上 − 定義原価） − 手数料 − 広告費
#   - 手数料 = nst.v_shipped_settlement の deduction_jpy + shipping_jpy
#     （注文番号で突合済 · 調整金額込み ·「店铺扣减」タブの控除合計と同一口径）
#   - 広告費 = finance.ad_spend_monthly（チャージ月 × 店舗 · 手入力 · 消耗ではない）
#     外貨は当月 NST 三金レートで円換算（shared.forex.nst_monthly_rates）
#   粒度は 月×店舗。担当者/市場へは金額を先に合算してから率を出す。
# ============================================================
_AD_TBL = "finance.ad_spend_monthly"
_AD_CURRENCIES = ("JPY", "PHP", "SGD", "MYR", "THB", "VND", "TWD", "USD", "KRW")


def _ensure_ad_schema() -> str | None:
    """広告費テーブル幂等作成（ops.shop_owner と同パターン · PG 前提）。"""
    try:
        conn_w.execute("CREATE SCHEMA IF NOT EXISTS finance")
        conn_w.execute(
            f"CREATE TABLE IF NOT EXISTS {_AD_TBL} ("
            "ym TEXT NOT NULL, "                  # 'YYYY-MM' チャージ月
            "shop TEXT NOT NULL, "
            "amount NUMERIC NOT NULL, "           # 現地通貨額
            "currency TEXT NOT NULL DEFAULT 'JPY', "
            "note TEXT, "
            "updated_at TIMESTAMPTZ DEFAULT NOW(), "
            "PRIMARY KEY (ym, shop))")
        conn_w.commit()
        return None
    except Exception as e:  # noqa: BLE001
        try:
            conn_w.rollback()
        except Exception:
            pass
        return str(e)


_ad_schema_err = _ensure_ad_schema()

# --- 手数料（月×店舗 · 注文突合済控除 + 物流控除）---
#     orders/matched = 費用匹配率、settled = 回款率（入金）。
#     Lazada/Other は明細 API 無しで入金判定不能 → 回款率の分母から外す
#     （orders_payable/settled_payable = 判定可能プラットフォームのみの件数）。
_DED_COLS_ALL = ["shop", "fee", "orders", "matched_orders",
                 "orders_payable", "settled_payable"]
_ded_df, _ded_err = _query(
    "SELECT trim(shop) AS shop, platform, deduction_jpy, shipping_jpy, "
    "orders, matched_orders, settled_orders "
    "FROM nst.v_shipped_settlement WHERE ym = ?", (ym,))
if _ded_df is None or _ded_df.empty:
    _ded_df = pd.DataFrame(columns=_DED_COLS_ALL)
else:
    for _c in ("deduction_jpy", "shipping_jpy", "orders",
               "matched_orders", "settled_orders"):
        _ded_df[_c] = pd.to_numeric(_ded_df[_c], errors="coerce").fillna(0).astype(float)
    _ded_df["fee"] = _ded_df["deduction_jpy"] + _ded_df["shipping_jpy"]
    _payable = ~_ded_df["platform"].isin(("Lazada", "Other"))
    _ded_df["orders_payable"] = _ded_df["orders"].where(_payable, 0.0)
    _ded_df["settled_payable"] = _ded_df["settled_orders"].where(_payable, 0.0)
    _ded_df = _ded_df[_DED_COLS_ALL]

# --- 広告費（円換算）---
#   ① 自動源 = Shopee Ads 日次消耗（shopee.ads_daily · 消耗月口径 · Boss 2026-09-08「用2」）
#      country → 店舗名は shopee.v_nst_shop_map、通貨は country 頭2字 → NST 月次レート
#   ② 手入力 = finance.ad_spend_monthly（非 Shopee 渠道用に残す）
#   同一店×月に両方あれば **手入力優先**（二重計上防止 · 手動修正の逃げ道）
from shared.forex import COUNTRY_TO_CURRENCY, nst_monthly_rates

_ad_auto_df, _ad_auto_err = _query(
    "SELECT trim(m.nst_shop) AS shop, upper(left(a.country, 2)) AS cc, "
    "sum(a.expense) AS amount "
    "FROM shopee.ads_daily a "
    "JOIN shopee.v_nst_shop_map m ON m.shop_key = a.country "
    "WHERE to_char(a.perf_date, 'YYYY-MM') = ? AND a.expense IS NOT NULL "
    "GROUP BY 1, 2", (ym,))
_ad_parts = []
if _ad_auto_df is not None and not _ad_auto_df.empty:
    _ad_auto_df["amount"] = pd.to_numeric(
        _ad_auto_df["amount"], errors="coerce").fillna(0).astype(float)
    _auto_rate = {cc: nst_monthly_rates(conn, COUNTRY_TO_CURRENCY.get(cc, cc), [ym])[ym]
                  for cc in _ad_auto_df["cc"].unique()}
    _ad_auto_df["ad"] = [
        _rhu(a * _auto_rate.get(cc, 0.0))
        for a, cc in zip(_ad_auto_df["amount"], _ad_auto_df["cc"])]
    _ad_parts.append(_ad_auto_df.groupby("shop", as_index=False)
                     .agg(ad=("ad", "sum")).assign(_src="auto"))

_ads_raw = _read_w(
    f"SELECT shop, amount, currency, note FROM {_AD_TBL} WHERE ym = ?", (ym,))
if not _ads_raw.empty:
    _ads_raw["shop"] = _ads_raw["shop"].astype(str).str.strip()
    _ads_raw["amount"] = pd.to_numeric(_ads_raw["amount"], errors="coerce").fillna(0).astype(float)
    _rate_by_cur = {c: nst_monthly_rates(conn, c, [ym])[ym]
                    for c in _ads_raw["currency"].astype(str).str.upper().unique()}
    _ads_raw["ad"] = [
        _rhu(a * _rate_by_cur.get(str(c).upper(), 0.0))
        for a, c in zip(_ads_raw["amount"], _ads_raw["currency"])]
    _ad_parts.append(_ads_raw.groupby("shop", as_index=False)
                     .agg(ad=("ad", "sum")).assign(_src="manual"))

if _ad_parts:
    _ad_all = pd.concat(_ad_parts, ignore_index=True)
    # 広告費タブの対照一覧用（自動/手入力を並べる）
    _ad_detail = (_ad_all.pivot_table(index="shop", columns="_src",
                                      values="ad", aggfunc="sum")
                  .reindex(columns=["auto", "manual"]).reset_index())
    # 手入力優先: 同一店に manual があれば auto を捨てる
    _ad_all["_pri"] = (_ad_all["_src"] == "manual").astype(int)
    _ad_all = (_ad_all.sort_values("_pri", ascending=False)
               .drop_duplicates("shop", keep="first"))
    _ad_df = _ad_all[["shop", "ad"]].reset_index(drop=True)
else:
    _ad_detail = pd.DataFrame(columns=["shop", "auto", "manual"])
    _ad_df = pd.DataFrame(columns=["shop", "ad"])

# --- 取消冲减估（月×店舗 · Boss 2026-09-08 A案）---
#     NST は出荷で売上計上、取消の控除（貸方票）は Shopee 財務報表待ちで
#     1〜2 ヶ月遅れる（Boss 確認済の構造的タイムラグ）。
#     予估池 = 平台側既知の取消（出荷前 preship_void + 出荷後 loss）のうち
#     **まだ貸方票が起票されていない注文だけ**（has_credit=負票行あり=起票済 → 除外）。
#     金額 = 正票行の nst_amount_jpy（請求書按分）。
#     ⚠️ 起票済注文を「正負相殺」で残すと按分残差が乗る：負票は注文実額・
#        正票行は合併票の均攤値で、取消注文（高客単ほど拒収されやすい）は
#        票均値より高く、月合計が負に振れる（2026-05 実測 −¥88万）。
#        起票済は NST 売上に反映済みなので推定から丸ごと外すのが正しい。
#     注文の帰属月 = min(ym)（出荷正票の月）。財務が補票すると当該注文が
#     除外に回り本列が減り、NST 売上が下がり、調整後売上はほぼ動かない。
# ⚠️ v_shipped_order 全量集計は ~36s → 2026-11-20 物化 nst.mv_shipped_order（日次 06:00/08:30 全量リフレッシュ）を読む。
#    超時を握り潰すと「取消冲减估 ¥0」になり調整後売上/CM が過大に出る → この 1 本だけ延長し、失敗は表示する。
#    ponytail: 本質対策はビューの事前集計（database 側）。結果は data_version で日次キャッシュ。
try:
    conn.execute("SET statement_timeout = '120s'")
except Exception:
    pass
_cxl_df, _cxl_err = _query(
    "WITH o AS ("
    "  SELECT trim(shop) AS shop, order_no, min(ym) AS ym0, "
    "         sum(coalesce(nst_amount_jpy,0)) "
    "           FILTER (WHERE nst_amount_jpy > 0) AS pos_amt, "
    "         bool_or(nst_amount_jpy < 0) AS has_credit "
    "  FROM nst.mv_shipped_order "
    "  WHERE settle_status IN ('loss','preship_void') "
    "  GROUP BY 1, 2"
    ") "
    "SELECT shop, sum(coalesce(pos_amt,0)) AS cancel_est "
    "FROM o WHERE ym0 = ? AND NOT has_credit "
    "GROUP BY 1", (ym,))
try:
    conn.execute("SET statement_timeout = DEFAULT")
except Exception:
    pass
if _cxl_err:
    st.error(("取消控除見込の取得に失敗（0 として扱えないため調整後売上/CM は参考値）: "
              if get_lang() == "ja" else
              "取消冲减估取数失败（调整后营业额/CM 仅供参考）: ") + _cxl_err)
if _cxl_df is None or _cxl_df.empty:
    _cxl_df = pd.DataFrame(columns=["shop", "cancel_est"])

# per-shop CM 素材（fee/ad/取消估 + 突合/入金件数を店舗単位で持つ · 各タブが merge して集計）
_cmb = pd.merge(_ded_df, _ad_df, on="shop", how="outer")
_cmb = pd.merge(_cmb, _cxl_df, on="shop", how="outer")
for _c in ("fee", "ad", "orders", "matched_orders",
           "orders_payable", "settled_payable", "cancel_est"):
    _cmb[_c] = pd.to_numeric(_cmb.get(_c), errors="coerce").fillna(0.0)

# 手数料明細カバレッジ（対象 = 本ページ主計算の店舗 · Σmatched/Σorders）
_scope_shops = set(df["shop"].unique())
_dd_scope = _ded_df[_ded_df["shop"].isin(_scope_shops)]
_cov_o = float(_dd_scope["orders"].sum()) if not _dd_scope.empty else 0.0
_cov_m = float(_dd_scope["matched_orders"].sum()) if not _dd_scope.empty else 0.0
_cov_pct = (_cov_m / _cov_o * 100) if _cov_o else None
# 広告費はあるが当月（担当者あり）売上の無い店舗 = CM に乗らない → 明示
_ad_orphans = sorted(set(_ad_df["shop"]) - _scope_shops) if not _ad_df.empty else []

# 表下の常時表示は「当月の動的シグナル」だけ（短い1行）。口径の定義は折りたたみへ
# （Boss 2026-09-08: 長すぎて乱れる → 分段+折叠）
_CM_WARN = (
    ((f"手数料明細カバレッジ {_cov_pct:.1f}%（100%未満は CM が高く出る）"
      if get_lang() == "ja" else
      f"手续费明细覆盖率 {_cov_pct:.1f}%（低于100%时 CM 偏高）")
     if _cov_pct is not None else "")
    + ((" · ⚠️ 広告費のみで当月売上の無い店舗（未計上）: " if get_lang() == "ja"
        else " · ⚠️ 有广告费但当月无(有负责人)销售、未计入: ")
       + "、".join(_ad_orphans) if _ad_orphans else "")
)

_CM_NOTE_MD_JA = """
- **CM(調整後)** = (粗利 − 取消控除見込 × NST粗利率) − 手数料 − 広告費
- **CM率** = CM ÷ 調整後売上
- **調整後売上** = 総収益(NST) − 取消控除見込
- **取消控除見込**：プラットフォーム側で判明している取消（出荷前＋出荷後）のうち NST 未計上分（純額で按分）。財務が貸方票を起こすと本列が自動で減り、NST 総収益が下がり、調整後売上はほぼ動かない。⚠️ Lazada は取消を取得できず高止まり
- **手数料**：注文番号で突合した控除＋物流控除（調整込み）。「店舗控除」タブと同じ集計基準
- **広告費**：Shopee Ads 日次消耗の月合算（自動・消耗月口径）＋手入力（Shopee 以外のチャネル）。同一店×月は手入力優先
- **突合率**：当月出荷注文のうちプラットフォーム側で費用明細を照合できた割合。100% 未満は CM が高く出る（Lazada は明細 API 無し）
- **入金率**：当月出荷注文のうち着金済みの割合。出荷後 1〜2 週で着金するため月内は低くて正常（Lazada 判定不能＝—）
"""

_CM_NOTE_MD_ZH = """
- **CM(调整后)** = (毛利 − 取消冲减估 × NST毛利率) − 手续费 − 广告费
- **CM率** = CM ÷ 调整后营业额
- **调整后营业额** = 总收益(NST) − 取消冲减估
- **取消冲减估**：平台侧已知取消（出货前＋出货后）中 NST 尚未入账的部分（净额按分）。财务开贷方票后此列自动减少、NST 总收益下降、调整后营业额基本不变。⚠️ Lazada 抓不到取消，其数字仍偏高
- **手续费**：按订单号突合的扣减＋物流扣减（含调整）。与「店铺扣减」tab 同口径
- **广告费**：Shopee Ads 日消耗按月合算（自动・消耗月口径）＋手工录入（非 Shopee 渠道）。同店同月手工优先
- **费用匹配率**：该店当月发货的订单中，能在平台侧查到费用明细的比例。低于 100% 时 CM 偏高（Lazada 无明细 API）
- **回款率**：该月发货的订单中，钱已经到账的比例。发货后 1~2 周到账，月内偏低正常（Lazada 不可判定＝—）
"""


def _cm_note(*, key: str) -> None:
    """表格下の注記: 動的シグナル1行 + 口径説明の折りたたみ。"""
    if _CM_WARN:
        st.caption(_CM_WARN)
    with st.expander("📖 CM 口径の説明" if get_lang() == "ja"
                     else "📖 CM 口径说明", expanded=False):
        st.markdown(_CM_NOTE_MD_JA if get_lang() == "ja" else _CM_NOTE_MD_ZH)


_CM_NUM_COLS = ("fee", "ad", "orders", "matched_orders",
                "orders_payable", "settled_payable", "cancel_est")


def _with_cm(g: pd.DataFrame, dim: str) -> pd.DataFrame:
    """dim 別集計 g（revenue/gross_profit 円丸め済）に CM（調整後口径 · A案）を付ける。

    - cancel_est   取消冲减估（NST 未入账の取消 · 純額按分）
    - revenue_adj  調整後売上 = revenue − cancel_est
    - cm           調整後CM = (粗利 − cancel_est×NST粗利率) − 手数料 − 広告費
                   （取消注文の粗利は当該行の NST 粗利率で折算して冲掉）
    - cm_rate      調整後CM ÷ 調整後売上
    金額・件数を dim へ合算してから率を出す（率の平均はしない）。
    g に shop 列が無い場合は df の shop→dim 対応で dim へ畳む。
    回款率は Lazada/Other（明細 API 無し）を分母から外す。判定可能件数 0 は「—」。
    """
    if dim == "shop":
        m = g.merge(_cmb, on="shop", how="left")
    else:
        _map = df[["shop", dim]].drop_duplicates()
        _fa = _map.merge(_cmb, on="shop", how="inner")
        _fa = _fa.groupby(dim, as_index=False).agg(
            **{c: (c, "sum") for c in _CM_NUM_COLS})
        m = g.merge(_fa, on=dim, how="left")
    for _c in _CM_NUM_COLS:
        m[_c] = pd.to_numeric(m.get(_c), errors="coerce").fillna(0.0)
    m["fee"] = m["fee"].map(_rhu)
    m["ad"] = m["ad"].map(_rhu)
    m["cancel_est"] = m["cancel_est"].map(_rhu)
    m["revenue_adj"] = m["revenue"] - m["cancel_est"]
    _nst_margin = (m["gross_profit"]
                   / m["revenue"].where(m["revenue"] != 0)).fillna(0)
    _gp_adj = m["gross_profit"] - (m["cancel_est"] * _nst_margin).map(_rhu)
    m["cm"] = _gp_adj - m["fee"] - m["ad"]
    m["cm_rate"] = (m["cm"]
                    / m["revenue_adj"].where(m["revenue_adj"] != 0)).fillna(0) * 100
    m["fee_match"] = [
        f"{mo / o * 100:.1f}%" if o else "—"
        for mo, o in zip(m["matched_orders"], m["orders"])]
    m["payout_rate"] = [
        f"{s / o * 100:.1f}%" if o else "—"
        for s, o in zip(m["settled_payable"], m["orders_payable"])]
    return m

# ============================================================
# KPI（総）· A案: NST 値 + 調整後（取消冲减估を反映）+ CM
# ============================================================
tot_r = _rhu(df["revenue"].sum())
tot_c = _rhu(df["defined_cost"].sum())
tot_g = tot_r - tot_c  # NST 表示丸め順(原価丸め→差)·半円境界の±1円ズレ防止
margin = (tot_g / tot_r * 100) if tot_r else 0

_kb = _cmb[_cmb["shop"].isin(set(df["shop"].unique()))]
tot_cancel = _rhu(float(_kb["cancel_est"].sum()))
tot_fee_k = _rhu(float(_kb["fee"].sum()))
tot_ad_k = _rhu(float(_kb["ad"].sum()))
tot_rev_adj = tot_r - tot_cancel
tot_cm = (tot_g - _rhu(tot_cancel * (tot_g / tot_r if tot_r else 0))
          ) - tot_fee_k - tot_ad_k
tot_cm_rate = (tot_cm / tot_rev_adj * 100) if tot_rev_adj else 0

if _no_owner:
    st.caption(("⚠️ 担当者未設定のため集計から除外: " if get_lang() == "ja"
                else "⚠️ 未设定负责人，已从全部计算中剔除：")
               + "、".join(_no_owner))

m1, m2, m3, m4, m5, m6 = st.columns(6)
_ja_kpi = get_lang() == "ja"
m1.metric("総収益(NST)" if _ja_kpi else "总收益(NST)", f"¥{tot_r:,.0f}")
m2.metric("取消控除見込" if _ja_kpi else "取消冲减估", f"¥{tot_cancel:,.0f}",
          delta=None)
m3.metric("調整後売上" if _ja_kpi else "调整后营业额", f"¥{tot_rev_adj:,.0f}")
m4.metric(t("粗利 計"), f"¥{tot_g:,.0f}",
          ("粗利率 " if get_lang() == "ja" else "毛利率 ") + f"{margin:.2f}%",
          delta_color="off")
m5.metric("CM(調整後)" if _ja_kpi else "CM(调整后)", f"¥{tot_cm:,.0f}")
m6.metric("CM率(調整後)" if _ja_kpi else "CM率(调整后)", f"{tot_cm_rate:.2f}%")

# 輸出事業 月次予算 vs 実績（Boss 2026-11-09）· 実績は市場/平台フィルタに関係なく全店 NST 総収益
from shared import sales_budget as _SB  # noqa: E402
try:
    _SB.ensure_schema(conn_w)
    _bud = _SB.load(conn_w).get(ym)
except Exception:  # noqa: BLE001 — 予算表が無くても毛利画面は出す
    try:
        conn_w.rollback()
    except Exception:
        pass
    _bud = None
if _bud:
    _act_df, _ = _query("SELECT SUM(revenue) AS r FROM nst.sales_daily "
                        "WHERE to_char(sale_date, 'YYYY-MM') = ?", (ym,))
    _act = float(_act_df["r"].iloc[0] or 0) if _act_df is not None and not _act_df.empty else 0.0
    b1, b2, b3, _b4 = st.columns(4)
    b1.metric("月次予算" if _ja_kpi else "月度预算", f"¥{_bud:,.0f}")
    b2.metric("実績(NST)" if _ja_kpi else "实绩(NST)", f"¥{_act:,.0f}")
    b3.metric("予算達成率" if _ja_kpi else "预算达成率", f"{_act / _bud * 100:.2f}%")
    st.caption("予算=輸出事業 予実表の予算（システムパラメータ設定で編集）· 実績=当月 NST 総収益（NST「売上予算vs実績_FB」"
               "輸出事業と同口径・フィルタ非連動）" if _ja_kpi else
               "预算=输出事业 预实表的预算（在「系统参数设定」修改）· 实绩=当月 NST 总收益（与 NST「売上予算vs実績_FB」"
               "输出事业同口径·不跟随上方筛选）")

st.divider()

