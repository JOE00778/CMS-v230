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

# 履歴（全量·通貨で絞り込み）
with st.expander(t("📜 汇率履歴（全量）")):
    _sel = st.multiselect(
        t("货币（留空=全部）"),
        sorted(df["tx_currency"].dropna().unique().tolist()), default=[], key="fx_cur")
    _h = df if not _sel else df[df["tx_currency"].isin(_sel)]
    _h = _h.sort_values(["effective_date", "tx_currency"], ascending=[False, True])
    _hist = pd.DataFrame({
        t("ソース通貨"): _h["tx_currency"],
        t("為替レート"): _h["exchange_rate"].map(
            lambda v: f"{v:g}" if pd.notna(v) else "—"),
        t("発効日"): _h["effective_date"].dt.strftime("%Y-%m-%d"),
    })
    st.dataframe(_hist, hide_index=True, use_container_width=True, height=420)
    st.download_button(t("📥 汇率履歴 CSV"), _hist.to_csv(index=False).encode("utf-8-sig"),
                       file_name="currency_rate_history.csv", mime="text/csv", key="fx_csv")
