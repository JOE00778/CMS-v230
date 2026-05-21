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

st.set_page_config(page_title=t("物流费用分析"), page_icon="🚚", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🚚 物流费用分析 (JD 配賦)"))
st.caption(t(
    "JD 请求书 → 按包裹号关联店铺 → 按部署配賦（费用为不含税）· "
    "包装费用=Pick&Pack / 国内运送费用=Last mile / 包材=Packing Charge"
))

# 費用種：内部キー → 表示名 / 配色
COST_TYPES = [
    ("pickpack", t("包装费用")),
    ("lastmile", t("国内运送费用")),
    ("packing", t("包材")),
]
CT_KEYS = [k for k, _ in COST_TYPES]
CT_LABEL = dict(COST_TYPES)

# 梱包材编码 → 名称（配賦表「算式保存用」より）
MATERIAL_NAMES = {
    "HC100317": "防水袋", "HC100318": "60サイズ箱", "HC100319": "80サイズ箱",
    "HC100320": "100サイズ箱", "HC100321": "120サイズ箱", "HC100322": "140サイズ箱",
    "HC100323": "160サイズ箱", "HC100452": "泡泡袋小", "HC100453": "泡泡袋大",
}
# 包材编码 固定表示顺（配賦表順 · 当月/当部署で未使用でも 0 列を出す）
MATERIAL_ORDER = ["HC100317", "HC100318", "HC100319", "HC100320", "HC100321",
                  "HC100322", "HC100323", "HC100449", "HC100452", "HC100453"]


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
    st.error(t("⚠️ logistics.cost_monthly 读取失败（schema 未部署？）") + f"\n\n{e}")
    st.stop()

if df.empty:
    st.warning(t(
        "⚠️ 暂无物流费用数据。"
        "请在「🚚 物流费用上传」上传 JD 请求书 与 BM(包裹号→店铺)。"
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
        help=t("默认仅输出（Boss 指示）。不明区在最下方单独列出供检查。"),
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
    t("物流费 合计 (¥)"), f"¥{total_cur:,.0f}",
    delta=(f"{mom:+.1f}% {t('环比上月')}" if mom is not None else None),
    delta_color="inverse",
)
for col, (key, label) in zip((k2, k3, k4), COST_TYPES):
    amt = float(by_type.get(key, 0))
    pct = (amt / total_cur * 100) if total_cur else 0
    col.metric(label, f"¥{amt:,.0f}", delta=f"{pct:.1f}%", delta_color="off")
k5.metric(t("出货件数 (Pick&Pack)"), f"{parcels:,}")

st.divider()


# ============================================================
# 月度趋势图（page05 风格: 点線 + 縦軸 + 縦ルール全値 hover）
# ============================================================
st.subheader(t("📈 物流费 月度推移（{dept}）").format(dept=sel_dept_label))

piv = base.groupby(["year_month", "cost_type"])["amount"].sum().unstack(fill_value=0)
for k in CT_KEYS:
    if k not in piv.columns:
        piv[k] = 0.0
piv["__total__"] = piv[CT_KEYS].sum(axis=1)
piv_r = piv.reset_index().sort_values("year_month")

series = [(k, CT_LABEL[k]) for k in CT_KEYS] + [("__total__", t("合计"))]

if len(piv_r) >= 1:
    _x = alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0))
    _near = alt.selection_point(nearest=True, on="pointerover",
                                fields=["year_month"], empty=False)
    base_c = alt.Chart(piv_r).encode(x=_x)
    lines = [
        base_c.mark_line(point=True).encode(
            y=alt.Y(f"{col}:Q", title=t("金额 (日元)")),
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
st.subheader(t("💴 JD 费用全构成（{ym} · Billing 对账）").format(ym=sel_month))
bill = _df(
    "SELECT seq, item_name, amount_ex_tax, amount_in_tax "
    "FROM logistics.cost_billing WHERE year_month=%(ym)s ORDER BY seq",
    {"ym": sel_month},
)
if bill.empty:
    st.info(t("当月无 Billing 数据（上传请求书时自动录入）。"))
else:
    bill["amount_ex_tax"] = pd.to_numeric(bill["amount_ex_tax"], errors="coerce").fillna(0.0)
    bill["amount_in_tax"] = pd.to_numeric(bill["amount_in_tax"], errors="coerce").fillna(0.0)
    b_ex, b_in = bill["amount_ex_tax"].sum(), bill["amount_in_tax"].sum()

    bill_prev = _df(
        "SELECT item_name, amount_ex_tax FROM logistics.cost_billing WHERE year_month=%(ym)s",
        {"ym": prev_month},
    ) if prev_month else pd.DataFrame()
    prev_map, b_ex_prev = {}, 0.0
    if not bill_prev.empty:
        bill_prev["amount_ex_tax"] = pd.to_numeric(bill_prev["amount_ex_tax"], errors="coerce").fillna(0.0)
        prev_map = dict(zip(bill_prev["item_name"], bill_prev["amount_ex_tax"]))
        b_ex_prev = bill_prev["amount_ex_tax"].sum()
    mom_total = (b_ex / b_ex_prev - 1) * 100 if b_ex_prev else None

    bc1, bc2, bc3 = st.columns(3)
    bc1.metric(t("JD 费用 不含税 合计"), f"¥{b_ex:,.0f}",
               delta=(f"{mom_total:+.1f}% {t('环比上月')}" if mom_total is not None else None),
               delta_color="inverse")
    bc2.metric(t("含税 合计"), f"¥{b_in:,.0f}")
    bc3.metric(t("费用种 数"), f"{len(bill)}")
    _ALLOC = ("OB-Pick&Pack", "Last mile", "Packing Charge")
    is_alloc = bill["item_name"].apply(lambda s: any(a in s for a in _ALLOC))
    others = bill[~is_alloc].reset_index(drop=True)

    if not others.empty:
        st.markdown(t("###### 🗂️ 其他费用（配賦3种以外 · 环比上月）"))
        ncol = 4
        recs = list(others.iterrows())
        for base in range(0, len(recs), ncol):
            ccols = st.columns(ncol)
            for j, (_, r) in enumerate(recs[base:base + ncol]):
                cn, en = _split_name(r["item_name"])
                pv = prev_map.get(r["item_name"])
                mom = (r["amount_ex_tax"] / pv - 1) * 100 if pv else None
                ccols[j].metric(
                    label=cn,
                    value=f"¥{r['amount_ex_tax']:,.0f}",
                    delta=(f"{mom:+.1f}% {t('环比上月')}" if mom is not None else None),
                    delta_color="inverse",
                    help=(f"{en} ・ " if en else "") + t("含税 ¥{v:,.0f}").format(v=r["amount_in_tax"]),
                )

    with st.expander(t("📋 全部费用明细（含配賦3种 · Billing 对账表）"), expanded=False):
        bdisp = bill.copy()
        bdisp[t("配賦")] = bdisp["item_name"].apply(
            lambda s: t("✅输出配賦") if any(a in s for a in _ALLOC) else t("仅总额"))
        bdisp[t("占比")] = (bdisp["amount_ex_tax"] / b_ex * 100) if b_ex else 0
        bdisp = bdisp.rename(columns={"item_name": t("费用种"),
                                      "amount_ex_tax": t("不含税(¥)"), "amount_in_tax": t("含税(¥)")})
        bdisp = bdisp[[t("费用种"), t("不含税(¥)"), t("含税(¥)"), t("占比"), t("配賦")]]
        st.dataframe(
            bdisp, hide_index=True, use_container_width=True,
            column_config={
                t("不含税(¥)"): st.column_config.NumberColumn(format="¥%,.0f"),
                t("含税(¥)"): st.column_config.NumberColumn(format="¥%,.0f"),
                t("占比"): st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        st.caption(t("※ 合计 = JD 请求书 Billing Total。仅配賦3种按店铺·部署配賦，其他为总额。"))

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
tbl[t("合计")] = tbl[[CT_LABEL[k] for k in CT_KEYS]].sum(axis=1)
tbl[t("件数")] = qty_piv["pickpack"] if "pickpack" in qty_piv.columns else 0
tbl[t("平均单价")] = (tbl[t("合计")] / tbl[t("件数")].replace(0, pd.NA)).fillna(0)
tbl = tbl.sort_values(t("合计"), ascending=False).reset_index()
tbl = tbl.rename(columns={"shop": t("店铺")})

with st.expander(t("🏪 店铺别明细（{ym}）").format(ym=sel_month), expanded=False):
    st.dataframe(
        tbl,
        use_container_width=True,
        hide_index=True,
        height=560,
        column_config={
            CT_LABEL["pickpack"]: st.column_config.NumberColumn(format="¥%,.0f"),
            CT_LABEL["lastmile"]: st.column_config.NumberColumn(format="¥%,.0f"),
            CT_LABEL["packing"]: st.column_config.NumberColumn(format="¥%,.0f"),
            t("合计"): st.column_config.NumberColumn(format="¥%,.0f"),
            t("平均单价"): st.column_config.NumberColumn(format="¥%,.1f"),
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
# 店舗別 梱包材使用（当月 · 折叠 · material_cd × 店舗）
# ============================================================
pk = _df(
    """SELECT COALESCE(d.dept,'不明') AS dept, COALESCE(m.shop,'(未マッチ包裹)') AS shop,
              r.material_cd, SUM(r.material_qty) AS qty
       FROM logistics.cost_invoice_raw r
       LEFT JOIN logistics.order_shop_map m ON r.join_key = m.parcel_no
       LEFT JOIN logistics.shop_dept_map  d ON m.shop = d.shop
       WHERE r.cost_type='packing' AND r.year_month=%(ym)s AND r.material_cd IS NOT NULL
       GROUP BY 1, 2, 3""",
    {"ym": sel_month},
)
with st.expander(t("🧊 店铺别 包材使用（{ym}）").format(ym=sel_month), expanded=False):
    if sel_dept_label == t("輸出"):
        pk = pk[pk["dept"] == "輸出"]
    elif sel_dept_label == t("EC"):
        pk = pk[pk["dept"] == "EC"]
    else:
        pk = pk[pk["dept"] != "不明"]
    if pk.empty:
        st.info(t("当月无包材数据。"))
    else:
        pk["qty"] = pd.to_numeric(pk["qty"], errors="coerce").fillna(0).astype(int)
        pkpiv = pk.pivot_table(index="shop", columns="material_cd", values="qty",
                               aggfunc="sum", fill_value=0)
        extra = [c for c in pkpiv.columns if c not in MATERIAL_ORDER]
        pkpiv = pkpiv.reindex(columns=MATERIAL_ORDER + extra, fill_value=0)
        pkpiv = pkpiv.rename(columns=lambda c: MATERIAL_NAMES.get(c, c))
        pkpiv[t("合计")] = pkpiv.sum(axis=1)
        pkpiv = pkpiv.sort_values(t("合计"), ascending=False).reset_index()
        pkpiv = pkpiv.rename(columns={"shop": t("店铺")})
        st.dataframe(pkpiv, hide_index=True, use_container_width=True, height=480)
        st.download_button(
            t("📥 下载 CSV"), pkpiv.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"packing_material_{sel_dept_label}_{sel_month}.csv",
            mime="text/csv", key="pk_csv",
        )

st.divider()


# ============================================================
# 【不明】区（当月 · 全部 · 供 Boss 检查分类）
# ============================================================
unknown = df[(df["dept"] == "不明") & (df["year_month"] == sel_month)]
u_total = float(unknown["amount"].sum())
with st.expander(t("⚠️ 【不明】明细 — 店铺未分类 / 包裹未匹配（{ym}·计 ¥{amt:,.0f}）")
                 .format(ym=sel_month, amt=u_total), expanded=False):
    if unknown.empty:
        st.success(t("当月无不明 ✅"))
    else:
        st.caption(t(
            "以下为店铺 dept 未登记，或包裹号在 BM 中未匹配的费用。"
            "请在「🚚 物流费用上传」添加店铺→部署，或补全 BM。"
        ))
        u = unknown.copy()
        u["cost_type"] = u["cost_type"].map(CT_LABEL).fillna(u["cost_type"])
        u = (u.groupby(["shop", "cost_type"])["amount"].sum()
             .reset_index()
             .rename(columns={"shop": t("店铺"), "cost_type": t("费用种"),
                              "amount": t("金额")})
             .sort_values(t("金额"), ascending=False))
        st.dataframe(u, use_container_width=True, hide_index=True, height=320)
