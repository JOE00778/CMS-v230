"""自动订货（测试）引擎 · page50 用。只读，不建 PO。

规则（Boss 2026-11-18 拍板 · 回测见 docs/analysis/订货规则回测_*）：
  order-up-to 定期补货  S = d·(L+R) + z·√((L+R)·σd² + (d·σL)²)
    d  = 90 天日均（可切 max(90天, 30天×1.25)）；只取 Shopee / Lazada / Coupang 销量
    L  = 供货商×品牌 受领交期中位（≥20 条）→ 供货商 → 14 天；σL 同源
    z  = A 2.33 / B 2.05 / C 1.28；A/B 无现货渠道 × z慢（默认 2.0），有现货渠道 × z现货（默认 1.0）
    lot = 该品 PO 数量的最大公约数；A/B 至少持有 1 lot
  每周一巡检（A/B）：未来 H 天内「手持 + 到货 − 需求 − 边际」跌破 0 → 有现货渠道走 5 天紧急单，否则把定期单提前
  库存只看弁天倉庫（JD 不再考虑 · Boss 2026-11-19）；在途 = PO 未受领数（含審批保留，单列）
  排除 lot ≥ 10,000 的自有品牌 OEM 品
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from functools import reduce

import numpy as np
import pandas as pd

Z_RANK = {"Aランク": 2.33, "Bランク": 2.05, "Cランク": 1.28}
SHOP_FILTER = "(shop ILIKE 'shopee%%' OR shop ILIKE 'lazada%%' OR shop ILIKE '%%coupang%%')"   # 带参数的 SQL 里 % 要写 %%（psycopg 会做格式化）
CARD_VENDOR_PAT = "カード仕入れ"   # 現金仕入れ 不作为渠道（Boss 2026-11-19：现金采购基本不能考虑）
OEM_LOT = 10000
DEFAULT_PARAMS = dict(zslow=2.0, zcard=1.0, trend=1.0, H=12, card_L=5, order_day=1, fwd_days=150, buffer_days=5)   # 保险日 5 天（Boss 2026-11-19）


def _df(conn, sql: str, params=None) -> pd.DataFrame:
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return pd.DataFrame([tuple(r) for r in rows], columns=cols)


def load_inputs(conn, today: date | None = None) -> dict:
    """从 PG 拉齐规则需要的全部输入（只读）。"""
    today = today or date.today()
    since = today - timedelta(days=180)
    items = _df(conn, "SELECT internal_id AS item_internal_id, jan, display_name, item_rank, cost_estimate "
                      "FROM nst.item_master_raw WHERE item_rank IN ('Aランク','Bランク','Cランク')")
    sales = _df(conn, f"SELECT item_internal_id, sale_date, SUM(qty_sold) AS qty FROM nst.sales_daily "
                      f"WHERE {SHOP_FILTER} AND sale_date >= ? GROUP BY 1, 2", (since,))
    snap_date = _df(conn, "SELECT MAX(snapshot_date) AS d FROM nst.inventory_snapshot").iloc[0, 0]
    snap = _df(conn, "SELECT item_internal_id, SUM(qty_on_hand) AS oh FROM nst.inventory_snapshot "
                     "WHERE snapshot_date = ? AND warehouse = '弁天倉庫' GROUP BY 1", (snap_date,))
    open_po = _df(conn, "SELECT po_internal_id, line_id, po_number, trandate, vendor_name, item_internal_id, "
                        "quantity - COALESCE(quantity_received, 0) AS open_qty, quantity, status, expected_receipt_date, rate "
                        "FROM nst.purchase_order_line WHERE quantity > COALESCE(quantity_received, 0) "
                        "AND trandate >= ? AND status NOT LIKE '%%終了%%'", (today - timedelta(days=120),))
    po = _df(conn, "SELECT trandate, vendor_name, item_internal_id, quantity FROM nst.purchase_order_line "
                   "WHERE quantity > 0 AND trandate >= ?", (today - timedelta(days=365),))
    rcpt = _df(conn, "SELECT vendor_name, item_internal_id, lead_time_days AS lt FROM nst.receipt_po_match "
                     "WHERE lead_time_days IS NOT NULL AND lead_time_days >= 0")
    try:
        brand = _df(conn, "SELECT jan, brand FROM brand.item_vendor_brand")
        brand_ok = True
    except Exception:   # cms_reader 无 brand schema 权限时降级：交期只到供货商级
        try: conn.rollback()
        except Exception: pass
        brand = pd.DataFrame(columns=["jan", "brand"]); brand_ok = False
    sales_max = sales.sale_date.max() if len(sales) else None
    return dict(today=today, items=items, sales=sales, snap=snap, snap_date=snap_date, open_po=open_po, po=po,
                rcpt=rcpt, brand=brand, brand_ok=brand_ok, sales_max=sales_max)


def _lead_tables(inp: dict):
    items = inp["items"][["item_internal_id", "jan"]].copy(); items["jan"] = items.jan.astype(str).str.strip()
    b = inp["brand"].copy(); b["jan"] = b.jan.astype(str).str.strip()
    rr = inp["rcpt"].merge(items, how="inner").merge(b, how="left")
    vb = rr.groupby(["vendor_name", "brand"]).lt.agg(["count", "median", "std"])
    vb = vb[vb["count"] >= 20]
    v = rr.groupby("vendor_name").lt.agg(["median", "std"])
    jan_brand = dict(zip(b.jan, b.brand))
    return vb, v, jan_brand


def compute(inp: dict, params: dict | None = None, plan: dict[str, float] | None = None) -> dict:
    """算今天的巡检 + 下次定期单 + 前推。plan = {jan: 计划月销}（覆盖 d）。"""
    p = {**DEFAULT_PARAMS, **(params or {})}
    today = pd.Timestamp(inp["today"]); plan = plan or {}
    nxt = _next_order_day(today, p["order_day"])
    it = inp["items"].copy(); it["cost"] = pd.to_numeric(it.cost_estimate, errors="coerce").fillna(0)
    it["jan"] = it.jan.astype(str).str.strip(); it = it.set_index("item_internal_id")
    days = pd.date_range(today - pd.Timedelta(days=180), today - pd.Timedelta(days=1))
    sd = inp["sales"]; sd = sd[sd.item_internal_id.isin(it.index)]
    sd = sd.assign(sale_date=pd.to_datetime(sd.sale_date), qty=pd.to_numeric(sd.qty))
    dm = (sd.pivot_table(index="item_internal_id", columns="sale_date", values="qty", aggfunc="sum")
            .reindex(columns=days, fill_value=0).fillna(0))
    oh = inp["snap"].set_index("item_internal_id").oh.astype(float)
    po = inp["po"].copy(); po["quantity"] = pd.to_numeric(po.quantity)
    last_v = po.sort_values("trandate").groupby("item_internal_id").vendor_name.last()
    lot = po.groupby("item_internal_id").quantity.apply(lambda x: reduce(math.gcd, [int(q) for q in x]))
    card_items = set(po[po.vendor_name.str.contains(CARD_VENDOR_PAT, na=False)].item_internal_id)
    vb, lv, jan_brand = _lead_tables(inp)
    op = inp["open_po"].copy(); op["open_qty"] = pd.to_numeric(op.open_qty); op["trandate"] = pd.to_datetime(op.trandate)
    op["pending"] = op.status.str.contains("承認保留", na=False)
    op_by = {k: g for k, g in op.groupby("item_internal_id")}

    rows = []
    for i in it.index:
        if i not in dm.index or dm.loc[i].sum() <= 0: continue
        lt_ = max(int(lot.get(i, 1)), 1)
        if lt_ >= OEM_LOT: continue
        v = last_v.get(i); br = jan_brand.get(it.jan[i])
        if v is not None and (v, br) in vb.index: L, sL = vb.loc[(v, br), "median"], vb.loc[(v, br), "std"]
        elif v is not None and v in lv.index: L, sL = lv.loc[v, "median"], lv.loc[v, "std"]
        else: L, sL = 14, 5
        L = int(L) if pd.notna(L) else 14; sL = float(sL) if pd.notna(sL) else 5.0
        h = dm.loc[i].values; d90 = h[-90:].mean(); d30 = h[-30:].mean()
        rk = it.item_rank[i]; isAB = rk != "Cランク"; card = i in card_items
        d = max(d90, d30 * p["trend"]) if isAB else max(h.mean(), d30)
        plan_m = plan.get(it.jan[i]); planned = plan_m is not None
        if planned: d = max(d, float(plan_m) / 30)
        sdv = h[-90:].std() * (d / d90 if d90 > 0 else 1.0)
        z = Z_RANK[rk] * ((p["zcard"] if card else p["zslow"]) if isAB else 1.0)
        o = op_by.get(i); pipe = float(o.open_qty.sum()) if o is not None else 0.0
        pend = float(o[o.pending].open_qty.sum()) if o is not None else 0.0
        eta = (o.trandate + pd.to_timedelta(L, "D")) if o is not None else None
        arr_before = float(o[eta < nxt].open_qty.sum()) if o is not None else 0.0
        oh0 = float(oh.get(i, 0.0))
        oh_next = max(oh0 - d * (nxt - today).days + arr_before, 0); pipe_next = pipe - arr_before
        R = 30; S = d * (L + R) + z * math.sqrt((L + R) * sdv ** 2 + (d * sL) ** 2)
        if isAB: S = max(S, lt_)
        need = S - oh_next - pipe_next
        q = math.ceil(max(need, 0) / lt_) * lt_ if (need > 0 and d > 0) else 0
        EL = p["card_L"] if (isAB and card) else L; Hk = p["H"] if isAB else L + 7
        margin = z * sdv * math.sqrt(max(EL, 1)); mn = oh0
        for tau in range(1, Hk + 1):
            arrived = float(o[eta <= today + pd.Timedelta(days=tau)].open_qty.sum()) if o is not None else 0.0
            mn = min(mn, oh0 + arrived - d * tau - margin)
        trig = bool(d > 0 and mn < 0); qe = 0
        if trig:
            Rn = (nxt - today).days; Se = d * (L + Rn) + z * math.sqrt((L + Rn) * sdv ** 2 + (d * sL) ** 2)
            arriving = float(o[eta <= today + pd.Timedelta(days=Hk)].open_qty.sum()) if o is not None else 0.0
            ne = max(Se - oh0 - pipe, d * Hk + margin - oh0 - arriving) if EL < Hk else Se - oh0 - pipe
            qe = math.ceil(max(ne, 0) / lt_) * lt_
        rows.append(dict(item=i, jan=it.jan[i], name=it.display_name[i], rank=rk[0], vendor=v, card=card, planned=planned,
                         L=L, sL=round(sL, 1), lot=lt_, d=round(d, 2), d30=round(d30, 2), d90=round(d90, 2), cost=float(it.cost[i]),
                         oh=oh0, pipe=pipe, pending=pend, oh_next=round(oh_next), S=round(S), q_next=q, amt_next=q * float(it.cost[i]),
                         trig_now=trig, q_emerg=qe, amt_emerg=qe * float(it.cost[i]),
                         days_cover=round(oh0 / d, 1) if d > 0 else None, sdv=sdv, z=z))
    df = pd.DataFrame(rows)
    fwd = _forward(df, op, today, p) if len(df) else pd.DataFrame()
    return dict(items=df, forward=fwd, next_day=nxt.date(), params=p)


def _next_order_day(today: pd.Timestamp, order_day: int) -> pd.Timestamp:
    y, m = today.year, today.month
    cand = pd.Timestamp(y, m, order_day)
    if cand <= today:
        m2 = m % 12 + 1; y2 = y + (m == 12); cand = pd.Timestamp(y2, m2, order_day)
    return cand


def _forward(df: pd.DataFrame, op: pd.DataFrame, T0: pd.Timestamp, p: dict) -> pd.DataFrame:
    """A/B 前推：需求按 d 恒定、交期取中位，记录每次下单日期/性质/SKU/金额。"""
    a = df[(df["rank"] != "C") & (df.d > 0)].reset_index(drop=True)
    if a.empty: return pd.DataFrame()
    n = len(a); DAYS = int(p["fwd_days"]); HOR = DAYS + 400
    z = a.z.values; d = a.d.values; L = a.L.values.astype(int); sL = a.sL.values; lot = a.lot.values.astype(float)
    cost = a.cost.values; card = a.card.values; EL = np.where(card, p["card_L"], L); H = int(p["H"]); sdv = np.sqrt(d)
    arr = np.zeros((n, HOR)); oh = a.oh.values.astype(float).copy(); idx = {i: k for k, i in enumerate(a.item)}
    for _, r in op.iterrows():
        k = idx.get(r.item_internal_id)
        if k is None: continue
        t = min(max((r.trandate + pd.Timedelta(days=int(L[k])) - T0).days, 1), HOR - 1); arr[k, t] += float(r.open_qty)
    pipe = arr.sum(1); log = []

    def place(mask, q, lead, t, kind):
        ks = np.where(mask & (q > 0))[0]
        for k in ks: arr[k, min(t + max(int(lead[k]), 1), HOR - 1)] += q[k]; pipe[k] += q[k]
        if len(ks): log.append(dict(date=(T0 + pd.Timedelta(days=t)).date(), kind=kind, sku=len(ks),
                                    vendors=int(a.vendor.iloc[ks].nunique()), amt=float((q[ks] * cost[ks]).sum())))
    for t in range(DAYS):
        day = T0 + pd.Timedelta(days=t)
        oh += arr[:, t]; pipe -= arr[:, t]; oh = np.maximum(oh - d, 0)
        if day.day == p["order_day"]:
            R = 30; S = np.maximum(d * (L + R) + z * np.sqrt((L + R) * sdv ** 2 + (d * sL) ** 2), lot); need = S - oh - pipe
            q = np.where(need > 0, np.ceil(np.maximum(need, 0) / lot) * lot, 0); place(need > 0, q, L, t, "定期")
        if day.weekday() == 0 or t == 0:
            cum = np.cumsum(arr[:, t + 1:t + 1 + H], axis=1); tau = np.arange(1, H + 1); margin = z * sdv * np.sqrt(EL)
            proj = oh[:, None] + cum - d[:, None] * tau[None, :] - margin[:, None]; trig = proj.min(1) < 0
            nxt = _next_order_day(day, p["order_day"]); Rn = (nxt - day).days
            S = d * (L + Rn) + z * np.sqrt((L + Rn) * sdv ** 2 + (d * sL) ** 2); arriving = arr[:, t + 1:t + 1 + H].sum(1)
            need = np.where(card, np.maximum(S - oh - pipe, d * H + margin - oh - arriving), S - oh - pipe)
            q = np.where(trig & (need > 0), np.ceil(np.maximum(need, 0) / lot) * lot, 0)
            place(trig & card, q, EL, t, "紧急(现货渠道)"); place(trig & ~card, q, L, t, "提前拉单")
    return pd.DataFrame(log)


def summary(res: dict) -> dict:
    df = res["items"]; ab = df[df["rank"] != "C"]; c = df[df["rank"] == "C"]
    em = ab[ab.trig_now]
    return dict(n=len(df), oh_ab=float((ab.oh * ab.cost).sum()), pipe_ab=float((ab["pipe"] * ab.cost).sum()),
                pending_ab=float((ab["pending"] * ab.cost).sum()),
                emerg_sku=len(em), emerg_amt=float(em.amt_emerg.sum()), emerg_card_sku=int(em.card.sum()),
                emerg_card_amt=float(em[em.card].amt_emerg.sum()),
                next_ab_sku=int((ab.q_next > 0).sum()), next_ab_amt=float(ab.amt_next.sum()),
                next_c_sku=int((c.q_next > 0).sum()), next_c_amt=float(c.amt_next.sum()),
                month_cogs_ab=float((ab.d * ab.cost).sum() * 30))


# ============================================================
# 到货闭环：每张未受领 PO 行 → 计划到货日 / 预计断货日 / 最晚决策日 / 状态
#   Boss 2026-11-19：超期以断货日倒推——第一货源不到，还来得及在第二货源下单才算安全
#   最晚决策日 = 预计断货日 − 第二货源交期 − 保险日
#   第二货源交期：有カード仕入れ渠道 → 5 天（現金仕入れ 不算）；否则近 12 月给该品供过货的其他供货商里交期中位最短的；都没有 → 14 天并标「无第二货源」
# ============================================================
def po_tracking(inp: dict, res: dict, params: dict | None = None) -> pd.DataFrame:
    p = {**DEFAULT_PARAMS, **(params or {})}
    today = pd.Timestamp(inp["today"]); items = res["items"].set_index("item")
    op = inp["open_po"].copy()
    if op.empty: return pd.DataFrame()
    op["open_qty"] = pd.to_numeric(op.open_qty); op["trandate"] = pd.to_datetime(op.trandate)
    op["expected_receipt_date"] = pd.to_datetime(op.expected_receipt_date, errors="coerce")
    # 供货商级交期中位（受领突合）
    lv = inp["rcpt"].groupby("vendor_name").lt.median()
    po = inp["po"].copy(); po["trandate"] = pd.to_datetime(po.trandate)
    alt = {}   # item → (第二货源名, 交期)
    for i, g in po.groupby("item_internal_id"):
        vs = [v for v in g.vendor_name.unique() if v in lv.index]
        alt[i] = sorted(((float(lv[v]), v) for v in vs), key=lambda x: x[0])
    rows = []
    for _, r in op.iterrows():
        i = r.item_internal_id
        if i not in items.index: continue
        it = items.loc[i]; L = int(it.L); d = float(it.d); oh = max(float(it.oh), 0.0)
        eta_src = "NST予定日"; eta = r.expected_receipt_date
        if pd.isna(eta): eta = r.trandate + pd.Timedelta(days=L); eta_src = "供货商交期"
        others = [(l, v) for l, v in alt.get(i, []) if v != r.vendor_name and "現金仕入れ" not in v]
        if it.card: L2, src2 = p["card_L"], "カード仕入れ"
        elif others: L2, src2 = int(others[0][0]), others[0][1]
        else: L2, src2 = 14, "无第二货源"
        stockout = today + pd.Timedelta(days=int(oh / d)) if d > 0 else pd.NaT
        decide = stockout - pd.Timedelta(days=L2 + p["buffer_days"]) if pd.notna(stockout) else pd.NaT
        late = (today - eta).days if today > eta else 0
        alt_arrive = today + pd.Timedelta(days=L2); isAB = it["rank"] != "C"
        if oh <= 0 and d > 0: state = "⛔ 已断货"
        elif isAB and pd.notna(decide) and today >= decide and alt_arrive < eta and src2 != "无第二货源": state = "🔴 转第二货源"
        elif isAB and pd.notna(decide) and today >= decide: state = "🟠 盯第一货源"   # 换货源也赶不上，只能催
        elif late > 0: state = "🟡 迟到"
        elif eta == today: state = "📦 今日应到"
        else: state = "🚚 在途"
        q2 = math.ceil(max(d * (L2 + p["buffer_days"] + 7) - oh, 0) / max(int(it.lot), 1)) * max(int(it.lot), 1) if state in ("🔴 转第二货源", "⛔ 已断货") and src2 != "无第二货源" else 0
        rows.append(dict(po_number=r.po_number, po_internal_id=r.po_internal_id, line_id=r.line_id, vendor=r.vendor_name, jan=it.jan, name=it["name"], rank=it["rank"],
                         po_date=r.trandate.date(), eta=eta.date(), eta_src=eta_src, late_days=late, open_qty=float(r.open_qty), amount=float(r.open_qty) * float(r.rate or 0),
                         oh=oh, d=round(d, 2), stockout=stockout.date() if pd.notna(stockout) else None, alt_vendor=src2, alt_L=L2,
                         decide_by=decide.date() if pd.notna(decide) else None, state=state, q_alt=q2, amt_alt=q2 * float(it.cost),
                         pending=bool(r.pending) if "pending" in r else False))
    df = pd.DataFrame(rows)
    order = {"⛔ 已断货": 0, "🔴 转第二货源": 1, "🟠 盯第一货源": 2, "🟡 迟到": 3, "📦 今日应到": 4, "🚚 在途": 5}
    return df.assign(_o=df.state.map(order)).sort_values(["_o", "decide_by", "late_days"], ascending=[True, True, False]).drop(columns="_o") if len(df) else df
