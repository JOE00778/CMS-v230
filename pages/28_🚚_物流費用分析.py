"""模块 #28 物流費用分析 · JD 物流費用配賦（輸出事業）。

数据源: logistics.cost_monthly（ym × 部署 × 店舗 × 費用種 → 金額/件数）
  · 上传 JD 請求書 + BM(包裹号→店舗) → recompute が join/配賦して書込み。
  · 費用は全て不含税。dept = 輸出 / EC / 不明（包裹未マッチ・店舗未分類）。

Boss 指示: 「以分析表格为核心、只要輸出部门」→ 默认 輸出。不明区は別途 Boss 確認用。
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("物流費用分析"), page_icon="🚚", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🚚 物流費用分析 (JD 配賦)"))
st.caption(t(
    "JD 請求書 → 包裹号で店舗紐付け → 部署別に配賦（費用は不含税）· "
    "梱包費用=Pick&Pack / 国内運送費用=Last mile / 梱包材=Packing Charge"
))

# 費用種：内部キー → 表示名 / 配色
COST_TYPES = [
    ("pickpack", t("梱包費用")),
    ("lastmile", t("国内運送費用")),
    ("packing", t("梱包材")),
]
CT_KEYS = [k for k, _ in COST_TYPES]
CT_LABEL = dict(COST_TYPES)


def _df(sql: str, params=None) -> pd.DataFrame:
    rs = conn.execute(sql, params or {}).fetchall()
    return pd.DataFrame([dict(r) for r in rs])


def _split_name(s: str):
    """'逆向入库费 Return IB-Inspect&Putaway' → ('逆向入库费', 'Return IB…')。"""
    for i, ch in enumerate(s):
        if ch.isascii() and ch.isalpha():
            return s[:i].strip(), s[i:].strip()
    return s.strip(), ""


# ============================================================
# データ取得
# ============================================================
try:
    df = _df(
        "SELECT year_month, dept, shop, cost_type, amount, qty "
        "FROM logistics.cost_monthly"
    )
except Exception as e:
    st.error(t("⚠️ logistics.cost_monthly 読取失败（schema 未部署？）") + f"\n\n{e}")
    st.stop()

if df.empty:
    st.warning(t(
        "⚠️ 物流費用データがまだありません。"
        "「🚚 物流費用上传」で JD 請求書 と BM(包裹号→店舗) をアップロードしてください。"
    ))
    st.stop()

df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)

months = sorted(df["year_month"].dropna().unique().tolist(), reverse=True)


# ============================================================
# 筛选: 月份 + 部署（默认 輸出）
# ============================================================
f1, f2 = st.columns([1.2, 3])
with f1:
    sel_month = st.selectbox(t("月份"), months, index=0)
with f2:
    dept_opts = [t("輸出"), t("EC"), t("合計（全部）")]
    sel_dept_label = st.radio(
        t("部署"), options=dept_opts, index=0, horizontal=True,
        help=t("默认仅輸出（Boss 指示）。不明区在最下方单独列出供检查。"),
    )

if sel_dept_label == t("輸出"):
    base = df[df["dept"] == "輸出"].copy()
elif sel_dept_label == t("EC"):
    base = df[df["dept"] == "EC"].copy()
else:
    base = df[df["dept"] != "不明"].copy()   # 合計 = 輸出+EC（不明は除外）


def _month_total(frame: pd.DataFrame, ym: str) -> float:
    return float(frame[frame["year_month"] == ym]["amount"].sum())


# ============================================================
# KPI（当月 · 選択部署）
# ============================================================
cur = base[base["year_month"] == sel_month]
prev_idx = months.index(sel_month) + 1
prev_month = months[prev_idx] if prev_idx < len(months) else None

total_cur = float(cur["amount"].sum())
total_prev = _month_total(base, prev_month) if prev_month else 0.0
mom = (total_cur / total_prev - 1) * 100 if total_prev else None
by_type = cur.groupby("cost_type")["amount"].sum().to_dict()
parcels = int(cur[cur["cost_type"] == "pickpack"]["qty"].sum())  # 出荷件数 ≒ Pick&Pack

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric(
    t("物流費 合計 (¥)"), f"¥{total_cur:,.0f}",
    delta=(f"{mom:+.1f}% {t('前月比')}" if mom is not None else None),
    delta_color="inverse",
)
for col, (key, label) in zip((k2, k3, k4), COST_TYPES):
    amt = float(by_type.get(key, 0))
    pct = (amt / total_cur * 100) if total_cur else 0
    col.metric(label, f"¥{amt:,.0f}", delta=f"{pct:.1f}%", delta_color="off")
k5.metric(t("出荷件数 (Pick&Pack)"), f"{parcels:,}")

st.divider()


# ============================================================
# 月度趋势图（page05 风格: 点線 + 縦軸 + 縦ルール全値 hover）
# ============================================================
st.subheader(t("📈 物流費 月次推移（{dept}）").format(dept=sel_dept_label))

piv = base.groupby(["year_month", "cost_type"])["amount"].sum().unstack(fill_value=0)
for k in CT_KEYS:
    if k not in piv.columns:
        piv[k] = 0.0
piv["__total__"] = piv[CT_KEYS].sum(axis=1)
piv_r = piv.reset_index().sort_values("year_month")

series = [(k, CT_LABEL[k]) for k in CT_KEYS] + [("__total__", t("合計"))]

if len(piv_r) >= 1:
    _x = alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0))
    _near = alt.selection_point(nearest=True, on="pointerover",
                                fields=["year_month"], empty=False)
    base_c = alt.Chart(piv_r).encode(x=_x)
    lines = [
        base_c.mark_line(point=True).encode(
            y=alt.Y(f"{col}:Q", title=t("金額 (円)")),
            color=alt.datum(label),
        )
        for col, label in series
    ]
    rule = base_c.mark_rule(color="#888").encode(
        opacity=alt.condition(_near, alt.value(0.3), alt.value(0)),
        tooltip=[alt.Tooltip("year_month:N", title=t("月份"))]
        + [alt.Tooltip(f"{col}:Q", title=label, format=",.0f") for col, label in series],
    ).add_params(_near)
    st.altair_chart(
        alt.layer(*lines, rule).properties(height=320)
        .configure_legend(orient="top"),
        use_container_width=True,
    )

st.divider()


# ============================================================
# JD 費用全構成（Billing 対账 · 全費用種 · 店舗/部署分けず）
# ============================================================
st.subheader(t("💴 JD 費用全構成（{ym} · Billing 対账）").format(ym=sel_month))
bill = _df(
    "SELECT seq, item_name, amount_ex_tax, amount_in_tax "
    "FROM logistics.cost_billing WHERE year_month=%(ym)s ORDER BY seq",
    {"ym": sel_month},
)
if bill.empty:
    st.info(t("当月 Billing データなし（請求書アップロード時に自動取込）。"))
else:
    bill["amount_ex_tax"] = pd.to_numeric(bill["amount_ex_tax"], errors="coerce").fillna(0.0)
    bill["amount_in_tax"] = pd.to_numeric(bill["amount_in_tax"], errors="coerce").fillna(0.0)
    b_ex, b_in = bill["amount_ex_tax"].sum(), bill["amount_in_tax"].sum()
    bc1, bc2, bc3 = st.columns(3)
    bc1.metric(t("JD 費用 税別 合計"), f"¥{b_ex:,.0f}")
    bc2.metric(t("税込 合計"), f"¥{b_in:,.0f}")
    bc3.metric(t("費用種 数"), f"{len(bill)}")
    _ALLOC = ("OB-Pick&Pack", "Last mile", "Packing Charge")
    is_alloc = bill["item_name"].apply(lambda s: any(a in s for a in _ALLOC))
    others = bill[~is_alloc].reset_index(drop=True)

    if not others.empty:
        st.markdown(t("###### 🗂️ その他費用（配賦3種以外）"))
        ncol = 4
        recs = list(others.iterrows())
        for base in range(0, len(recs), ncol):
            ccols = st.columns(ncol)
            for j, (_, r) in enumerate(recs[base:base + ncol]):
                cn, en = _split_name(r["item_name"])
                ccols[j].metric(
                    label=cn,
                    value=f"¥{r['amount_ex_tax']:,.0f}",
                    delta=t("税込 ¥{v:,.0f}").format(v=r["amount_in_tax"]),
                    delta_color="off",
                    help=en or None,
                )

    with st.expander(t("📋 全費用明細（配賦3種含む · Billing 対账表）"), expanded=False):
        bdisp = bill.copy()
        bdisp[t("配賦")] = bdisp["item_name"].apply(
            lambda s: t("✅輸出配賦") if any(a in s for a in _ALLOC) else t("総額のみ"))
        bdisp[t("占比")] = (bdisp["amount_ex_tax"] / b_ex * 100) if b_ex else 0
        bdisp = bdisp.rename(columns={"item_name": t("費用種"),
                                      "amount_ex_tax": t("税別(¥)"), "amount_in_tax": t("税込(¥)")})
        bdisp = bdisp[[t("費用種"), t("税別(¥)"), t("税込(¥)"), t("占比"), t("配賦")]]
        st.dataframe(
            bdisp, hide_index=True, use_container_width=True,
            column_config={
                t("税別(¥)"): st.column_config.NumberColumn(format="¥%,.0f"),
                t("税込(¥)"): st.column_config.NumberColumn(format="¥%,.0f"),
                t("占比"): st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        st.caption(t("※ 合計 = JD 請求書 Billing Total。配賦3種のみ店舗・部署別配賦、その他は総額。"))

st.divider()


# ============================================================
# 店舗别明细表（当月）
# ============================================================
amt_piv = cur.pivot_table(index="shop", columns="cost_type", values="amount",
                          aggfunc="sum", fill_value=0)
qty_piv = cur.pivot_table(index="shop", columns="cost_type", values="qty",
                          aggfunc="sum", fill_value=0)
for k in CT_KEYS:
    if k not in amt_piv.columns:
        amt_piv[k] = 0.0
    if k not in qty_piv.columns:
        qty_piv[k] = 0

tbl = pd.DataFrame(index=amt_piv.index)
for k in CT_KEYS:
    tbl[CT_LABEL[k]] = amt_piv[k]
tbl[t("合計")] = tbl[[CT_LABEL[k] for k in CT_KEYS]].sum(axis=1)
tbl[t("件数")] = qty_piv["pickpack"] if "pickpack" in qty_piv.columns else 0
tbl[t("平均単価")] = (tbl[t("合計")] / tbl[t("件数")].replace(0, pd.NA)).fillna(0)
tbl = tbl.sort_values(t("合計"), ascending=False).reset_index()
tbl = tbl.rename(columns={"shop": t("店舗")})

with st.expander(t("🏪 店舗別明細（{ym}）").format(ym=sel_month), expanded=False):
    st.dataframe(
        tbl,
        use_container_width=True,
        hide_index=True,
        height=560,
        column_config={
            CT_LABEL["pickpack"]: st.column_config.NumberColumn(format="¥%,.0f"),
            CT_LABEL["lastmile"]: st.column_config.NumberColumn(format="¥%,.0f"),
            CT_LABEL["packing"]: st.column_config.NumberColumn(format="¥%,.0f"),
            t("合計"): st.column_config.NumberColumn(format="¥%,.0f"),
            t("平均単価"): st.column_config.NumberColumn(format="¥%,.1f"),
        },
    )
    st.download_button(
        t("📥 下载 CSV"),
        data=tbl.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"logistics_cost_{sel_dept_label}_{sel_month}.csv",
        mime="text/csv",
    )

st.divider()


# ============================================================
# 【不明】区（当月 · 全部 · 供 Boss 检查分类）
# ============================================================
unknown = df[(df["dept"] == "不明") & (df["year_month"] == sel_month)]
u_total = float(unknown["amount"].sum())
with st.expander(t("⚠️ 【不明】明細 — 店舗未分類 / 包裹未マッチ（{ym}・計 ¥{amt:,.0f}）")
                 .format(ym=sel_month, amt=u_total), expanded=False):
    if unknown.empty:
        st.success(t("当月 不明 なし ✅"))
    else:
        st.caption(t(
            "以下は店舗が dept 未登録、または包裹号が BM に未マッチの費用。"
            "「🚚 物流費用上传」で店舗→部署を追加するか BM を補完してください。"
        ))
        u = unknown.copy()
        u["cost_type"] = u["cost_type"].map(CT_LABEL).fillna(u["cost_type"])
        u = (u.groupby(["shop", "cost_type"])["amount"].sum()
             .reset_index()
             .rename(columns={"shop": t("店舗"), "cost_type": t("費用種"),
                              "amount": t("金額")})
             .sort_values(t("金額"), ascending=False))
        st.dataframe(u, use_container_width=True, hide_index=True, height=320)
