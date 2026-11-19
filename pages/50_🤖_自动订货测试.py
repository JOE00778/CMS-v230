"""模块 #50 · 🤖 自动订货（测试）（Boss 2026-11-19「把这个放到 CMS 中，新做一个自动订货板块，用于测试」）

规则与回测：docs/analysis/订货规则回测_*（z慢2.0 + 周巡检 · 90 天日均 · 只看 Shopee/Lazada/Coupang · 库存只看弁天）
只读：不建 PO、不写 PG。逻辑在 shared/auto_order_engine。
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import streamlit as st

from shared import auto_order_engine as E
from shared.auth import require_admin
from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t
from shared.theme import inject_theme

st.set_page_config(page_title=t("自动订货测试"), page_icon="🤖", layout="wide")
require_admin()
inject_theme()
lang_selector()
st.title(t("🤖 自动订货（测试）"))
st.caption(t("按回测定稿规则（z慢 + 周巡检）用线上最新销量/库存/在途算：今天该补什么、下次定期单多少钱、往后几个月怎么订。"
             "只算不下单；销量只取 Shopee/Lazada/Coupang，库存只看弁天倉庫。"))

with st.sidebar:
    st.subheader(t("参数"))
    zslow = st.number_input(t("z慢（A/B 无现货渠道倍数）"), 1.0, 4.0, 2.0, 0.5)
    zcard = st.number_input(t("z现货（A/B 有カード仕入れ渠道倍数）"), 0.5, 3.0, 1.0, 0.5)
    trend = st.selectbox(t("需求口径"), [1.0, 1.25], index=0,
                         format_func=lambda x: t("90 天日均") if x == 1.0 else t("max(90 天, 30 天×1.25)"))
    H = st.number_input(t("巡检视野（天）"), 7, 30, 12, 1)
    order_day = st.number_input(t("每月定期订货日"), 1, 28, 1, 1)
    fwd_days = st.number_input(t("前推天数"), 60, 240, 150, 30)
    buffer_days = st.number_input(t("保险日（到货闭环）"), 0, 14, 5, 1)
    st.markdown("---")
    plan_txt = st.text_area(t("计划月销（活动/上新前录入）"), placeholder="JAN,月销件数\n4901234567890,600",
                            help=t("覆盖该品的日均需求（取大）。回测证明这是补掉促销尖峰断货的唯一办法。"))
    run = st.button(t("🔄 重新计算"), type="primary")

plan: dict[str, float] = {}
for line in (plan_txt or "").splitlines():
    parts = [x.strip() for x in line.replace("\t", ",").split(",") if x.strip()]
    if len(parts) >= 2:
        try: plan[parts[0]] = float(parts[1])
        except ValueError: pass


@st.cache_data(ttl=3600, show_spinner=t("从 PG 取数…"))
def _inputs(today: date) -> dict:
    return E.load_inputs(get_readonly_connection(), today)


if run: _inputs.clear()
inp = _inputs(date.today())
params = dict(zslow=zslow, zcard=zcard, trend=trend, H=int(H), order_day=int(order_day), fwd_days=int(fwd_days), buffer_days=int(buffer_days))
with st.spinner(t("计算中…")):
    res = E.compute(inp, params, plan)
df, fwd, s = res["items"], res["forward"], E.summary(res)
wan = lambda x: f"{x / 1e4:,.0f}万"
st.caption(t("库存快照 {s} · 销量到 {d} · 品数 {n}（A/B/C 有销量、排除 OEM）· 下次定期订货日 {nx}").format(
    s=inp["snap_date"], d=inp["sales_max"], n=s["n"], nx=res["next_day"]))

if not inp.get("brand_ok", True):
    st.warning(t("只读账号没有 brand schema 权限：交期按供货商级中位计算（未到供货商×品牌）。授权后自动恢复。"))
c1, c2, c3, c4 = st.columns(4)
c1.metric(t("A/B 手持（原价）"), wan(s["oh_ab"]), help=t("弁天倉庫 × 定義原価"))
c2.metric(t("A/B 在途"), wan(s["pipe_ab"]), t("含审批保留 {x}").format(x=wan(s["pending_ab"])))
c3.metric(t("今天巡检触发 A/B"), wan(s["emerg_amt"]), t("{n} SKU · 现货渠道 {c} SKU {a}").format(
    n=s["emerg_sku"], c=s["emerg_card_sku"], a=wan(s["emerg_card_amt"])))
c4.metric(t("下次定期单 A/B"), wan(s["next_ab_amt"]), t("{n} SKU · C 另 {c}").format(n=s["next_ab_sku"], c=wan(s["next_c_amt"])))

tab1, tab2, tab3, tab4, tab5 = st.tabs([t("🚨 今日巡检"), t("📅 下次定期单"), t("📈 前推"), t("📋 逐品明细"), t("🚚 到货闭环")])
SHOW = ["jan", "name", "rank", "vendor", "card", "planned", "L", "sL", "lot", "d", "d30", "d90", "cost", "oh", "pipe", "pending",
        "days_cover", "oh_next", "S", "q_next", "amt_next", "trig_now", "q_emerg", "amt_emerg"]

with tab1:
    em = df[df.trig_now & (df["rank"] != "C")].sort_values("amt_emerg", ascending=False)
    st.write(t("未来 {h} 天内预计跌破安全线的 A/B：{n} SKU · {a}。有现货渠道的走 5 天紧急单；其余是把定期单提前。").format(
        h=int(H), n=len(em), a=wan(em.amt_emerg.sum())))
    g = em.groupby("vendor").agg(SKU=("item", "size"), 金额=("amt_emerg", "sum")).sort_values("金额", ascending=False)
    g["金额"] = g["金额"].map(wan); st.dataframe(g, use_container_width=True)
    st.dataframe(em[["jan", "name", "rank", "vendor", "card", "L", "d", "oh", "pipe", "days_cover", "q_emerg", "amt_emerg"]],
                 use_container_width=True, hide_index=True)

with tab2:
    nx = df[df.q_next > 0].sort_values("amt_next", ascending=False)
    ab = nx[nx["rank"] != "C"]
    st.write(t("{d} 定期单：A/B {n} SKU {a}；C {nc} SKU {ac}（在今天的提前单之前算，若今天已下单则扣减）").format(
        d=res["next_day"], n=len(ab), a=wan(ab.amt_next.sum()), nc=len(nx) - len(ab), ac=wan(nx[nx["rank"] == "C"].amt_next.sum())))
    g = nx.groupby("vendor").agg(SKU=("item", "size"), 金额=("amt_next", "sum"),
                                 AB金额=("amt_next", lambda x: x[nx.loc[x.index, "rank"] != "C"].sum())).sort_values("金额", ascending=False)
    for c in ("金额", "AB金额"): g[c] = g[c].map(wan)
    st.dataframe(g, use_container_width=True)
    st.dataframe(nx[["jan", "name", "rank", "vendor", "L", "lot", "d", "oh", "pipe", "oh_next", "S", "q_next", "amt_next"]],
                 use_container_width=True, hide_index=True)

with tab3:
    if fwd.empty:
        st.info(t("无 A/B 需求，无法前推"))
    else:
        f = fwd.copy(); f["月"] = pd.to_datetime(f.date).dt.to_period("M").astype(str)
        st.write(t("需求按当前日均恒定、交期取中位（无抖动）。A/B 月销原价 {m}，稳态后每月订货额应与之接近。").format(m=wan(s["month_cogs_ab"])))
        m = f.groupby("月").agg(次数=("amt", "size"), SKU=("sku", "sum"), 金额=("amt", "sum")); m["金额"] = m["金额"].map(wan)
        st.dataframe(m, use_container_width=True)
        f["金额"] = f.amt.map(wan)
        st.dataframe(f[["date", "kind", "sku", "vendors", "金额"]].rename(columns={"date": t("日期"), "kind": t("性质"), "vendors": t("供货商数")}),
                     use_container_width=True, hide_index=True)

with tab4:
    st.dataframe(df[SHOW], use_container_width=True, hide_index=True)
    buf = io.BytesIO(); df[SHOW].to_csv(buf, index=False, encoding="utf-8-sig")
    st.download_button(t("⬇️ 下载逐品 CSV"), buf.getvalue(), f"auto_order_{date.today()}.csv", "text/csv")

with tab5:
    trk = E.po_tracking(inp, res, params)
    if trk.empty:
        st.info(t("没有未受领的 PO 行"))
    else:
        st.write(t("全部未受领 PO 行（不限来源）。计划到货日 = NST 入荷予定日，没填的按供货商交期；"
                   "最晚决策日 = 预计断货日 − 第二货源交期 − 保险日 {b} 天。到了最晚决策日还没到货 → 🔴 直接在第二货源下单（给出建议量）。").format(b=int(buffer_days)))
        cnt = trk.state.value_counts()
        keys = ["⛔ 已断货", "🔴 转第二货源", "🟠 盯第一货源", "🟡 迟到", "📦 今日应到", "🚚 在途"]
        c = st.columns(len(keys))
        for k, key in enumerate(keys):
            sub = trk[trk.state == key]; c[k].metric(key, f"{len(sub)} 行", wan(sub.amount.sum()))
        red = trk[trk.state.isin(["🔴 转第二货源", "⛔ 已断货"]) & (trk.q_alt > 0)]
        if len(red):
            st.error(t("🔴 {n} 行建议立即在第二货源下单，合计 {a}（原价）").format(n=len(red), a=wan(red.amt_alt.sum())))
        cols = ["state", "po_number", "vendor", "jan", "name", "rank", "po_date", "eta", "eta_src", "late_days", "open_qty", "amount",
                "oh", "d", "stockout", "decide_by", "alt_vendor", "alt_L", "q_alt", "amt_alt"]
        st.dataframe(trk[cols].rename(columns={"state": t("状态"), "eta": t("计划到货"), "eta_src": t("来源"), "late_days": t("迟到天"),
                                              "stockout": t("预计断货"), "decide_by": t("最晚决策日"), "alt_vendor": t("第二货源"),
                                              "alt_L": t("第二交期"), "q_alt": t("第二货源建议量")}), use_container_width=True, hide_index=True)
        buf2 = io.BytesIO(); trk.to_csv(buf2, index=False, encoding="utf-8-sig")
        st.download_button(t("⬇️ 下载到货闭环 CSV"), buf2.getvalue(), f"po_tracking_{date.today()}.csv", "text/csv")
