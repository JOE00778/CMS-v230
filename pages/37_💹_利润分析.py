"""模块 #37 利润分析 · 市場別 月次損益（Boss 2026-07-14 依頼）.

サイドバー「店铺毛利」直下。3 タブ構成（Boss 2026-07-14）:
- 🏬 平台      = 東南亜/韓国/日本 × 損益項目（市場ごとに右隣へ 占比=対総収益 列 + 合計列）
- 🌐 自建站    = smikiejapan 自社サイト単独（NST 入りまで ¥0）
- 📦 輸出      = 全体 1 列 · 市場別なし · 全社口径費用（物流費/固定費 手入力）はここのみ計上
各タブ KPI = 純利益額 / 純利益率（そのタブの範囲）。

口径（Boss 2026-07-14）:
- 総収益     = NST 当月営業額（nst.sales_daily.revenue · 前日確定分まで · page05 同口径）
- 仕入金額   = 定義原価（revenue − gross_profit · NST 円丸め順）
- 物流費     = 頁内折りたたみで Boss 手入力（finance.fixed_cost.logistics_cost · 月別 · 全社口径）
- 広告費     = 暫定空欄（未接続）
- 固定費     = 人件費 + 管理費 + 本社配賦（頁内折りたたみで Boss 手入力 · 単一金額 ·
  finance.fixed_cost 月別 · 全社口径 → 市場別へは未配賦、合計純利益のみ計上 · Boss 2026-07-14）
- 市場別人数 = 頁内折りたたみで Boss 手入力（finance.market_headcount · ym×市場 · 記録のみ、
  現時点で損益計算には未使用）
- 決済手数料 = 店舗控除合計（coupang.settlement · 現状韓国のみ · KRW→JPY 当月三金レート）
  東南亜/日本/自建站は暫定空欄。
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang
from shared.markets import (ALL_MARKETS, MARKET_KOREA, MARKET_UNKNOWN,
                            add_market_column)

st.set_page_config(page_title=t("利润分析"), page_icon="💹", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme, html_table
require_password()
inject_theme()
lang_selector()
conn = get_connection()

_ja = get_lang() == "ja"


def _dl(zh: str, ja: str) -> str:
    return ja if _ja else zh


st.title(t("💹 利润分析"))
st.caption(_dl(
    "市场别月次损益 · 总收益/采购金额 = NST 日次销售（截至前日确定分 · 定义原价口径）· "
    "支付手续费 = 店铺扣减总费用（现仅 Coupang 韩国店 · KRW 按当月三金汇率换算 JPY）· "
    "物流费/固定费用=页内折叠项手动输入（全公司口径·仅计入合计净利）· "
    "广告费及东南亚·日本·自建站的扣减暂空，待接入",
    "市場別月次損益 · 総収益/仕入金額 = NST 日次売上（前日確定分まで · 定義原価ベース）· "
    "決済手数料 = 店舗控除合計（現状 Coupang 韓国店のみ · KRW は当月三金レートで円換算）· "
    "物流費/固定費=頁内折りたたみで手入力（全社口径·合計純利益のみ計上）· "
    "広告費と東南亜·日本·自建站の控除は暫定空欄（未接続）",
))


def _rhu(v: float) -> float:
    """円丸め HALF_UP(away from zero)= NST 表示丸め（page05 と同一）。"""
    import math
    return float(math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5))


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
# 対象月
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

ym = st.selectbox(t("対象月"), months_df["ym"].tolist())

# ============================================================
# 総収益 / 定義原価（市場別 · nst.sales_daily）
# ============================================================
df, e2 = _query(
    "SELECT s.shop, s.sale_date, s.revenue, s.gross_profit "
    "FROM nst.sales_daily s "
    "WHERE to_char(s.sale_date,'YYYY-MM') = ?",
    (ym,),
)
if e2:
    st.error(e2)
    st.stop()
if df is None or df.empty:
    st.info(t("この条件のデータがありません"))
    st.stop()

df["revenue"] = df["revenue"].astype(float)
df["gross_profit"] = df["gross_profit"].astype(float)
df["defined_cost"] = df["revenue"] - df["gross_profit"]   # NetSuite 口径の原価
# 未来日付除外 + 当日は未確定（07:00 に前日分取得）→ 前日まで（page05 と同口径）
_today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
_cutoff = _today - dt.timedelta(days=1)
df = df[df["sale_date"] <= _cutoff]
if df.empty:
    st.info(t("確定済みの売上データがまだありません（前日分は翌07:00に取得）"))
    st.stop()
df = add_market_column(df, store_col="shop")

_g = df.groupby("market").agg(revenue=("revenue", "sum"),
                              defined_cost=("defined_cost", "sum"))
revenue = {m: _rhu(float(_g.loc[m, "revenue"])) if m in _g.index else 0.0
           for m in ALL_MARKETS}
cost = {m: _rhu(float(_g.loc[m, "defined_cost"])) if m in _g.index else 0.0
        for m in ALL_MARKETS}

# ============================================================
# 決済手数料 = 店舗控除合計（coupang.settlement · 韓国のみ · KRW→JPY）
# ============================================================
_DED_COLS = ["service_fee", "seller_service_fee", "seller_discount_coupon",
             "downloadable_coupon", "store_fee_discount", "courantee_fee",
             "courantee_customer_reward", "deduction_amount",
             "debt_of_last_week", "dedicated_delivery_amount"]


@st.cache_data(ttl=300, show_spinner=False)
def _coupang_ver() -> str:
    """coupang 域の缓存版本（coupang.pull_schedule.last_run_at 最大値 · page05 と同一）。"""
    try:
        row = get_connection().execute(
            "SELECT max(last_run_at) FROM coupang.pull_schedule").fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        pass
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")


def _cq(sql: str, params: tuple = ()):
    try:
        from shared.cache import cached_df
        return cached_df(conn, sql, params or None, ver=_coupang_ver()), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


fee: dict[str, float | None] = {m: None for m in ALL_MARKETS}   # None = 暫定空欄（表示 "—"）
_fee_note = ""
cdf, cerr = _cq(
    "SELECT " + ", ".join(_DED_COLS) +
    " FROM coupang.settlement WHERE revenue_recognition_year_month = ?",
    (str(ym),),
)
if cerr:
    _fee_note = _dl("⚠️ coupang.settlement 未接続（支付手续费按空计）: ",
                    "⚠️ coupang.settlement 未接続（決済手数料は空欄扱い）: ") + cerr
elif cdf is None or cdf.empty:
    _fee_note = _dl(
        f"ℹ️ {ym} 的 Coupang 结算单尚未生成（认知月结束后出单）· 支付手续费暂无",
        f"ℹ️ {ym} の Coupang 結算単は未生成（認識月終了後に出単）· 決済手数料は未計上")
else:
    for _c in _DED_COLS:
        cdf[_c] = pd.to_numeric(cdf[_c], errors="coerce").fillna(0.0)
    _krw_sum = float(cdf[_DED_COLS].sum().sum())
    from shared.forex import nst_monthly_rates
    _rate = nst_monthly_rates(conn, "KRW", [str(ym)])[str(ym)]
    fee[MARKET_KOREA] = _rhu(_krw_sum * _rate)
    _fee_note = _dl(
        f"支付手续费 = Coupang 结算单扣减合计 ₩{_krw_sum:,.0f} × {_rate:g}（{ym} 三金汇率换算 JPY）",
        f"決済手数料 = Coupang 結算控除合計 ₩{_krw_sum:,.0f} × {_rate:g}（{ym} 三金レートで円換算）")

# ============================================================
# 固定費（Boss 手入力 · 月別 単一金額 · finance.fixed_cost · 全社口径 → 合計のみ計上）
# ============================================================
def _ensure_fixed_cost() -> str | None:
    """幂等建表（page34 と同パターン）。失敗は文字列で返す（固定費は補助機能 · 頁は止めない）。"""
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS finance")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS finance.fixed_cost ("
            "ym TEXT PRIMARY KEY, amount NUMERIC(14,2) NOT NULL DEFAULT 0, "
            "updated_at TIMESTAMPTZ DEFAULT NOW())")
        conn.execute("ALTER TABLE finance.fixed_cost "
                     "ADD COLUMN IF NOT EXISTS amount NUMERIC(14,2)")
        conn.execute("ALTER TABLE finance.fixed_cost "
                     "ADD COLUMN IF NOT EXISTS logistics_cost NUMERIC(14,2)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS finance.market_headcount ("
            "ym TEXT, market TEXT, headcount NUMERIC(10,2), "
            "updated_at TIMESTAMPTZ DEFAULT NOW(), PRIMARY KEY (ym, market))")
        conn.commit()
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        return str(e)
    # 2026-07-14 当日の3列細分版(人工费/管理费/本社配额)からの移行: 合算→amount 後に旧列削除。
    # 旧列が無い(新規/移行済)と UPDATE が失敗する → 無視して次へ。
    try:
        conn.execute(
            "UPDATE finance.fixed_cost SET amount = "
            "COALESCE(labor_cost,0)+COALESCE(admin_cost,0)+COALESCE(hq_allocation,0) "
            "WHERE amount IS NULL OR amount = 0")
        conn.execute("ALTER TABLE finance.fixed_cost DROP COLUMN IF EXISTS labor_cost")
        conn.execute("ALTER TABLE finance.fixed_cost DROP COLUMN IF EXISTS admin_cost")
        conn.execute("ALTER TABLE finance.fixed_cost DROP COLUMN IF EXISTS hq_allocation")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return None


_fx_err = _ensure_fixed_cost()
fx_total: float | None = None      # 固定費（手入力）
lg_total: float | None = None      # 物流費（手入力）
if not _fx_err:
    try:
        _r = conn.execute(
            "SELECT amount, logistics_cost FROM finance.fixed_cost WHERE ym = ?",
            (str(ym),)).fetchone()
        if _r is not None:
            if _r["amount"] is not None:
                fx_total = float(_r["amount"])
            if _r["logistics_cost"] is not None:
                lg_total = float(_r["logistics_cost"])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
_hc: dict[str, float] = {}         # 市場別 人数（手入力）
if not _fx_err:
    try:
        for _hr in conn.execute(
                "SELECT market, headcount FROM finance.market_headcount "
                "WHERE ym = ?", (str(ym),)).fetchall():
            if _hr["headcount"] is not None:
                _hc[_hr["market"]] = float(_hr["headcount"])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass

# ============================================================
# 純利益 = 総収益 − 仕入金額 − 物流費(空=0) − 広告費(空=0) − 決済手数料(空=0)
#          − 固定費(全社口径 · 市場別へは未配賦 → 合計のみ)
# ============================================================
net = {m: revenue[m] - cost[m] - (fee[m] or 0.0) for m in ALL_MARKETS}
tot_rev = sum(revenue.values())
tot_cost = sum(cost.values())
_fee_vals = [v for v in fee.values() if v is not None]
tot_fee = sum(_fee_vals)
tot_net = tot_rev - tot_cost - tot_fee - (fx_total or 0.0) - (lg_total or 0.0)
tot_margin = (tot_net / tot_rev * 100) if tot_rev else 0.0

# ============================================================
# 3 タブ（Boss 2026-07-14: 平台=市場別 / 自建站=単独 / 輸出=全体·市場別なし·最後）
#   物流費(手入力)/固定費は全社口径 → 輸出タブのみ計上。
# ============================================================
_item_lbl = _dl("项目", "項目")
_tot_lbl = _dl("合计", "合計")
_pct_lbl = _dl("占比", "構成比")
PLATFORM_MKTS = [m for m in ALL_MARKETS if m != MARKET_UNKNOWN]

# 市場名ヘッダ居中（Boss 2026-07-14 · html_table の th.num=右対齐をこのページのみ上書き）
st.markdown("<style>.cms-table thead th.num{text-align:center;}</style>",
            unsafe_allow_html=True)


def _yen(v: float) -> str:
    return f"¥{v:,.0f}"


def _pct(v: float, base: float) -> str:
    return f"{v / base * 100:.2f}%" if base else "—"


def _pnl_rows(mkts: list, total_label: str | None):
    """市場列の損益表（平台/自建站タブ用 · 全社口径費用は含めない）。

    返り値: (rows, cols, scope_net, scope_rev)。total_label=None なら合計列なし。
    """
    s_rev = sum(revenue[m] for m in mkts)
    s_cost = sum(cost[m] for m in mkts)
    s_fee_vals = [fee[m] for m in mkts if fee[m] is not None]
    s_fee = sum(s_fee_vals)
    s_net = s_rev - s_cost - s_fee

    def row(label, nums, tot):
        r = [label]
        for m in mkts:
            v = nums.get(m)
            r += ["—", "—"] if v is None else [_yen(v), _pct(v, revenue[m])]
        if total_label is not None:
            r += ["—", "—"] if tot is None else [_yen(tot), _pct(tot, s_rev)]
        return r

    rows = [
        row(_dl("总收益", "総収益"), revenue, s_rev),
        row(_dl("采购金额（定义原价）", "仕入金額（定義原価）"), cost, s_cost),
        row(_dl("物流费", "物流費"), {}, None),
        row(_dl("广告费", "広告費"), {}, None),
        row(_dl("支付手续费", "決済手数料"),
            {m: v for m, v in fee.items() if v is not None},
            s_fee if s_fee_vals else None),
        row(_dl("净利额", "純利益額"), {m: net[m] for m in mkts}, s_net),
    ]
    cols = [_item_lbl]
    for m in mkts:
        cols += [m, _pct_lbl]
    if total_label is not None:
        cols += [total_label, _pct_lbl]
    return rows, cols, s_net, s_rev


tab_pf, tab_own, tab_all = st.tabs([
    _dl("🏬 平台", "🏬 プラットフォーム"),
    _dl("🌐 自建站", "🌐 自社サイト"),
    _dl("📦 输出", "📦 輸出"),
])

# ── 平台タブ（東南亜/韓国/日本 + 合計）──
with tab_pf:
    _rows, _cols, _s_net, _s_rev = _pnl_rows(PLATFORM_MKTS, _tot_lbl)
    _k1, _k2, _, _ = st.columns(4)
    _k1.metric(_dl("平台净利额", "プラットフォーム純利益額"), f"¥{_s_net:,.0f}")
    _k2.metric(_dl("平台净利率", "プラットフォーム純利益率"),
               f"{(_s_net / _s_rev * 100) if _s_rev else 0:.2f}%")
    html_table(pd.DataFrame(_rows, columns=_cols))
    if _fee_note:
        st.caption(_fee_note)
    st.caption(_dl(
        "物流费/固定费用为全公司口径 → 只计入「输出」tab · 占比 = 各项 ÷ 该市场总收益"
        "（净利额行的占比即净利率）",
        "物流費/固定費は全社口径 → 「輸出」タブのみ計上 · 構成比 = 各項目 ÷ 当該市場の総収益"
        "（純利益行の構成比 = 純利益率）"))

# ── 自建站タブ（単独）──
with tab_own:
    _rows, _cols, _s_net, _s_rev = _pnl_rows([MARKET_UNKNOWN], None)
    _k1, _k2, _, _ = st.columns(4)
    _k1.metric(_dl("自建站净利额", "自社サイト純利益額"), f"¥{_s_net:,.0f}")
    _k2.metric(_dl("自建站净利率", "自社サイト純利益率"),
               f"{(_s_net / _s_rev * 100) if _s_rev else 0:.2f}%")
    html_table(pd.DataFrame(_rows, columns=_cols))
    st.caption(_dl(
        "数据源与平台 tab 相同（NST 日次销售）· 自建站(Shopify)销售进入 NST 前此处为 ¥0",
        "データ源はプラットフォームタブと同一（NST 日次売上）· 自社サイト(Shopify)売上が"
        "NST に入るまでは ¥0"))

# ── 輸出タブ（全体 · 市場別なし · 全社口径費用ここに計上）──
with tab_all:
    _k1, _k2, _, _ = st.columns(4)
    _k1.metric(_dl("净利额", "純利益額"), f"¥{tot_net:,.0f}")
    _k2.metric(_dl("净利率", "純利益率"), f"{tot_margin:.2f}%")

    def _arow(label: str, v: float | None) -> list:
        return [label] + (["—", "—"] if v is None else [_yen(v), _pct(v, tot_rev)])

    _rows = [
        _arow(_dl("总收益", "総収益"), tot_rev),
        _arow(_dl("采购金额（定义原价）", "仕入金額（定義原価）"), tot_cost),
        _arow(_dl("物流费", "物流費"), lg_total),
        _arow(_dl("广告费", "広告費"), None),
        _arow(_dl("支付手续费", "決済手数料"), tot_fee if _fee_vals else None),
        _arow(_dl("固定费用", "固定費"), fx_total),
        _arow(_dl("净利额", "純利益額"), tot_net),
    ]
    html_table(pd.DataFrame(_rows, columns=[_item_lbl, _dl("整体", "全体"), _pct_lbl]))
    st.caption(_dl(
        "固定费用 = 人工费 + 管理费 + 本社配额 · 物流费/固定费用在下方折叠项输入（全公司口径）",
        "固定費 = 人件費 + 管理費 + 本社配賦 · 物流費/固定費は下の折りたたみで入力（全社口径）"))
    st.caption(_dl(
        "净利额 = 总收益 − 采购金额 − 物流费 − 广告费 − 支付手续费 − 固定费用（暂空项按 0 计）· "
        "占比 = 各项 ÷ 整体总收益",
        "純利益額 = 総収益 − 仕入金額 − 物流費 − 広告費 − 決済手数料 − 固定費（空欄は 0 扱い）· "
        "構成比 = 各項目 ÷ 全体総収益",
    ))

# ============================================================
# 固定費入力（折りたたみ · Boss 手入力 · 対象月単位で保存）
# ============================================================
with st.expander(_dl("✏️ 固定费用输入", "✏️ 固定費入力")):
    if _fx_err:
        st.warning(_dl("finance.fixed_cost 初始化失败（PG 未接続？）: ",
                       "finance.fixed_cost 初期化失敗（PG 未接続？）: ") + _fx_err)
    else:
        st.caption(_dl(
            f"对象月 {ym} · 固定费用 = 人工费 + 管理费 + 本社配额 · "
            "各市场人数一并保存 · 按月保存，切换对象月后各自独立",
            f"対象月 {ym} · 固定費 = 人件費 + 管理費 + 本社配賦 · "
            "市場別人数も同時保存 · 月単位で保存（対象月ごとに独立）"))
        _fc1, _fc2 = st.columns(2)
        _amt = _fc1.number_input(
            _dl("固定费用（円）", "固定費（円）"), min_value=0.0,
            value=(fx_total or 0.0), step=10000.0, format="%.0f",
            key=f"fx_amt_{ym}")
        _lg = _fc2.number_input(
            _dl("物流费（円）", "物流費（円）"), min_value=0.0,
            value=(lg_total or 0.0), step=10000.0, format="%.0f",
            key=f"fx_lg_{ym}")
        _hcols = st.columns(len(ALL_MARKETS))
        _hc_in: dict[str, float] = {}
        for _hcol, _m in zip(_hcols, ALL_MARKETS):
            _hc_in[_m] = _hcol.number_input(
                f"{_m} " + _dl("人数", "人数"), min_value=0.0,
                value=_hc.get(_m, 0.0), step=1.0, format="%.0f",
                key=f"fx_hc_{_m}_{ym}")
        if st.button(_dl("💾 保存", "💾 保存"), key=f"fx_save_{ym}"):
            try:
                conn.execute(
                    "INSERT INTO finance.fixed_cost (ym, amount, logistics_cost, updated_at) "
                    "VALUES (?, ?, ?, NOW()) "
                    "ON CONFLICT (ym) DO UPDATE SET "
                    "amount = EXCLUDED.amount, "
                    "logistics_cost = EXCLUDED.logistics_cost, "
                    "updated_at = NOW()",
                    (str(ym), _amt, _lg))
                for _m, _v in _hc_in.items():
                    conn.execute(
                        "INSERT INTO finance.market_headcount "
                        "(ym, market, headcount, updated_at) "
                        "VALUES (?, ?, ?, NOW()) "
                        "ON CONFLICT (ym, market) DO UPDATE SET "
                        "headcount = EXCLUDED.headcount, updated_at = NOW()",
                        (str(ym), _m, _v))
                conn.commit()
                st.rerun()   # 保存後 KPI/表を新固定費で再計算
            except Exception as e:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:
                    pass
                st.error(_dl("保存失败: ", "保存失敗: ") + str(e))
