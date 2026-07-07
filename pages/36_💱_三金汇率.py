"""模块 #36 三金汇率 · NST 為替レート表示（毎月1日自動更新）.

nst.currency_rate（database 仓 pull_currency_rates.py が毎月1日 06:58 JST に
NST currencyrate を全量ミラー · pull_schedule job=currency_monthly）を表示する。
基準通貨=日本円 × ソース通貨 × 発効日。「いつ更新されたか」（取得時刻）を明示。
Boss 2026-07-07。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("三金汇率"), page_icon="💱", layout="wide")
from shared.auth import require_password  # noqa: E402
require_password()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
lang_selector()
conn = get_connection()

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
_show = pd.DataFrame({
    t("ソース通貨"): latest["tx_currency"],
    t("為替レート"): latest["exchange_rate"].map(
        lambda v: f"{v:g}" if pd.notna(v) else "—"),
    t("発効日"): latest["effective_date"].dt.strftime("%Y-%m-%d"),
    t("取得时刻(JST)"): latest["pulled_at"].map(_jst),
})
st.dataframe(_show, hide_index=True, use_container_width=True)
st.caption(t("為替レート=1 外貨あたりの日本円 · 発効日=NST 上のレート適用開始日 · "
             "取得时刻=CMS が NST から取り込んだ時刻"))

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
_piv = pd.DataFrame(_rows).sort_values("tx_currency").reset_index(drop=True)
_piv_disp = _piv.rename(columns={"tx_currency": t("ソース通貨")})
for _c in [str(m) for m in _months]:
    _piv_disp[_c] = _piv_disp[_c].map(lambda v: f"{v:g}" if pd.notna(v) else "—")
st.dataframe(_piv_disp, hide_index=True, use_container_width=True)
st.caption(t("各月=月末时点适用中的汇率（按発効日·当月无更新则沿用上一次汇率）"))
st.download_button(t("📥 月度汇率 CSV"), _piv_disp.to_csv(index=False).encode("utf-8-sig"),
                   file_name="currency_rate_monthly.csv", mime="text/csv", key="fx_csv")
