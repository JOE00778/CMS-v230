"""模块 #36 三金汇率 · NST 為替レート表示（毎月1日自動更新）.

nst.currency_rate（database 仓 pull_currency_rates.py が毎月1日 06:58 JST に
NST currencyrate を全量ミラー · pull_schedule job=currency_monthly）を表示する。
基準通貨=日本円 × ソース通貨 × 発効日。「いつ更新されたか」（取得時刻）を明示。
Boss 2026-07-07。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.forex import usd_export_rate
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("三金汇率"), page_icon="💱", layout="wide")
from shared.auth import require_password  # noqa: E402
require_password()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
lang_selector()
conn = get_readonly_connection()

st.title(t("💱 三金汇率（NST 為替レート）"))
st.caption(t("每月 1 日 06:58 JST 从 NST（currencyrate）自动全量更新。基準通貨=日本円。"))


def _read(sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return pd.DataFrame()


df = _read("SELECT base_currency, tx_currency, exchange_rate, effective_date, pulled_at "
           "FROM nst.currency_rate")
if df.empty:
    st.info(t("暂无汇率数据（每月 1 日自动拉取；急需时在 pull_schedule 触发 currency_monthly）"))
    st.stop()

df["exchange_rate"] = pd.to_numeric(df["exchange_rate"], errors="coerce")
df["effective_date"] = pd.to_datetime(df["effective_date"], errors="coerce")
df["pulled_at"] = pd.to_datetime(df["pulled_at"], errors="coerce", utc=True)

# 国名の後に英語名を付記（Boss 2026-07-08）· NST 名称は部分一致でマップ
_EN_NAMES = [
    ("フィリピン", "Philippine Peso (PHP)"),
    ("ブラジル", "Brazilian Real (BRL)"),
    ("ベトナム", "Vietnamese Dong (VND)"),
    ("マレーシア", "Malaysian Ringgit (MYR)"),
    ("シンガポール", "Singapore Dollar (SGD)"),
    ("人民元", "Chinese Yuan (CNY)"),
    ("台湾ドル", "New Taiwan Dollar (TWD)"),
    ("大韓民国ウォン", "South Korean Won (KRW)"),
    ("日本円", "Japanese Yen (JPY)"),
    ("泰銖", "Thai Baht (THB)"),
    ("泰铢", "Thai Baht (THB)"),
    ("米ドル", "US Dollar (USD)"),
]


def _with_en(name: str) -> str:
    for _ja, _en in _EN_NAMES:
        if _ja in name:
            return f"{name} / {_en}"
    return name


df["tx_currency"] = df["tx_currency"].astype(str).map(_with_en)


def _jst(ts) -> str:
    try:
        return ts.tz_convert("Asia/Tokyo").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


# 最新レート = 通貨ごとに発効日最大
latest = (df.sort_values("effective_date")
          .drop_duplicates(subset=["base_currency", "tx_currency"], keep="last")
          .sort_values("tx_currency").reset_index(drop=True))
_pulled = df["pulled_at"].max()

k1, k2, k3 = st.columns(3)
k1.metric(t("货币数"), f"{latest['tx_currency'].nunique()}")
k2.metric(t("最新発効日"), str(latest["effective_date"].max().date()))
k3.metric(t("数据取得时刻(JST)"), _jst(_pulled) if pd.notna(_pulled) else "—")

st.markdown("##### " + t("📌 最新汇率一览"))
# 米ドルは進口（NST 原値）/ 出口（公司口径 · shared/forex.py）の 2 本立て（Boss 2026-07-08）
_is_usd = latest["tx_currency"].astype(str).str.contains("米ドル|USD")
_cur_ym = pd.Timestamp.today().strftime("%Y-%m")
_show = pd.DataFrame({
    t("ソース通貨"): latest["tx_currency"].where(~_is_usd, latest["tx_currency"] + t("（进口）")),
    t("為替レート"): latest["exchange_rate"].map(
        lambda v: f"{v:g}" if pd.notna(v) else "—"),
    t("発効日"): latest["effective_date"].dt.strftime("%Y-%m-%d"),
    t("取得时刻(JST)"): latest["pulled_at"].map(_jst),
})
if _is_usd.any():
    _usd_name = str(latest.loc[_is_usd, "tx_currency"].iloc[0])
    _show = pd.concat([_show, pd.DataFrame([{
        t("ソース通貨"): _usd_name + t("（出口）"),
        t("為替レート"): f"{usd_export_rate(_cur_ym):g}",
        t("発効日"): "2026-04-01",
        t("取得时刻(JST)"): t("公司口径（非NST）"),
    }])], ignore_index=True).sort_values(t("ソース通貨")).reset_index(drop=True)
st.dataframe(_show, hide_index=True, use_container_width=True)
st.caption(t("為替レート=1 外貨あたりの日本円 · 発効日=NST 上のレート適用開始日 · "
             "取得时刻=CMS が NST から取り込んだ時刻"))
st.caption(t("💡 米ドルは 2 本立て：进口=NST currencyrate 原值（輸入側）· "
             "出口=公司口径 150（2026-04 由 145 调整 · shared/forex.py · CMS 各模块换算用出口）"))

# 月度履歴（近12个月 · 通貨×月のマトリクス · 更新の無い月は直前レートを継続適用）
st.markdown("##### " + t("📜 月度汇率（近 12 个月）"))
_months = pd.period_range(end=pd.Timestamp.today().to_period("M"), periods=12, freq="M")
_rows = []
for _cur, _g in df.dropna(subset=["effective_date"]).groupby("tx_currency"):
    _g = _g.sort_values("effective_date")
    _row = {"tx_currency": _cur}
    for _m in _months:
        _appl = _g[_g["effective_date"] <= _m.to_timestamp(how="end")]
        _row[str(_m)] = (float(_appl["exchange_rate"].iloc[-1])
                         if not _appl.empty and pd.notna(_appl["exchange_rate"].iloc[-1])
                         else None)
    _rows.append(_row)
# 米ドル行は「進口」に改名し、公司口径の「出口」行を追加（145→150 · 2026-04 調整）
for _r in _rows:
    if "米ドル" in str(_r["tx_currency"]) or "USD" in str(_r["tx_currency"]):
        _exp = {"tx_currency": str(_r["tx_currency"]) + t("（出口）")}
        for _m in _months:
            _exp[str(_m)] = usd_export_rate(str(_m))
        _r["tx_currency"] = str(_r["tx_currency"]) + t("（进口）")
        _rows.append(_exp)
        break
_piv = pd.DataFrame(_rows).sort_values("tx_currency").reset_index(drop=True)
_piv_disp = _piv.rename(columns={"tx_currency": t("ソース通貨")})
for _c in [str(m) for m in _months]:
    _piv_disp[_c] = _piv_disp[_c].map(lambda v: f"{v:g}" if pd.notna(v) else "—")
st.dataframe(_piv_disp, hide_index=True, use_container_width=True)
st.caption(t("各月=月末时点适用中的汇率（按発効日·当月无更新则沿用上一次汇率）"))
st.download_button(t("📥 月度汇率 CSV"), _piv_disp.to_csv(index=False).encode("utf-8-sig"),
                   file_name="currency_rate_monthly.csv", mime="text/csv", key="fx_csv")
