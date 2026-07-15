"""模块 #34 供货商管理 v2 · 采购可视化看板（第一期 · spec 2026-07-15 a5138cf）.

  📊 供应商驾驶舱   供应商×月：需求基线/计划基线金额/实际PO/差异/起订·免运缺口/理论节省
  🏷️ 品牌×供应商    品牌→各供应商 近12个月金额/数量/加权折扣率/最低报价差异
  📉 折扣率总览     全体/等级/品牌×供应商/SKU×供应商 四层月度加权折扣率 + MSRP覆盖率
  📋 SKU采购明细    主供/最近PO供/最低有效报价/价差/销量/库存/在途 + 规则状态标记
  🔍 优化机会       ①毛利问题(折扣率过高) ②采购机会(当前价>最低有效报价)
  📤 报价维护       手动录入/上传/一括/主供指定/PO种子 + 有效期维护 + 数据质量摘要
  🏢 供应商与品牌规则 供应商主档 + 供应商×品牌规则(第一期仅展示维护·不参与分配)

统一口径（spec「统一数据集与口径」节）:
  计划基线金额 = 当月销量 × 当前采购价（主供指定价 > 最近PO单价；两者皆无=数据不足）
  实际PO金额   = trandate 自然月 Σ(rate×quantity)
  最低有效报价 = 启用供应商最新报价中 有效(期间内)/有效性未确认(期间未设) 的最低价；
                 过期/未生效/缺价缺日期/停用 不参与节省测算
  理论节省     = (当前采购价 − 最低有效报价) × 对应销量需求（机会测算·非可实现承诺）
  金额加权折扣率 = Σ(rate×qty) ÷ Σ(msrp_taxex×qty) × 10（仅 MSRP 完整 PO 行）
  MSRP覆盖率   = 有MSRP的PO金额 ÷ 全部PO金额
  时间范围     = 近12个完整自然月；当月标注「截至今日」

データ: PG sourcing schema（本ページ idempotent 建表）+ nst.*（NST 直连）。
ロジック: shared/sourcing.py（純関数·tests/test_sourcing.py）。
第一期禁止: 発注書生成/自動発注/供应商自動切換/page25 旧引擎接続。
"""
from __future__ import annotations

import datetime as dt
import io

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang
from shared import sourcing as sc

st.set_page_config(page_title=t("供货商管理"), page_icon="🏢", layout="wide")
from shared.auth import require_password  # noqa: E402
require_password()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
lang_selector()
conn = get_connection()

_ja = get_lang() == "ja"


def _dl(zh: str, ja: str) -> str:
    return ja if _ja else zh


st.title(t("🏢 供货商管理"))
st.caption(_dl(
    "采购可视化看板（第一期）：供应商驾驶舱 · 品牌×供应商 · 折扣率总览 · SKU采购明细 · "
    "优化机会 · 报价维护 · 供应商与品牌规则。最低报价仅为理论优化机会，不产生订货指令。",
    "仕入可視化ダッシュボード（第1期）：仕入先コックピット · ブランド×仕入先 · 掛率総覧 · "
    "SKU 仕入明細 · 改善機会 · 見積メンテ · 仕入先とブランドルール。"
    "最安見積は理論上の機会表示のみで発注指示は生成しない。",
))


# ============================================================
# 幂等建表（PG · 首次加载自动建）
# ============================================================
def _ensure_schema() -> str | None:
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS sourcing")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sourcing.supplier ("
            "supplier_name TEXT PRIMARY KEY, min_order_amount NUMERIC(14,2), "
            "default_lead_days INTEGER, is_prepay BOOLEAN DEFAULT FALSE, "
            "active BOOLEAN DEFAULT TRUE, note TEXT, updated_at TIMESTAMPTZ DEFAULT NOW())")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sourcing.supplier_quote ("
            "id BIGSERIAL PRIMARY KEY, supplier_name TEXT NOT NULL, jan TEXT NOT NULL, "
            "item_name TEXT, price NUMERIC(14,2), moq NUMERIC(14,2), order_lot NUMERIC(14,2), "
            "lead_days INTEGER, quote_date DATE NOT NULL, source TEXT, note TEXT, "
            "imported_at TIMESTAMPTZ DEFAULT NOW())")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sq_jan ON sourcing.supplier_quote (jan)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sq_sup ON sourcing.supplier_quote (supplier_name)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sourcing.item_main_supplier ("
            "jan TEXT PRIMARY KEY, supplier_name TEXT NOT NULL, price NUMERIC(14,2), "
            "source TEXT, updated_at TIMESTAMPTZ DEFAULT NOW())")
        conn.execute("ALTER TABLE sourcing.supplier "
                     "ADD COLUMN IF NOT EXISTS free_ship_threshold NUMERIC(14,2)")
        conn.execute("ALTER TABLE sourcing.supplier "
                     "ADD COLUMN IF NOT EXISTS official_name TEXT")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sourcing.supplier_alias ("
            "alias TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
        # ── v2 第一期新增（spec 2026-07-15）──
        conn.execute("ALTER TABLE sourcing.supplier_quote "
                     "ADD COLUMN IF NOT EXISTS valid_from DATE")
        conn.execute("ALTER TABLE sourcing.supplier_quote "
                     "ADD COLUMN IF NOT EXISTS valid_to DATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sourcing.supplier_brand_rule ("
            "supplier_name TEXT NOT NULL, maker TEXT NOT NULL, "
            "effective_from DATE NOT NULL, "
            "min_order_amount NUMERIC(14,2), min_order_qty NUMERIC(14,2), "
            "ship_fee NUMERIC(14,2), free_ship_threshold NUMERIC(14,2), "
            "effective_to DATE, note TEXT, updated_by TEXT, "
            "updated_at TIMESTAMPTZ DEFAULT NOW(), "
            "PRIMARY KEY (supplier_name, maker, effective_from))")
        conn.commit()
        return None
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        return str(e)


_schema_err = _ensure_schema()
if _schema_err:
    st.error(t("⚠️ sourcing schema 初期化失败（PG 未接続？）") + f"\n\n{_schema_err}")
    st.stop()


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


def _alias_map() -> dict:
    """供货商别名表（NST 長名 → Boss 短名·整理相同供货商用）。"""
    _al = _read("SELECT alias, canonical FROM sourcing.supplier_alias")
    return dict(zip(_al["alias"], _al["canonical"])) if not _al.empty else {}


def _ensure_suppliers(names: list[str]) -> None:
    for _n in sorted({str(x).strip() for x in names if str(x).strip()}):
        conn.execute("INSERT INTO sourcing.supplier (supplier_name) VALUES (?) "
                     "ON CONFLICT (supplier_name) DO NOTHING", (_n,))
    conn.commit()


def sc_num(v):
    """文字/记号付き → float。空/异常 → None。"""
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(str(v).replace(",", "").replace("¥", "").replace("￥", "")
                     .replace("₱", "").strip())
    except (ValueError, TypeError):
        return None


def sc_int(v):
    _n = sc_num(v)
    return int(_n) if _n is not None else None


def _date_or_none(v):
    """data_editor の日付列 → 'YYYY-MM-DD' or None。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    return s[:10] if s else None


# ============================================================
# 统一采购分析数据集（spec「统一数据集与口径」· 全 tab 共用同源数字）
# ============================================================
_TODAY = dt.date.today()
_CUR_YM = _TODAY.strftime("%Y-%m")
_m = _TODAY.replace(day=1)
MONTHS_FULL: list[str] = []           # 近 12 个完整自然月（升序）
for _ in range(12):
    _m = (_m - dt.timedelta(days=1)).replace(day=1)
    MONTHS_FULL.insert(0, _m.strftime("%Y-%m"))
ALL_YMS = MONTHS_FULL + [_CUR_YM]     # + 当月（截至今日）


def _ym_label(ym: str) -> str:
    return ym + _dl("(截至今日)", "(本日まで)") if ym == _CUR_YM else ym


def _items_all(ranks: list[str] | None = None) -> pd.DataFrame:
    _sql = ("SELECT internal_id, jan, display_name, maker, item_rank, last_purchase_cost "
            "FROM nst.item_master_raw WHERE jan IS NOT NULL")
    _p: tuple = ()
    if ranks:
        _sql += " AND item_rank IN (" + ",".join(["?"] * len(ranks)) + ")"
        _p = tuple(ranks)
    _d = _read(_sql, _p)
    if not _d.empty:
        _d["internal_id"] = _d["internal_id"].astype(str)
        _d["last_purchase_cost"] = pd.to_numeric(_d["last_purchase_cost"], errors="coerce")
    return _d


def _msrp() -> pd.DataFrame:
    """jan → msrp_taxex（税抜換算 · shared/sourcing.msrp_taxex）。"""
    _d = _read("SELECT jan, msrp_jpy, msrp_jpy_taxin FROM nst.item_msrp")
    if _d.empty:
        return pd.DataFrame(columns=["jan", "msrp_taxex"])
    _d["msrp_taxex"] = sc.msrp_taxex(_d["msrp_jpy"], _d["msrp_jpy_taxin"])
    return _d[["jan", "msrp_taxex"]].drop_duplicates("jan")


def _po12() -> pd.DataFrame:
    """近12完整月+当月の PO 行（実際PO/折扣率の唯一ソース）。

    列: internal_id, ym, trandate, supplier_name(別名归一), rate, quantity,
        amount(=rate×qty), jan, display_name, maker, item_rank, msrp_taxex。
    """
    _d = _read(
        "SELECT pl.item_internal_id AS internal_id, "
        "SUBSTR(CAST(pl.trandate AS TEXT),1,7) AS ym, pl.trandate, "
        "pl.vendor_name AS supplier_name, pl.rate, pl.quantity "
        "FROM nst.purchase_order_line pl "
        "WHERE pl.item_internal_id IS NOT NULL AND pl.vendor_name IS NOT NULL "
        "AND pl.rate IS NOT NULL AND pl.quantity IS NOT NULL AND pl.trandate >= ?",
        (MONTHS_FULL[0] + "-01",))
    if _d.empty:
        return pd.DataFrame(columns=["internal_id", "ym", "trandate", "supplier_name",
                                     "rate", "quantity", "amount", "jan",
                                     "display_name", "maker", "item_rank", "msrp_taxex"])
    _d["internal_id"] = _d["internal_id"].astype(str)
    _d["rate"] = pd.to_numeric(_d["rate"], errors="coerce")
    _d["quantity"] = pd.to_numeric(_d["quantity"], errors="coerce")
    _d = _d.dropna(subset=["rate", "quantity"])
    _d = _d[_d["quantity"] > 0]
    _d["amount"] = _d["rate"] * _d["quantity"]
    _d = sc.apply_supplier_alias(_d, _alias_map())
    _it = _items_all()
    _d = _d.merge(_it[["internal_id", "jan", "display_name", "maker", "item_rank"]],
                  on="internal_id", how="left")
    _d = _d.merge(_msrp(), on="jan", how="left")
    _d["msrp_taxex"] = pd.to_numeric(_d["msrp_taxex"], errors="coerce")
    return _d


def _latest_po_vendor() -> pd.DataFrame:
    """internal_id → NST 直近 PO 行の仕入先/単価（MAX(trandate)·簡称へ正規化）."""
    _p = _read("SELECT item_internal_id, trandate, vendor_name AS supplier_name, rate "
               "FROM nst.purchase_order_line "
               "WHERE item_internal_id IS NOT NULL AND vendor_name IS NOT NULL")
    if _p.empty:
        return pd.DataFrame(columns=["internal_id", "cur_supplier", "cur_rate"])
    _p = _p.sort_values("trandate").drop_duplicates("item_internal_id", keep="last")
    _p = sc.apply_supplier_alias(_p, _alias_map())
    _p = _p.rename(columns={"item_internal_id": "internal_id",
                            "supplier_name": "cur_supplier", "rate": "cur_rate"})
    _p["internal_id"] = _p["internal_id"].astype(str)
    _p["cur_rate"] = pd.to_numeric(_p["cur_rate"], errors="coerce")
    return _p[["internal_id", "cur_supplier", "cur_rate"]]


def _quotes_status() -> pd.DataFrame:
    """最新报价 + 有效性判定（sc.quote_status）。列に validity / eligible。"""
    _q = _read("SELECT id, supplier_name, jan, price, quote_date, valid_from, valid_to, "
               "moq, order_lot FROM sourcing.supplier_quote")
    if _q.empty:
        return pd.DataFrame(columns=["supplier_name", "jan", "price", "quote_date",
                                     "valid_from", "valid_to", "validity", "eligible"])
    _sup = _read("SELECT supplier_name, active FROM sourcing.supplier")
    # active: PG=bool / SQLite=0·1 / 未設定 NULL=啓用扱い
    _act = ({str(r["supplier_name"]).strip():
             (True if r["active"] is None or (isinstance(r["active"], float)
                                              and pd.isna(r["active"]))
              else bool(r["active"]))
             for _, r in _sup.iterrows()} if not _sup.empty else None)
    return sc.quote_status(sc.latest_quotes(_q), active=_act, today=pd.Timestamp(_TODAY))


def _sales_m13() -> pd.DataFrame:
    """internal_id × ym の販売数（nst.sales_daily 集計 · 近12完整月+当月）。"""
    _d = _read(
        "SELECT item_internal_id AS internal_id, "
        "SUBSTR(CAST(sale_date AS TEXT),1,7) AS ym, SUM(qty_sold) AS qty "
        "FROM nst.sales_daily WHERE sale_date >= ? "
        "GROUP BY item_internal_id, SUBSTR(CAST(sale_date AS TEXT),1,7)",
        (MONTHS_FULL[0] + "-01",))
    if _d.empty:
        return pd.DataFrame(columns=["internal_id", "ym", "qty"])
    _d["internal_id"] = _d["internal_id"].astype(str)
    _d["qty"] = pd.to_numeric(_d["qty"], errors="coerce").fillna(0)
    return _d


def _inventory() -> pd.DataFrame:
    """jan → JD在库 / 在途（nst.inventory_snapshot 最新快照 · purchase_engine 同口径）。"""
    _d = _read(
        "SELECT im.jan AS jan, "
        "COALESCE(SUM(CASE WHEN inv.warehouse LIKE 'JD%' THEN inv.qty_on_hand ELSE 0 END),0) AS jd_on_hand, "
        "COALESCE(SUM(inv.qty_on_order),0) AS on_order "
        "FROM nst.inventory_snapshot inv "
        "JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id "
        "WHERE im.jan IS NOT NULL "
        "AND inv.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot) "
        "GROUP BY im.jan")
    if _d.empty:
        return pd.DataFrame(columns=["jan", "jd_on_hand", "on_order"])
    for _c in ("jd_on_hand", "on_order"):
        _d[_c] = pd.to_numeric(_d[_c], errors="coerce").fillna(0)
    return _d


def _sku_frame(ranks: list[str]) -> pd.DataFrame:
    """SKU 統一明細（tab 驾驶舱/SKU明细/优化机会 共用 → 跨 tab 数字一致）。

    列: internal_id, jan, display_name, maker, item_rank,
        main_supplier, main_price, cur_supplier, cur_rate,
        cur_price(主供价>最近PO单价), cur_src, plan_supplier(计划归属),
        msrp_taxex, best_supplier, best_price, best_validity, n_eligible, n_backup,
        n_quotes, qty_lm(直近完整月販), qty_3m(近3完整月合計), jd_on_hand, on_order,
        saving_unit, saving_amt(×qty_lm), flags。
    """
    f = _items_all(ranks)
    if f.empty:
        return f
    _ms = _read("SELECT jan, supplier_name AS main_supplier, price AS main_price "
                "FROM sourcing.item_main_supplier")
    f = (f.merge(_ms.drop_duplicates("jan"), on="jan", how="left") if not _ms.empty
         else f.assign(main_supplier=None, main_price=None))
    f["main_price"] = pd.to_numeric(f["main_price"], errors="coerce")
    f = f.merge(_latest_po_vendor(), on="internal_id", how="left")
    # 当前采购价 = 主供指定价 > 最近PO单价（spec: 前回購入原価は fallback にしない）
    f["cur_price"] = f["main_price"].fillna(f["cur_rate"])
    f["cur_src"] = pd.Series(
        ["main" if pd.notna(mp) else ("po" if pd.notna(cr) else None)
         for mp, cr in zip(f["main_price"], f["cur_rate"])], index=f.index)
    f["plan_supplier"] = f["main_supplier"].fillna(f["cur_supplier"])
    f = f.merge(_msrp(), on="jan", how="left")
    f["msrp_taxex"] = pd.to_numeric(f["msrp_taxex"], errors="coerce")
    # 最低有效报价 + 备用候选数
    _stq = _quotes_status()
    _best = sc.best_effective_quote(_stq)
    f = (f.merge(_best, on="jan", how="left") if not _best.empty
         else f.assign(best_supplier=None, best_price=None,
                       best_validity=None, n_eligible=0))
    f["best_price"] = pd.to_numeric(f.get("best_price"), errors="coerce")
    if not _stq.empty:
        _nq = _stq.groupby("jan").size().rename("n_quotes").reset_index()
        f = f.merge(_nq, on="jan", how="left")
        _el = _stq[_stq["eligible"]][["jan", "supplier_name"]].merge(
            f[["jan", "plan_supplier"]].drop_duplicates("jan"), on="jan", how="inner")
        _nb = (_el[_el["supplier_name"].astype(str)
                   != _el["plan_supplier"].astype(str)]
               .groupby("jan")["supplier_name"].nunique().rename("n_backup").reset_index())
        f = f.merge(_nb, on="jan", how="left")
    for _c, _dv in (("n_quotes", 0), ("n_backup", 0), ("n_eligible", 0)):
        f[_c] = pd.to_numeric(f.get(_c), errors="coerce").fillna(_dv).astype(int)
    # 販売数（直近完整月 / 近3完整月）
    _sd = _sales_m13()
    if not _sd.empty:
        _l3 = MONTHS_FULL[-3:]
        _q3 = (_sd[_sd["ym"].isin(_l3)].groupby("internal_id")["qty"]
               .sum().rename("qty_3m").reset_index())
        _ql = (_sd[_sd["ym"] == MONTHS_FULL[-1]].groupby("internal_id")["qty"]
               .sum().rename("qty_lm").reset_index())
        f = f.merge(_q3, on="internal_id", how="left").merge(_ql, on="internal_id", how="left")
    for _c in ("qty_3m", "qty_lm"):
        f[_c] = pd.to_numeric(f.get(_c), errors="coerce").fillna(0)
    # 在库/在途
    f = f.merge(_inventory(), on="jan", how="left")
    for _c in ("jd_on_hand", "on_order"):
        f[_c] = pd.to_numeric(f.get(_c), errors="coerce").fillna(0)
    # 理论节省（機会測算 · spec: (当前采购价−最低有效报价)×対応需要）
    _diff = f["cur_price"] - f["best_price"]
    f["saving_unit"] = _diff.where(_diff > 0)
    f["saving_amt"] = (f["saving_unit"] * f["qty_lm"]).fillna(0.0)

    def _flags(r) -> str:
        fl = []
        if pd.notna(r["best_price"]) and pd.notna(r["cur_price"]) \
                and r["best_price"] < r["cur_price"]:
            fl.append(_dl("可谈价", "交渉余地"))
        if r["n_backup"] == 0:
            fl.append(_dl("无备用", "予備なし"))
        if r["n_quotes"] > 0 and pd.isna(r["best_price"]):
            fl.append(_dl("报价过期/无效", "見積失効"))
        if pd.isna(r["cur_price"]):
            fl.append(_dl("数据不足(无当前价)", "データ不足(現行価なし)"))
        if r.get("best_validity") == "unconfirmed":
            fl.append(sc.VALIDITY_LABELS["unconfirmed"] if not _ja else "有効性未確認")
        return "·".join(fl)

    f["flags"] = f.apply(_flags, axis=1)
    return f


def _supplier_rules() -> pd.DataFrame:
    return _read("SELECT supplier_name, min_order_amount, free_ship_threshold, "
                 "default_lead_days, is_prepay, active FROM sourcing.supplier")


_RANK_OPTS = ["Aランク", "Bランク", "Cランク", "NEW"]

tab_cp, tab_bs, tab_wr, tab_sku, tab_opp, tab_up, tab_rule = st.tabs([
    t("📊 供应商驾驶舱"), t("🏷️ 品牌×供应商"), t("📉 折扣率总览"),
    t("📋 SKU采购明细"), t("🔍 优化机会"), t("📤 报价维护"),
    t("🏢 供应商与品牌规则")])

_CHART_LABEL_FS, _CHART_TITLE_FS = 10, 11

# ============================================================
# Tab 1：📊 供应商驾驶舱
# ============================================================
with tab_cp:
    st.caption(_dl(
        "每个供应商的月度需求基线 / 计划基线金额（当月销量×当前采购价·当前价=主供指定价否则最近PO单价）/ "
        "实际PO金额（trandate自然月 Σ单价×数量）/ 差异 / 起订·免运缺口（对计划基线）/ "
        "理论节省（最低有效报价的机会测算·不构成切换或凑单建议）。",
        "仕入先ごとの月次需要ベース / 計画ベース金額（当月販売数×現行仕入価·現行価=主仕入先指定価→直近PO単価）/ "
        "実際PO金額（trandate 自然月 Σ単価×数量）/ 差異 / 最低発注·送料無料ギャップ（計画ベース比）/ "
        "理論節約（最安有効見積の機会試算·切替や取り纏めの指示ではない）。"))
    _cp_ranks = st.multiselect(t("等级"), _RANK_OPTS,
                               default=["Aランク", "Bランク", "Cランク"], key="cp_ranks")
    _cp_ym = st.selectbox(t("対象月"), list(reversed(ALL_YMS)),
                          index=1, format_func=_ym_label, key="cp_ym")
    if _cp_ym == _CUR_YM:
        st.caption("ℹ️ " + _dl("当月为截至今日的部分数据，不宜与完整月直接比较",
                               "当月は本日までの部分データ · 完全月とは直接比較しない"))
    _f = _sku_frame(_cp_ranks or _RANK_OPTS[:3])
    _po = _po12()
    if _f.empty:
        st.info(t("无数据（NST item_master 未就绪？）"))
    else:
        # 対象月販売数 → 需求基线
        _sd = _sales_m13()
        _qm = (_sd[_sd["ym"] == _cp_ym].groupby("internal_id")["qty"]
               .sum().rename("qty_m").reset_index()
               if not _sd.empty else pd.DataFrame(columns=["internal_id", "qty_m"]))
        _fm = _f.merge(_qm, on="internal_id", how="left")
        _fm["qty_m"] = pd.to_numeric(_fm["qty_m"], errors="coerce").fillna(0)
        _fm["plan_amt"] = (_fm["qty_m"] * _fm["cur_price"]).fillna(0.0)
        _fm["saving_m"] = (_fm["saving_unit"] * _fm["qty_m"]).fillna(0.0)
        _fm["short_risk"] = ((_fm["jd_on_hand"] + _fm["on_order"]) < _fm["qty_m"]) \
            & (_fm["qty_m"] > 0)
        _has_sup = _fm[_fm["plan_supplier"].notna()
                       & (_fm["plan_supplier"].astype(str).str.strip() != "")]
        _nosup = len(_fm) - len(_has_sup)

        _g = (_has_sup.groupby("plan_supplier", as_index=False)
              .agg(sku_n=("jan", "nunique"), qty_m=("qty_m", "sum"),
                   plan_amt=("plan_amt", "sum"), saving=("saving_m", "sum"),
                   saving_sku=("saving_m", lambda s: int((s > 0).sum())),
                   risk_sku=("short_risk", "sum")))
        # 実際PO（対象月 · PO 実際仕入先）
        _act = (_po[_po["ym"] == _cp_ym].groupby("supplier_name", as_index=False)
                .agg(actual_amt=("amount", "sum"))
                if not _po.empty else pd.DataFrame(columns=["supplier_name", "actual_amt"]))
        _g = _g.merge(_act, left_on="plan_supplier", right_on="supplier_name",
                      how="outer")
        _g["plan_supplier"] = _g["plan_supplier"].fillna(_g["supplier_name"])
        _g = _g.drop(columns=["supplier_name"])
        for _c in ("sku_n", "qty_m", "plan_amt", "saving", "saving_sku",
                   "risk_sku", "actual_amt"):
            _g[_c] = pd.to_numeric(_g[_c], errors="coerce").fillna(0)
        _g["diff"] = _g["actual_amt"] - _g["plan_amt"]
        # 起订/免运缺口（供应商默认规则 vs 计划基线金额）
        _rl = _supplier_rules()
        if not _rl.empty:
            _g = _g.merge(_rl[["supplier_name", "min_order_amount", "free_ship_threshold"]],
                          left_on="plan_supplier", right_on="supplier_name", how="left")
            _g = _g.drop(columns=["supplier_name"])
        else:
            _g["min_order_amount"] = None
            _g["free_ship_threshold"] = None
        for _c in ("min_order_amount", "free_ship_threshold"):
            _g[_c] = pd.to_numeric(_g[_c], errors="coerce")
        _g["moq_gap"] = (_g["min_order_amount"] - _g["plan_amt"]).where(
            _g["min_order_amount"].notna() & (_g["min_order_amount"] > _g["plan_amt"]))
        _g["ship_gap"] = (_g["free_ship_threshold"] - _g["plan_amt"]).where(
            _g["free_ship_threshold"].notna() & (_g["free_ship_threshold"] > _g["plan_amt"]))
        _g = _g.sort_values("plan_amt", ascending=False)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric(t("供货商数"), f"{len(_g):,}")
        k2.metric(_dl("计划基线合计", "計画ベース合計"), f"¥{_g['plan_amt'].sum():,.0f}")
        k3.metric(_dl("实际PO合计", "実際PO合計"), f"¥{_g['actual_amt'].sum():,.0f}")
        k4.metric(_dl("理论节省合计", "理論節約合計"), f"¥{_g['saving'].sum():,.0f}")

        _show = _g.rename(columns={
            "plan_supplier": t("供货商"), "sku_n": t("SKU数"),
            "qty_m": _dl("需求数量", "需要数量"),
            "plan_amt": _dl("计划基线金额", "計画ベース金額"),
            "actual_amt": _dl("实际PO金额", "実際PO金額"),
            "diff": _dl("差异(实际-计划)", "差異(実際-計画)"),
            "moq_gap": _dl("起订缺口", "最低発注ギャップ"),
            "ship_gap": _dl("免运缺口", "送料無料ギャップ"),
            "risk_sku": _dl("缺货风险SKU", "欠品リスクSKU"),
            "saving": _dl("理论节省", "理論節約"),
            "saving_sku": _dl("节省SKU数", "節約SKU数"),
        })[[t("供货商"), t("SKU数"), _dl("需求数量", "需要数量"),
            _dl("计划基线金额", "計画ベース金額"), _dl("实际PO金额", "実際PO金額"),
            _dl("差异(实际-计划)", "差異(実際-計画)"),
            _dl("起订缺口", "最低発注ギャップ"), _dl("免运缺口", "送料無料ギャップ"),
            _dl("缺货风险SKU", "欠品リスクSKU"),
            _dl("理论节省", "理論節約"), _dl("节省SKU数", "節約SKU数")]]
        st.dataframe(
            _show, hide_index=True, use_container_width=True, height=520,
            column_config={c: st.column_config.NumberColumn(format="localized")
                           for c in [_dl("计划基线金额", "計画ベース金額"),
                                     _dl("实际PO金额", "実際PO金額"),
                                     _dl("差异(实际-计划)", "差異(実際-計画)"),
                                     _dl("起订缺口", "最低発注ギャップ"),
                                     _dl("免运缺口", "送料無料ギャップ"),
                                     _dl("理论节省", "理論節約")]})
        st.download_button(t("📥 CSV"), _show.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"supplier_cockpit_{_cp_ym}.csv",
                           mime="text/csv", key="cp_csv")
        if _nosup:
            st.caption("⚠️ " + _dl(
                f"{_nosup} 个 SKU 无计划归属供应商（无主供指定且无 PO 履历），未计入上表",
                f"{_nosup} SKU は計画帰属仕入先なし（主仕入先指定も PO 履歴もなし）· 上表未計上"))

        _pick = st.selectbox(_dl("供应商 SKU 下钻", "仕入先 SKU ドリルダウン"),
                             [""] + _g["plan_supplier"].dropna().astype(str).tolist(),
                             key="cp_pick")
        if _pick:
            _dsk = _fm[_fm["plan_supplier"].astype(str) == _pick].copy()
            _dsk = _dsk.sort_values("plan_amt", ascending=False)
            _dshow = _dsk[["jan", "display_name", "item_rank", "qty_m", "cur_price",
                           "plan_amt", "best_supplier", "best_price", "saving_m",
                           "jd_on_hand", "on_order", "flags"]].rename(columns={
                "jan": t("JAN"), "display_name": t("商品名"), "item_rank": t("RANK"),
                "qty_m": _dl("需求数量", "需要数量"), "cur_price": _dl("当前价", "現行価"),
                "plan_amt": _dl("计划金额", "計画金額"),
                "best_supplier": _dl("最低报价供应商", "最安見積先"),
                "best_price": _dl("最低有效报价", "最安有効見積"),
                "saving_m": _dl("理论节省", "理論節約"),
                "jd_on_hand": _dl("库存", "在庫"), "on_order": _dl("在途", "発注残"),
                "flags": _dl("状态", "状態")})
            st.dataframe(_dshow, hide_index=True, use_container_width=True, height=420,
                         column_config={
                             _dl("当前价", "現行価"): st.column_config.NumberColumn(format="¥%.0f"),
                             _dl("最低有效报价", "最安有効見積"): st.column_config.NumberColumn(format="¥%.0f"),
                             _dl("计划金额", "計画金額"): st.column_config.NumberColumn(format="localized"),
                             _dl("理论节省", "理論節約"): st.column_config.NumberColumn(format="localized"),
                         })
            st.download_button(t("📥 明细 CSV"),
                               _dshow.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"supplier_sku_{_pick}_{_cp_ym}.csv",
                               mime="text/csv", key="cp_sku_csv")

# ============================================================
# Tab 2：🏷️ 品牌×供应商
# ============================================================
with tab_bs:
    st.caption(_dl(
        "选择品牌 → 近12个月各供应商：采购金额/数量（PO实绩）、金额加权折扣率、SKU覆盖、"
        "最后采购月、最低报价差异（该品牌归属该供应商 SKU 的理论节省）。"
        "供谈判/集中采购/备用供应商管理参考，不作为自动改供应商依据。",
        "ブランド選択 → 直近12ヶ月の各仕入先：仕入金額/数量（PO実績）、金額加重掛率、SKU カバー、"
        "最終仕入月、最安見積差異。交渉・集中仕入・予備仕入先管理の参考であり、自動切替の根拠にしない。"))
    _po_b = _po12()
    if _po_b.empty:
        st.info(t("无数据（NST item_master / 近12个月 PO 未就绪？）"))
    else:
        _mks = sorted(_po_b["maker"].dropna().astype(str).unique().tolist())
        _bk = st.selectbox(t("品牌"), [""] + _mks, key="bs_brand")
        if not _bk:
            # 品牌一覧（概要）
            _ov = (_po_b[_po_b["maker"].notna()]
                   .groupby("maker", as_index=False)
                   .agg(amount=("amount", "sum"),
                        sup_n=("supplier_name", "nunique"),
                        sku_n=("jan", "nunique")))
            _ov = _ov.sort_values("amount", ascending=False)
            st.dataframe(_ov.rename(columns={
                "maker": t("品牌"), "amount": _dl("近12月采购金额", "直近12ヶ月仕入金額"),
                "sup_n": t("供货商数"), "sku_n": t("SKU数")}),
                hide_index=True, use_container_width=True, height=480,
                column_config={_dl("近12月采购金额", "直近12ヶ月仕入金額"):
                               st.column_config.NumberColumn(format="localized")})
            st.caption(_dl("↑ 选择上方品牌查看供应商构成与折扣率趋势",
                           "↑ 上のブランドを選択すると仕入先構成と掛率トレンドを表示"))
        else:
            _bdf = _po_b[_po_b["maker"].astype(str) == _bk].copy()
            # 供应商汇总（近12月）
            _has_m = _bdf["msrp_taxex"] > 0
            _bdf["_amt_m"] = _bdf["amount"].where(_has_m, 0.0)
            _bdf["_msrp_amt"] = (_bdf["msrp_taxex"] * _bdf["quantity"]).where(_has_m, 0.0)
            _gb = (_bdf.groupby("supplier_name", as_index=False)
                   .agg(amount=("amount", "sum"), qty=("quantity", "sum"),
                        sku_n=("jan", "nunique"), amt_m=("_amt_m", "sum"),
                        msrp_amt=("_msrp_amt", "sum"), last_ym=("ym", "max")))
            _gb["wari_w"] = (_gb["amt_m"] / _gb["msrp_amt"].where(_gb["msrp_amt"] > 0)) * 10
            _gb["coverage"] = _gb["amt_m"] / _gb["amount"].where(_gb["amount"] > 0)
            # 最低报价差异 = 该品牌 SKU 中计划归属该供应商的理论节省合计
            _fb = _sku_frame(_RANK_OPTS)
            _fb = _fb[_fb["maker"].astype(str) == _bk]
            _sv = (_fb.groupby("plan_supplier", as_index=False)["saving_amt"].sum()
                   .rename(columns={"plan_supplier": "supplier_name",
                                    "saving_amt": "saving"})
                   if not _fb.empty else pd.DataFrame(columns=["supplier_name", "saving"]))
            _gb = _gb.merge(_sv, on="supplier_name", how="left")
            _gb["saving"] = pd.to_numeric(_gb["saving"], errors="coerce").fillna(0)
            _gb = _gb.sort_values("amount", ascending=False)
            st.dataframe(_gb[["supplier_name", "amount", "qty", "wari_w", "coverage",
                              "sku_n", "last_ym", "saving"]].rename(columns={
                "supplier_name": t("供货商"),
                "amount": _dl("近12月采购金额", "直近12ヶ月仕入金額"),
                "qty": _dl("采购数量", "仕入数量"),
                "wari_w": _dl("加权折扣率", "加重掛率"),
                "coverage": _dl("MSRP覆盖率", "MSRP カバー率"),
                "sku_n": t("SKU数"), "last_ym": _dl("最后采购月", "最終仕入月"),
                "saving": _dl("最低报价差异(理论节省)", "最安見積差異(理論節約)")}),
                hide_index=True, use_container_width=True,
                column_config={
                    _dl("近12月采购金额", "直近12ヶ月仕入金額"): st.column_config.NumberColumn(format="localized"),
                    _dl("采购数量", "仕入数量"): st.column_config.NumberColumn(format="localized"),
                    _dl("加权折扣率", "加重掛率"): st.column_config.NumberColumn(format="%.2f折"),
                    _dl("MSRP覆盖率", "MSRP カバー率"): st.column_config.NumberColumn(format="percent"),
                    _dl("最低报价差异(理论节省)", "最安見積差異(理論節約)"): st.column_config.NumberColumn(format="localized"),
                })
            # 月度金额构成（堆叠柱）+ 加权折扣率曲线
            _mw = sc.monthly_weighted_wari(_bdf, ["supplier_name"])
            if not _mw.empty:
                _c1 = alt.Chart(_mw).mark_bar().encode(
                    x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=0)),
                    y=alt.Y("amount:Q", title=_dl("采购金额", "仕入金額")),
                    color=alt.Color("supplier_name:N", title=None,
                                    legend=alt.Legend(orient="top")),
                    tooltip=["ym:N", "supplier_name:N",
                             alt.Tooltip("amount:Q", format=",.0f")],
                ).properties(height=240).configure_axis(
                    labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
                st.altair_chart(_c1, use_container_width=True)
                _mw2 = _mw[_mw["wari_w"].notna()]
                if not _mw2.empty:
                    _c2 = alt.Chart(_mw2).mark_line(point=True).encode(
                        x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=0)),
                        y=alt.Y("wari_w:Q", title=_dl("加权折扣率(折)", "加重掛率(掛)"),
                                scale=alt.Scale(zero=False)),
                        color=alt.Color("supplier_name:N", title=None,
                                        legend=alt.Legend(orient="top")),
                        tooltip=["ym:N", "supplier_name:N",
                                 alt.Tooltip("wari_w:Q", format=".2f"),
                                 alt.Tooltip("coverage:Q", format=".0%")],
                    ).properties(height=240).configure_axis(
                        labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
                    st.altair_chart(_c2, use_container_width=True)
            st.caption(_dl(
                f"最右月份为当月（截至今日）· 加权折扣率=Σ(单价×数量)÷Σ(MSRP税抜×数量)×10,仅MSRP完整PO行 · "
                f"覆盖率下降时不要把折扣率改善解读为价格改善",
                "最右月は当月（本日まで）· 加重掛率=Σ(単価×数量)÷Σ(MSRP税抜×数量)×10（MSRP有りPO行のみ）· "
                "カバー率低下時は掛率改善を価格改善と解釈しない"))

# ============================================================
# Tab 3：📉 折扣率总览（四层月度加权 wari）
# ============================================================
with tab_wr:
    st.caption(_dl(
        "四层同口径：全体 / ABC等级 / 品牌×供应商 / SKU×供应商 · "
        "金额加权折扣率=Σ(PO单价×数量)÷Σ(MSRP税抜×数量)×10（仅MSRP完整PO行）· "
        "每层同时显示 MSRP 金额覆盖率——覆盖率下降时折扣率变化不能解读为价格改善。",
        "4 層同一口径：全体 / ABC ランク / ブランド×仕入先 / SKU×仕入先 · "
        "金額加重掛率=Σ(PO単価×数量)÷Σ(MSRP税抜×数量)×10（MSRP有りPO行のみ）· "
        "各層に MSRP 金額カバー率を併記——カバー率低下時の掛率変化は価格改善と読まない。"))
    _po_w = _po12()
    if _po_w.empty:
        st.info(t("无数据（NST item_master / 近12个月 PO 未就绪？）"))
    else:
        # ── Layer① 全体 ──
        _w0 = sc.monthly_weighted_wari(_po_w, [])
        _w0f = _w0[_w0["ym"].isin(MONTHS_FULL)]
        if len(_w0f) >= 2:
            _last, _prev = _w0f.iloc[-1], _w0f.iloc[-2]
            k1, k2, k3 = st.columns(3)
            _delta = (f"{_last['wari_w'] - _prev['wari_w']:+.2f}"
                      if pd.notna(_last["wari_w"]) and pd.notna(_prev["wari_w"]) else None)
            k1.metric(_dl(f"直近完整月折扣率({_last['ym']})",
                          f"直近完全月掛率({_last['ym']})"),
                      f"{_last['wari_w']:.2f}" if pd.notna(_last["wari_w"]) else "—",
                      _delta, delta_color="inverse")
            k2.metric(_dl("MSRP金额覆盖率", "MSRP 金額カバー率"),
                      f"{_last['coverage'] * 100:.1f}%" if pd.notna(_last["coverage"]) else "—")
            k3.metric(_dl("当月PO金额", "当月PO金額"), f"¥{_last['amount']:,.0f}")
        if not _w0.empty:
            _w0p = _w0[_w0["wari_w"].notna()]
            _l1 = alt.Chart(_w0p).mark_line(point=True, color="#4F46E5").encode(
                x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("wari_w:Q", title=_dl("全体加权折扣率(折)", "全体加重掛率(掛)"),
                        scale=alt.Scale(zero=False)),
                tooltip=["ym:N", alt.Tooltip("wari_w:Q", format=".2f"),
                         alt.Tooltip("coverage:Q", format=".0%"),
                         alt.Tooltip("amount:Q", format=",.0f")],
            ).properties(height=240).configure_axis(
                labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
            st.altair_chart(_l1, use_container_width=True)

        # ── Layer② 等级别 ──
        st.markdown("##### " + _dl("等级别折扣率", "ランク別掛率"))
        _w2 = sc.monthly_weighted_wari(
            _po_w[_po_w["item_rank"].isin(["Aランク", "Bランク", "Cランク"])], ["item_rank"])
        if _w2.empty:
            st.info(t("この条件のデータがありません"))
        else:
            _w2p = _w2[_w2["wari_w"].notna()]
            _l2 = alt.Chart(_w2p).mark_line(point=True).encode(
                x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("wari_w:Q", title=_dl("加权折扣率(折)", "加重掛率(掛)"),
                        scale=alt.Scale(zero=False)),
                color=alt.Color("item_rank:N", title=None, legend=alt.Legend(orient="top")),
                tooltip=["ym:N", "item_rank:N", alt.Tooltip("wari_w:Q", format=".2f"),
                         alt.Tooltip("coverage:Q", format=".0%")],
            ).properties(height=240).configure_axis(
                labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
            st.altair_chart(_l2, use_container_width=True)
            _pv2 = _w2.pivot_table(index="item_rank", columns="ym", values="wari_w")
            _pv2 = _pv2.reindex(columns=[c for c in ALL_YMS if c in _pv2.columns]).round(2)
            st.dataframe(_pv2.reset_index().rename(columns={"item_rank": t("RANK")}),
                         hide_index=True, use_container_width=True)

        # ── Layer③ 品牌×供应商（跳到品牌tab看图·这里给月度表） ──
        st.markdown("##### " + _dl("品牌×供应商（选择品牌）", "ブランド×仕入先（ブランド選択）"))
        _mks_w = sorted(_po_w["maker"].dropna().astype(str).unique().tolist())
        _bk_w = st.selectbox(t("品牌"), [""] + _mks_w, key="wr_brand")
        if _bk_w:
            _w3 = sc.monthly_weighted_wari(
                _po_w[_po_w["maker"].astype(str) == _bk_w], ["supplier_name"])
            _pv3 = _w3.pivot_table(index="supplier_name", columns="ym", values="wari_w")
            _pv3 = _pv3.reindex(columns=[c for c in ALL_YMS if c in _pv3.columns]).round(2)
            st.dataframe(_pv3.reset_index().rename(columns={"supplier_name": t("供货商")}),
                         hide_index=True, use_container_width=True)

        # ── Layer④ SKU×供应商 ──
        st.markdown("##### " + _dl("SKU×供应商（JAN/商品名搜索）", "SKU×仕入先（JAN/商品名検索）"))
        _kw_w = st.text_input(t("🔍 JAN / 商品名 搜索"), key="wr_kw")
        if _kw_w.strip():
            _k = _kw_w.strip()
            _w4src = _po_w[_po_w["jan"].astype(str).str.contains(_k, na=False)
                           | _po_w["display_name"].astype(str)
                           .str.contains(_k, case=False, na=False)]
            if _w4src.empty:
                st.info(t("この条件のデータがありません"))
            else:
                _w4 = sc.monthly_weighted_wari(_w4src, ["jan", "display_name",
                                                        "supplier_name"])
                _w4p = _w4[_w4["wari_w"].notna()]
                if not _w4p.empty:
                    _w4p = _w4p.assign(
                        _lbl=_w4p["supplier_name"].astype(str) + "·"
                        + _w4p["jan"].astype(str))
                    _l4 = alt.Chart(_w4p).mark_line(point=True).encode(
                        x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=0)),
                        y=alt.Y("wari_w:Q", title=_dl("加权折扣率(折)", "加重掛率(掛)"),
                                scale=alt.Scale(zero=False)),
                        color=alt.Color("_lbl:N", title=None,
                                        legend=alt.Legend(orient="top")),
                        tooltip=["ym:N", "supplier_name:N", "jan:N", "display_name:N",
                                 alt.Tooltip("wari_w:Q", format=".2f")],
                    ).properties(height=240).configure_axis(
                        labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
                    st.altair_chart(_l4, use_container_width=True)
                st.dataframe(
                    _w4[["jan", "display_name", "supplier_name", "ym", "wari_w",
                         "amount", "coverage"]].rename(columns={
                        "jan": t("JAN"), "display_name": t("商品名"),
                        "supplier_name": t("供货商"), "ym": t("月"),
                        "wari_w": _dl("加权折扣率", "加重掛率"),
                        "amount": _dl("采购金额", "仕入金額"),
                        "coverage": _dl("MSRP覆盖率", "MSRP カバー率")}),
                    hide_index=True, use_container_width=True, height=320,
                    column_config={
                        _dl("加权折扣率", "加重掛率"): st.column_config.NumberColumn(format="%.2f折"),
                        _dl("采购金额", "仕入金額"): st.column_config.NumberColumn(format="localized"),
                        _dl("MSRP覆盖率", "MSRP カバー率"): st.column_config.NumberColumn(format="percent"),
                    })

# ============================================================
# Tab 4：📋 SKU采购明细
# ============================================================
with tab_sku:
    st.caption(_dl(
        "每 SKU：主供应商 / 最近PO供应商 / 最低有效报价 / 当前价（主供价否则最近PO单价）/ "
        "价差 / 近三月销量 / 库存 / 在途 · 状态标记：可谈价·无备用·报价过期·数据不足·有效性未确认。",
        "SKU ごと：主仕入先 / 直近PO仕入先 / 最安有効見積 / 現行価（主仕入先価→直近PO単価）/ "
        "価差 / 直近3ヶ月販売 / 在庫 / 発注残 · 状態タグ：交渉余地·予備なし·見積失効·データ不足·有効性未確認。"))
    _ranks_s = st.multiselect(t("等级"), _RANK_OPTS,
                              default=["Aランク", "Bランク", "Cランク"], key="sku_ranks")
    _fs = _sku_frame(_ranks_s or _RANK_OPTS[:3])
    if _fs.empty:
        st.info(t("无该等级商品（NST item_master 未就绪？）"))
    else:
        _c1, _c2, _c3 = st.columns([1, 1, 2])
        _only_op = _c1.checkbox(_dl("只看可谈价", "交渉余地のみ"), value=False, key="sku_onlyop")
        _only_qt = _c2.checkbox(t("只看有报价的"), value=False, key="sku_onlyq")
        _kw_s = _c3.text_input(t("🔍 JAN / 商品名 搜索"), key="sku_kw")
        _v = _fs
        if _only_op:
            _v = _v[_v["saving_unit"].notna()]
        if _only_qt:
            _v = _v[_v["n_quotes"] > 0]
        if _kw_s.strip():
            _k = _kw_s.strip()
            _v = _v[_v["jan"].astype(str).str.contains(_k, na=False)
                    | _v["display_name"].astype(str).str.contains(_k, case=False, na=False)]
            if _v.empty:
                _k2 = f"%{_k}%"
                _im_hit = _read("SELECT item_rank FROM nst.item_master_raw "
                                "WHERE jan LIKE ? OR display_name LIKE ?", (_k2, _k2))
                _sq_hit = _read("SELECT COUNT(*) AS n FROM sourcing.supplier_quote "
                                "WHERE jan LIKE ?", (_k2,))
                _nq = int(_sq_hit["n"].iloc[0]) if not _sq_hit.empty else 0
                if not _im_hit.empty:
                    _rk = "、".join(sorted(
                        {str(x) if pd.notna(x) and str(x).strip() else "（空）"
                         for x in _im_hit["item_rank"]}))
                    st.warning(t("商品在 NST 主档但等级={r}，不在当前等级筛选 → "
                                 "上方「等级」多选加上即可显示。该关键词报价库有 {n} 条报价。"
                                 ).format(r=_rk, n=_nq))
                elif _nq:
                    st.warning(t("报价库有 {n} 条该关键词报价，但商品不在 NST 商品主档 → "
                                 "仕入詳細以 NST 主档为底，暂无法显示该行。").format(n=_nq))

        k1, k2, k3, k4 = st.columns(4)
        k1.metric(t("商品数"), f"{len(_v):,}")
        k2.metric(_dl("可谈价", "交渉余地"), f"{int(_v['saving_unit'].notna().sum()):,}")
        k3.metric(_dl("无备用", "予備なし"), f"{int((_v['n_backup'] == 0).sum()):,}")
        k4.metric(_dl("数据不足", "データ不足"), f"{int(_v['cur_price'].isna().sum()):,}")

        _v = _v.sort_values("saving_amt", ascending=False)
        _cols_s = {"item_rank": t("RANK"), "jan": t("JAN"), "display_name": t("商品名"),
                   "main_supplier": t("主供货商"),
                   "cur_supplier": _dl("最近PO供应商", "直近PO仕入先"),
                   "cur_price": _dl("当前价", "現行価"),
                   "best_supplier": _dl("最低报价供应商", "最安見積先"),
                   "best_price": _dl("最低有效报价", "最安有効見積"),
                   "saving_unit": _dl("价差", "価差"),
                   "qty_3m": _dl("近三月销量", "直近3ヶ月販売"),
                   "jd_on_hand": _dl("库存", "在庫"), "on_order": _dl("在途", "発注残"),
                   "flags": _dl("状态", "状態")}
        _shows = _v[list(_cols_s.keys())].rename(columns=_cols_s)
        st.dataframe(
            _shows, hide_index=True, use_container_width=True, height=560,
            column_config={
                _dl("当前价", "現行価"): st.column_config.NumberColumn(format="¥%.0f"),
                _dl("最低有效报价", "最安有効見積"): st.column_config.NumberColumn(format="¥%.0f"),
                _dl("价差", "価差"): st.column_config.NumberColumn(format="¥%.0f"),
                _dl("近三月销量", "直近3ヶ月販売"): st.column_config.NumberColumn(format="%.0f"),
                _dl("库存", "在庫"): st.column_config.NumberColumn(format="%.0f"),
                _dl("在途", "発注残"): st.column_config.NumberColumn(format="%.0f"),
            })
        st.download_button(t("📥 CSV"), _shows.to_csv(index=False).encode("utf-8-sig"),
                           file_name="sku_sourcing_detail.csv", mime="text/csv",
                           key="sku_csv")
        st.caption(_dl(
            "当前价=主供指定价否则最近PO单价（两者皆无=数据不足）· 最低有效报价仅含启用供应商的"
            "有效/有效性未确认报价 · 价差>0=存在更便宜有效报价（理论机会,非指令）",
            "現行価=主仕入先指定価→直近PO単価（両方なし=データ不足）· 最安有効見積は稼働仕入先の"
            "有効/有効性未確認見積のみ · 価差>0=より安い有効見積あり（理論上の機会·指示ではない）"))

# ============================================================
# Tab 5：🔍 优化机会
# ============================================================
with tab_opp:
    st.caption(_dl(
        "两类问题分开：①毛利问题=采购折扣率过高（当前价÷MSRP税抜×10 ≥ 阈值）"
        "②采购机会=当前价高于最低有效报价（可谈价/换报价）。",
        "2 種類を区別：①粗利問題=仕入掛率が高すぎ（現行価÷MSRP税抜×10 ≥ 閾値）"
        "②仕入機会=現行価が最安有効見積より高い（交渉/見積切替余地）。"))
    _ranks_o = st.multiselect(t("等级"), _RANK_OPTS,
                              default=["Aランク", "Bランク", "NEW"], key="opp_ranks")
    _fo = _sku_frame(_ranks_o or ["Aランク", "Bランク", "NEW"])
    if _fo.empty:
        st.info(t("无数据（NST item_master 未就绪？）"))
    else:
        _fo["wari_cur"] = sc.wari(_fo["cur_price"], _fo["msrp_taxex"])

        # ── ① 毛利问题（折扣率过高）──
        st.markdown("##### " + _dl("① 毛利问题（折扣率过高）", "① 粗利問題（掛率過高）"))
        _th = st.slider(t("見直し阈值（仕入折数 ≥ X 折 = 需重新谈价）"),
                        0.0, 10.0, 7.0, 0.5, key="opp_th")
        _hasw = _fo[_fo["wari_cur"].notna()].copy()
        _now = _fo[_fo["wari_cur"].isna() & _fo["cur_price"].notna()]
        _hasw["need"] = _hasw["wari_cur"] >= _th
        k1, k2, k3 = st.columns(3)
        k1.metric(t("有参考价"), f"{len(_hasw):,}")
        k2.metric(t("見直し必要"), f"{int(_hasw['need'].sum()):,}")
        k3.metric(t("无参考価"), f"{len(_now):,}")
        _vw = _hasw[_hasw["need"]].sort_values("wari_cur", ascending=False)
        _show_w = _vw[["item_rank", "jan", "display_name", "cur_price", "msrp_taxex",
                       "wari_cur", "plan_supplier"]].rename(columns={
            "item_rank": t("RANK"), "jan": t("JAN"), "display_name": t("商品名"),
            "cur_price": _dl("当前价", "現行価"),
            "msrp_taxex": t("建议零售价(税抜)"), "wari_cur": t("仕入折数"),
            "plan_supplier": _dl("归属供应商", "帰属仕入先")})
        st.dataframe(_show_w, hide_index=True, use_container_width=True, height=380,
                     column_config={
                         _dl("当前价", "現行価"): st.column_config.NumberColumn(format="¥%.0f"),
                         t("建议零售价(税抜)"): st.column_config.NumberColumn(format="¥%.0f"),
                         t("仕入折数"): st.column_config.NumberColumn(format="%.1f折"),
                     })
        st.download_button(t("📥 見直し必要 CSV"),
                           _show_w.to_csv(index=False).encode("utf-8-sig"),
                           file_name="msrp_review.csv", mime="text/csv", key="opp_csv1")
        if not _now.empty:
            with st.expander(t("无参考価商品一览") + f"（{len(_now):,}）"):
                st.dataframe(_now[["item_rank", "jan", "display_name", "cur_price"]]
                             .rename(columns={"item_rank": t("RANK"), "jan": t("JAN"),
                                              "display_name": t("商品名"),
                                              "cur_price": _dl("当前价", "現行価")}),
                             hide_index=True, use_container_width=True, height=280,
                             column_config={_dl("当前价", "現行価"):
                                            st.column_config.NumberColumn(format="¥%.0f")})

        st.divider()
        # ── ② 采购机会（当前价 > 最低有效报价）──
        st.markdown("##### " + _dl("② 采购机会（当前价 > 最低有效报价）",
                                   "② 仕入機会（現行価 > 最安有効見積）"))
        _op = _fo[_fo["saving_unit"].notna()].sort_values("saving_amt", ascending=False)
        k4, k5 = st.columns(2)
        k4.metric(_dl("机会SKU数", "機会SKU数"), f"{len(_op):,}")
        k5.metric(_dl("理论节省合计(按直近完整月销量)", "理論節約合計(直近完全月販売)"),
                  f"¥{_op['saving_amt'].sum():,.0f}")
        _show_p = _op[["item_rank", "jan", "display_name", "plan_supplier", "cur_price",
                       "best_supplier", "best_price", "saving_unit", "qty_lm",
                       "saving_amt", "best_validity", "flags"]].rename(columns={
            "item_rank": t("RANK"), "jan": t("JAN"), "display_name": t("商品名"),
            "plan_supplier": _dl("归属供应商", "帰属仕入先"),
            "cur_price": _dl("当前价", "現行価"),
            "best_supplier": _dl("最低报价供应商", "最安見積先"),
            "best_price": _dl("最低有效报价", "最安有効見積"),
            "saving_unit": _dl("价差", "価差"),
            "qty_lm": _dl("直近月销量", "直近月販売"),
            "saving_amt": _dl("理论节省", "理論節約"),
            "best_validity": _dl("报价有效性", "見積有効性"),
            "flags": _dl("状态", "状態")})
        st.dataframe(_show_p, hide_index=True, use_container_width=True, height=420,
                     column_config={
                         _dl("当前价", "現行価"): st.column_config.NumberColumn(format="¥%.0f"),
                         _dl("最低有效报价", "最安有効見積"): st.column_config.NumberColumn(format="¥%.0f"),
                         _dl("价差", "価差"): st.column_config.NumberColumn(format="¥%.0f"),
                         _dl("理论节省", "理論節約"): st.column_config.NumberColumn(format="localized"),
                         _dl("直近月销量", "直近月販売"): st.column_config.NumberColumn(format="%.0f"),
                     })
        st.download_button(t("📥 CSV"), _show_p.to_csv(index=False).encode("utf-8-sig"),
                           file_name="purchase_opportunity.csv", mime="text/csv",
                           key="opp_csv2")
        st.caption(_dl(
            "理论节省=(当前价−最低有效报价)×直近完整月销量,机会测算非可实现承诺 · "
            "报价有效性=有效/有效性未确认(有效期未设置的历史报价) · 不产生订货或切换指令",
            "理論節約=(現行価−最安有効見積)×直近完全月販売 · 機会試算であり実現保証ではない · "
            "見積有効性=有効/有効性未確認（有効期間未設定の歴史見積）· 発注/切替指示は生成しない"))

# ============================================================
# Tab 6：📤 报价维护（原 見積書UP 全保留 + 有效期/数据质量）
# ============================================================
with tab_up:
    # ── 数据质量摘要（spec: 报价日期/有效期/MOQ/箱规/运费规则 缺失可视）──
    _qall = _read("SELECT id, supplier_name, jan, price, quote_date, valid_from, "
                  "valid_to, moq, order_lot FROM sourcing.supplier_quote")
    if not _qall.empty:
        _lq_all = sc.latest_quotes(_qall)
        _sup_r = _supplier_rules()
        _br = _read("SELECT DISTINCT supplier_name FROM sourcing.supplier_brand_rule")
        _qs = (_lq_all.groupby("supplier_name")
               .agg(sku_n=("jan", "nunique"),
                    last_quote=("quote_date", "max"),
                    no_valid=("valid_to", lambda s: int(s.isna().sum())),
                    no_moq=("moq", lambda s: int(pd.to_numeric(s, errors="coerce").isna().sum())),
                    no_lot=("order_lot", lambda s: int(pd.to_numeric(s, errors="coerce").isna().sum())))
               .reset_index())
        if not _sup_r.empty:
            _qs = _qs.merge(_sup_r[["supplier_name", "free_ship_threshold"]],
                            on="supplier_name", how="left")
        else:
            _qs["free_ship_threshold"] = None
        _brset = set(_br["supplier_name"]) if not _br.empty else set()
        _qs["no_ship_rule"] = [
            "" if (pd.notna(v) or s in _brset) else "⚠️"
            for v, s in zip(pd.to_numeric(_qs["free_ship_threshold"], errors="coerce"),
                            _qs["supplier_name"])]
        with st.expander("📊 " + _dl("报价数据质量摘要（最新报价口径）",
                                     "見積データ品質サマリ（最新見積ベース）"), expanded=False):
            st.dataframe(_qs[["supplier_name", "sku_n", "last_quote", "no_valid",
                              "no_moq", "no_lot", "no_ship_rule"]].rename(columns={
                "supplier_name": t("供货商"), "sku_n": t("SKU数"),
                "last_quote": _dl("最新报价日", "最新見積日"),
                "no_valid": _dl("有效期未设", "有効期限未設定"),
                "no_moq": _dl("MOQ缺失", "MOQ 欠損"),
                "no_lot": _dl("箱规缺失", "箱規欠損"),
                "no_ship_rule": _dl("无运费规则", "送料ルールなし")}),
                hide_index=True, use_container_width=True, height=300)
            st.caption(_dl("有效期未设=看板按「有效性未确认」参与节省测算并明确标注",
                           "有効期限未設定=ダッシュボードでは「有効性未確認」として明示のうえ節約試算に参加"))

    st.markdown("##### " + t("✍️ 手动输入报价（少量更新）"))
    st.caption(t("少量产品直接在表格输入 → 写入报价库（追加记录留历史，比价自动取最新）。"
                 "供货商从主档选择；新供货商请走下方文件上传。"))
    _sup_opts = _read(
        "SELECT supplier_name FROM sourcing.supplier ORDER BY supplier_name")
    _sup_opts = (_sup_opts["supplier_name"].dropna().tolist()
                 if not _sup_opts.empty else [])
    _hand_seed = pd.DataFrame({
        "supplier_name": pd.Series(dtype="object"),
        "jan": pd.Series(dtype="object"),
        "item_name": pd.Series(dtype="object"),
        "price": pd.Series(dtype="float64"),
        "moq": pd.Series(dtype="float64"),
        "order_lot": pd.Series(dtype="float64"),
        "lead_days": pd.Series(dtype="float64"),
        "valid_from": pd.Series(dtype="object"),
        "valid_to": pd.Series(dtype="object"),
    })
    _hand = st.data_editor(
        _hand_seed, num_rows="dynamic", hide_index=True,
        use_container_width=True, key="sq_hand",
        column_config={
            "supplier_name": (st.column_config.SelectboxColumn(
                t("供货商"), options=_sup_opts, width="medium") if _sup_opts
                else st.column_config.TextColumn(t("供货商"))),
            "jan": st.column_config.TextColumn("JAN"),
            "item_name": st.column_config.TextColumn(t("品名（可选）")),
            "price": st.column_config.NumberColumn(
                t("仕入单价（税抜）"), min_value=0.0, format="%.2f"),
            "moq": st.column_config.NumberColumn(
                t("起订量"), min_value=0.0, format="%.0f"),
            "order_lot": st.column_config.NumberColumn(
                t("订货批量"), min_value=0.0, format="%.0f"),
            "lead_days": st.column_config.NumberColumn(
                t("纳期(日)"), min_value=0.0, format="%.0f"),
            "valid_from": st.column_config.DateColumn(_dl("有效期从", "有効期間自")),
            "valid_to": st.column_config.DateColumn(_dl("有效期至", "有効期間至")),
        })
    _hdate = st.date_input(t("报价日（整批适用）"), value=dt.date.today(),
                           key="sq_hand_date")
    if st.button(t("✅ 写入报价库"), key="sq_hand_write",
                 disabled=_hand.empty):
        _rows = _hand.copy()
        _rows["supplier_name"] = (_rows["supplier_name"].fillna("")
                                  .astype(str).str.strip())
        _rows["jan"] = _rows["jan"].fillna("").astype(str).str.strip()
        _valid = _rows[(_rows["supplier_name"] != "") & (_rows["jan"] != "")
                       & _rows["price"].notna()]
        _skip = len(_rows) - len(_valid)
        if _valid.empty:
            st.warning(t("没有可写入的行（需 供货商+JAN+单价）"))
        else:
            _valid = sc.apply_supplier_alias(_valid, _alias_map())
            _ensure_suppliers(_valid["supplier_name"].tolist())
            for _, _r in _valid.iterrows():
                _iname = _r.get("item_name")
                _iname = (None if pd.isna(_iname)
                          else (str(_iname).strip() or None))
                conn.execute(
                    "INSERT INTO sourcing.supplier_quote "
                    "(supplier_name, jan, item_name, price, moq, order_lot, "
                    " lead_days, quote_date, valid_from, valid_to, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(_r["supplier_name"]).strip(), str(_r["jan"]).strip(),
                     _iname, sc_num(_r.get("price")), sc_num(_r.get("moq")),
                     sc_num(_r.get("order_lot")), sc_int(_r.get("lead_days")),
                     _hdate.isoformat(), _date_or_none(_r.get("valid_from")),
                     _date_or_none(_r.get("valid_to")), "manual"))
            conn.commit()
            _msg = t("✅ 已写入 {n} 条报价").format(n=len(_valid))
            if _skip:
                _msg += " · " + t("跳过 {m} 行（缺 供货商/JAN/单价）").format(m=_skip)
            st.success(_msg)

    st.divider()
    st.markdown("##### " + t("📤 上传报价表"))
    st.caption(t("列名自动识别(供货商, 本地SKU/JAN, 仕入金额/采购价, 订货批量, 起订量, 纳期)。"))
    _up = st.file_uploader(t("报价文件（CSV / Excel）"), type=["csv", "xlsx"], key="sq_up")
    _qdate = st.date_input(t("报价日（整批适用）"), value=dt.date.today(), key="sq_date")
    if _up is not None:
        try:
            _raw = (pd.read_csv(io.BytesIO(_up.read()), dtype=str, keep_default_na=False)
                    if _up.name.lower().endswith(".csv")
                    else pd.read_excel(io.BytesIO(_up.read()), dtype=str))
        except Exception as e:  # noqa: BLE001
            st.error(t("解析失败") + f"\n\n{e}")
            _raw = None
        if _raw is not None:
            _norm, _miss = sc.normalize_upload(_raw)
            if "jan" in _norm.columns and "supplier_name" not in _miss and "price" not in _miss:
                # 供货商列缺失 → 让用户统一指派
                if "supplier_name" not in _norm.columns or _norm["supplier_name"].isna().all():
                    _sup_one = st.text_input(t("该文件的供货商名（文件无供货商列时填）"), key="sq_sup_one")
                    if _sup_one.strip():
                        _norm["supplier_name"] = _sup_one.strip()
                _norm = _norm[_norm["jan"].astype(str).str.strip() != ""]
                _norm = sc.apply_supplier_alias(_norm, _alias_map())
                st.dataframe(_norm.head(50), hide_index=True, use_container_width=True)
                st.caption(t("预览前 50 行 · 共 {n} 行").format(n=len(_norm)))
                if "supplier_name" in _norm.columns and st.button(
                        t("✅ 写入报价库"), key="sq_write"):
                    _ensure_suppliers(_norm["supplier_name"].tolist())
                    _ins = 0
                    for _, _r in _norm.iterrows():
                        _sup = str(_r.get("supplier_name", "")).strip()
                        _jan = str(_r.get("jan", "")).strip()
                        if not _sup or not _jan:
                            continue
                        conn.execute(
                            "INSERT INTO sourcing.supplier_quote "
                            "(supplier_name, jan, item_name, price, moq, order_lot, lead_days, "
                            " quote_date, source) VALUES (?,?,?,?,?,?,?,?,?)",
                            (_sup, _jan, _r.get("item_name"),
                             sc_num(_r.get("price")), sc_num(_r.get("moq")),
                             sc_num(_r.get("order_lot")), sc_int(_r.get("lead_days")),
                             _qdate.isoformat(), "upload"))
                        _ins += 1
                    conn.commit()
                    st.success(t("✅ 已写入 {n} 条报价").format(n=_ins))
            else:
                st.error(t("缺少必要列：") + "、".join(_miss)
                         + "｜" + t("实际列：") + "、".join(map(str, _raw.columns)))

    st.divider()
    st.markdown("##### " + t("📚 一键导入 仕入先管理リスト.xlsx（多供货商）"))
    st.caption(t("上传那个多 sheet 的 仕入先管理リスト → 自动逐供货商抽取 JAN/见积价/批量/起订金额。"))
    _ml = st.file_uploader(t("仕入先管理リスト（.xlsx）"), type=["xlsx"], key="sq_multi")
    _mdate = st.date_input(t("报价日（整批适用）"), value=dt.date.today(), key="sq_mdate")
    if _ml is not None:
        try:
            _xls = pd.ExcelFile(io.BytesIO(_ml.read()))
            _sheets = {_n: pd.read_excel(_xls, sheet_name=_n, header=None, dtype=str)
                       for _n in _xls.sheet_names}
        except Exception as e:  # noqa: BLE001
            st.error(t("解析失败") + f"\n\n{e}")
            _sheets = None
        if _sheets:
            _allq, _counts = sc.extract_vendor_quotes(_sheets)
            _allq = sc.apply_supplier_alias(_allq, _alias_map())
            _ok = {k: v for k, v in _counts.items() if v > 0}
            _zero = [k for k, v in _counts.items() if v == 0]
            st.success(t("抽出 {n} 条 · 供货商 {s} 家").format(n=len(_allq), s=len(_ok)))
            st.dataframe(pd.DataFrame(sorted(_counts.items(), key=lambda x: -x[1]),
                                      columns=[t("供货商sheet"), t("报价数")]),
                         hide_index=True, use_container_width=True, height=240)
            if _zero:
                st.caption("⚠️ " + t("0 条的 sheet（已跳过）: ") + "、".join(_zero))
            if not _allq.empty and st.button(t("✅ 全部写入报价库"), key="sq_multi_write"):
                _ensure_suppliers(_allq["supplier_name"].tolist())
                _params = [
                    (str(_r["supplier_name"]).strip(), str(_r["jan"]).strip(),
                     _r.get("item_name"), sc_num(_r.get("price")),
                     sc_num(_r.get("order_lot")), _mdate.isoformat(), "excel")
                    for _, _r in _allq.iterrows()
                    if str(_r.get("supplier_name", "")).strip() and str(_r.get("jan", "")).strip()
                ]
                conn.executemany(
                    "INSERT INTO sourcing.supplier_quote "
                    "(supplier_name, jan, item_name, price, order_lot, quote_date, source) "
                    "VALUES (?,?,?,?,?,?,?)", _params)
                # 注文最低金额（供货商级起订金额）→ supplier 主档（未设的才填，不覆盖手动）
                _ma = _allq.copy()
                _ma["min_order_amount"] = pd.to_numeric(_ma["min_order_amount"], errors="coerce")
                for _sup, _amt in _ma.dropna(subset=["min_order_amount"]).groupby(
                        "supplier_name")["min_order_amount"].max().items():
                    conn.execute(
                        "UPDATE sourcing.supplier SET min_order_amount=? "
                        "WHERE supplier_name=? AND (min_order_amount IS NULL OR min_order_amount=0)",
                        (float(_amt), str(_sup)))
                conn.commit()
                st.success(t("✅ 已写入 {n} 条报价 · 起订金额已回填供货商主档").format(n=len(_params)))

    st.divider()
    st.markdown("##### " + t("🎯 主供货商指定文件（仕入先別_免送料判定 ③SKU明細）"))
    st.caption(t("上传「仕入先別_系列別_免送料判定_明細付」xlsx → 读 ③SKU明細 sheet，"
                 "每 JAN 的 仕入先/単価 = 主供货商/主供货商价格。写入=全量替换旧指定。"))
    _msf = st.file_uploader(t("免送料判定（.xlsx）"), type=["xlsx"], key="ms_up")
    if _msf is not None:
        try:
            _mxl = pd.ExcelFile(io.BytesIO(_msf.read()))
            _msheet = next((n for n in _mxl.sheet_names if "SKU明細" in n), None)
            _mraw = (pd.read_excel(_mxl, sheet_name=_msheet, header=None, dtype=str)
                     if _msheet else None)
        except Exception as e:  # noqa: BLE001
            st.error(t("解析失败") + f"\n\n{e}")
            _mraw = None
        if _mraw is None:
            st.error(t("找不到 ③SKU明細 sheet"))
        else:
            _mains = sc.extract_main_suppliers(_mraw)
            _mains = sc.apply_supplier_alias(_mains, _alias_map())
            if _mains.empty:
                st.error(t("③SKU明細 里没抽到有效行（表头需含 仕入先/JAN/単価）"))
            else:
                st.dataframe(_mains.head(50), hide_index=True, use_container_width=True)
                st.caption(t("预览前 50 行 · 共 {n} 行").format(n=len(_mains))
                           + f" · {t('供货商')} {_mains['supplier_name'].nunique()}")
                if st.button(t("✅ 写入主供货商（全量替换）"), key="ms_write"):
                    conn.execute("DELETE FROM sourcing.item_main_supplier")
                    conn.executemany(
                        "INSERT INTO sourcing.item_main_supplier "
                        "(jan, supplier_name, price, source) VALUES (?,?,?,?)",
                        [(str(_r["jan"]).strip(), str(_r["supplier_name"]).strip(),
                          sc_num(_r.get("price")), _msf.name)
                         for _, _r in _mains.iterrows()])
                    conn.commit()
                    st.success(t("✅ 主供货商已替换：{n} 条").format(n=len(_mains)))

    st.divider()
    st.markdown("##### " + t("🌱 从 NST PO 实绩导入报价（种子）"))
    st.caption(t("用 po_item_supplier_monthly 每个 供货商×JAN 的最新月加重平均单价 作为一条 source=po 报价。"))
    if st.button(t("从 PO 实绩导入/刷新"), key="sq_seed_po"):
        _po_seed = _read(
            "SELECT q.vendor_name AS supplier_name, q.jan, q.display_name AS item_name, "
            "q.avg_unit_price AS price, q.year_month "
            "FROM nst.po_item_supplier_monthly q "
            "WHERE q.jan IS NOT NULL AND q.avg_unit_price IS NOT NULL AND q.vendor_name IS NOT NULL")
        if _po_seed.empty:
            st.info(t("无 PO 实绩数据"))
        else:
            _po_seed = sc.apply_supplier_alias(_po_seed, _alias_map())
            _po_seed = _po_seed.sort_values("year_month").drop_duplicates(
                subset=["supplier_name", "jan"], keep="last")
            _ensure_suppliers(_po_seed["supplier_name"].tolist())
            _n = 0
            for _, _r in _po_seed.iterrows():
                _qd = str(_r["year_month"]) + "-01"
                conn.execute(
                    "INSERT INTO sourcing.supplier_quote "
                    "(supplier_name, jan, item_name, price, quote_date, source) "
                    "VALUES (?,?,?,?,?,?)",
                    (str(_r["supplier_name"]).strip(), str(_r["jan"]).strip(),
                     _r.get("item_name"), sc_num(_r.get("price")), _qd, "po"))
                _n += 1
            conn.commit()
            st.success(t("✅ 从 PO 实绩导入 {n} 条").format(n=_n))

    st.divider()
    # ── 有效期一括维护（spec: 报价应补充 valid_from/valid_to）──
    st.markdown("##### " + _dl("📆 报价有效期维护（按供应商）", "📆 見積有効期間メンテ（仕入先別）"))
    _vsup = st.selectbox(t("供货商"), [""] + _sup_opts, key="vld_sup")
    if _vsup:
        _vq = _read("SELECT id, jan, item_name, price, quote_date, valid_from, valid_to "
                    "FROM sourcing.supplier_quote WHERE supplier_name = ?", (_vsup,))
        _vq = sc.latest_quotes(_vq.assign(supplier_name=_vsup)) if not _vq.empty else _vq
        if _vq.empty:
            st.info(t("この条件のデータがありません"))
        else:
            _vq = _vq[["id", "jan", "item_name", "price", "quote_date",
                       "valid_from", "valid_to"]].sort_values("jan")
            _ved = st.data_editor(
                _vq, hide_index=True, use_container_width=True, height=360,
                key=f"vld_ed_{_vsup}",
                column_config={
                    "id": None,
                    "jan": st.column_config.TextColumn("JAN", disabled=True),
                    "item_name": st.column_config.TextColumn(t("品名（可选）"), disabled=True),
                    "price": st.column_config.NumberColumn(
                        t("仕入单价（税抜）"), format="%.2f", disabled=True),
                    "quote_date": st.column_config.DateColumn(
                        _dl("报价日", "見積日"), disabled=True),
                    "valid_from": st.column_config.DateColumn(_dl("有效期从", "有効期間自")),
                    "valid_to": st.column_config.DateColumn(_dl("有效期至", "有効期間至")),
                })
            if st.button(_dl("💾 保存有效期", "💾 有効期間を保存"), key="vld_save"):
                _nup = 0
                for _, _r in _ved.iterrows():
                    conn.execute(
                        "UPDATE sourcing.supplier_quote SET valid_from=?, valid_to=? "
                        "WHERE id=?",
                        (_date_or_none(_r.get("valid_from")),
                         _date_or_none(_r.get("valid_to")), int(_r["id"])))
                    _nup += 1
                conn.commit()
                st.success(t("✅ 已保存") + f"（{_nup}）")
                st.rerun()

# ============================================================
# Tab 7：🏢 供应商与品牌规则
# ============================================================
with tab_rule:
    st.markdown("##### " + t("🏢 供货商主档（起订金额 / 纳期 / 预付 / 启用）"))
    # 把报价里出现但主档没有的供货商补进来
    _seen = _read("SELECT DISTINCT supplier_name FROM sourcing.supplier_quote")
    if not _seen.empty:
        _ensure_suppliers(_seen["supplier_name"].tolist())
    _sup_df = _read(
        "SELECT supplier_name, official_name, min_order_amount, free_ship_threshold, "
        "default_lead_days, is_prepay, active, note "
        "FROM sourcing.supplier ORDER BY supplier_name")
    if _sup_df.empty:
        st.info(t("还没有供货商。先在「报价上传」导入或上传报价。"))
    else:
        _sup_df["is_prepay"] = _sup_df["is_prepay"].fillna(False).astype(bool)
        _sup_df["active"] = _sup_df["active"].fillna(True).astype(bool)
        _ed = st.data_editor(
            _sup_df, hide_index=True, use_container_width=True, key="sup_editor",
            column_config={
                "supplier_name": st.column_config.TextColumn(t("简称(改名自动同步报价/主供货商)")),
                "official_name": st.column_config.TextColumn(t("正式名(NST)")),
                "min_order_amount": st.column_config.NumberColumn(t("起订金额(¥)"), format="%.0f"),
                "free_ship_threshold": st.column_config.NumberColumn(t("免送料閾値(¥)"), format="%.0f"),
                "default_lead_days": st.column_config.NumberColumn(t("纳期(日)"), format="%d"),
                "is_prepay": st.column_config.CheckboxColumn(t("预付(现金支付)")),
                "active": st.column_config.CheckboxColumn(t("启用")),
                "note": st.column_config.TextColumn(t("备注")),
            })
        if st.button(t("💾 保存供货商主档"), key="save_sup"):
            # ① 简称改名 → 报价 / 主供货商 / 品牌规则 / 别名表 级联同步(旧名进别名表防回流)
            _renamed, _conflict = 0, []
            for _i, _r in _ed.iterrows():
                _new = str(_r["supplier_name"]).strip()
                _old = str(_sup_df.iloc[_i]["supplier_name"]).strip()
                if not _new or _new == _old:
                    continue
                _dup = _read("SELECT 1 FROM sourcing.supplier WHERE supplier_name=?", (_new,))
                if not _dup.empty:
                    _conflict.append(f"{_old}→{_new}")
                    _ed.at[_i, "supplier_name"] = _old  # 回退,后续按旧名 upsert
                    continue
                conn.execute("UPDATE sourcing.supplier SET supplier_name=? "
                             "WHERE supplier_name=?", (_new, _old))
                conn.execute("UPDATE sourcing.supplier_quote SET supplier_name=? "
                             "WHERE supplier_name=?", (_new, _old))
                conn.execute("UPDATE sourcing.item_main_supplier SET supplier_name=? "
                             "WHERE supplier_name=?", (_new, _old))
                conn.execute("UPDATE sourcing.supplier_brand_rule SET supplier_name=? "
                             "WHERE supplier_name=?", (_new, _old))
                conn.execute("UPDATE sourcing.supplier_alias SET canonical=? "
                             "WHERE canonical=?", (_new, _old))
                conn.execute("INSERT INTO sourcing.supplier_alias (alias, canonical) "
                             "VALUES (?,?) ON CONFLICT (alias) DO UPDATE SET "
                             "canonical=EXCLUDED.canonical", (_old, _new))
                _renamed += 1
            # ② 字段 upsert
            for _, _r in _ed.iterrows():
                conn.execute(
                    "INSERT INTO sourcing.supplier "
                    "(supplier_name, official_name, min_order_amount, free_ship_threshold, "
                    " default_lead_days, is_prepay, active, note, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, NOW()) "
                    "ON CONFLICT (supplier_name) DO UPDATE SET "
                    "official_name=EXCLUDED.official_name, "
                    "min_order_amount=EXCLUDED.min_order_amount, "
                    "free_ship_threshold=EXCLUDED.free_ship_threshold, "
                    "default_lead_days=EXCLUDED.default_lead_days, "
                    "is_prepay=EXCLUDED.is_prepay, active=EXCLUDED.active, "
                    "note=EXCLUDED.note, updated_at=NOW()",
                    (str(_r["supplier_name"]).strip(), _r.get("official_name"),
                     sc_num(_r.get("min_order_amount")),
                     sc_num(_r.get("free_ship_threshold")),
                     sc_int(_r.get("default_lead_days")), bool(_r.get("is_prepay")),
                     bool(_r.get("active")), _r.get("note")))
            conn.commit()
            st.success(t("✅ 已保存"))
            if _renamed:
                st.info(t("🔁 简称改名 {n} 件·报价/主供货商/别名已同步").format(n=_renamed))
            if _conflict:
                st.warning(t("⚠️ 改名目标已存在,已跳过: ") + "、".join(_conflict))

    st.divider()
    # ── 供应商×品牌规则（spec 第一期最小新增模型 · 仅展示维护·不参与分配）──
    st.markdown("##### " + _dl("🏷️ 供应商×品牌规则（起订/免运·第一期仅维护展示）",
                               "🏷️ 仕入先×ブランドルール（最低発注/送料無料·第1期は表示保守のみ）"))
    st.caption(_dl(
        "品牌规则存在且在有效期内时覆盖供应商默认规则,否则回退供应商默认。"
        "第一期不参与订单分配,仅供看板展示与第二期准备。",
        "ブランドルールが存在し有効期間内なら仕入先デフォルトを上書き、なければデフォルトへ回退。"
        "第1期は発注割当に関与せず、表示と第2期準備のみ。"))
    _mk_opts = _read("SELECT DISTINCT maker FROM nst.item_master_raw "
                     "WHERE maker IS NOT NULL ORDER BY maker")
    _mk_opts = (_mk_opts["maker"].dropna().astype(str).tolist()
                if not _mk_opts.empty else [])
    _bre = _read("SELECT supplier_name, maker, effective_from, min_order_amount, "
                 "min_order_qty, ship_fee, free_ship_threshold, effective_to, note "
                 "FROM sourcing.supplier_brand_rule "
                 "ORDER BY supplier_name, maker, effective_from")
    if _bre.empty:
        _bre = pd.DataFrame({
            "supplier_name": pd.Series(dtype="object"),
            "maker": pd.Series(dtype="object"),
            "effective_from": pd.Series(dtype="object"),
            "min_order_amount": pd.Series(dtype="float64"),
            "min_order_qty": pd.Series(dtype="float64"),
            "ship_fee": pd.Series(dtype="float64"),
            "free_ship_threshold": pd.Series(dtype="float64"),
            "effective_to": pd.Series(dtype="object"),
            "note": pd.Series(dtype="object"),
        })
    _bed = st.data_editor(
        _bre, num_rows="dynamic", hide_index=True, use_container_width=True,
        key="brand_rule_ed",
        column_config={
            "supplier_name": st.column_config.SelectboxColumn(
                t("供货商"), options=_sup_opts if _sup_opts else None),
            "maker": st.column_config.SelectboxColumn(
                t("品牌"), options=_mk_opts if _mk_opts else None),
            "effective_from": st.column_config.DateColumn(
                _dl("生效日", "適用開始日"), default=dt.date.today()),
            "min_order_amount": st.column_config.NumberColumn(t("起订金额(¥)"), format="%.0f"),
            "min_order_qty": st.column_config.NumberColumn(_dl("起订数量", "最低発注数"), format="%.0f"),
            "ship_fee": st.column_config.NumberColumn(_dl("运费(¥)", "送料(¥)"), format="%.0f"),
            "free_ship_threshold": st.column_config.NumberColumn(t("免送料閾値(¥)"), format="%.0f"),
            "effective_to": st.column_config.DateColumn(_dl("失效日(可空)", "適用終了日(空可)")),
            "note": st.column_config.TextColumn(t("备注")),
        })
    if st.button(_dl("💾 保存品牌规则（全量替换）", "💾 ブランドルール保存（全量置換）"),
                 key="brand_rule_save"):
        _ok_rows = []
        _skip_b = 0
        for _, _r in _bed.iterrows():
            _s = str(_r.get("supplier_name") or "").strip()
            _mk = str(_r.get("maker") or "").strip()
            _ef = _date_or_none(_r.get("effective_from")) or _TODAY.isoformat()
            if not _s or not _mk or _mk == "nan" or _s == "nan":
                _skip_b += 1
                continue
            _ok_rows.append((_s, _mk, _ef, sc_num(_r.get("min_order_amount")),
                             sc_num(_r.get("min_order_qty")), sc_num(_r.get("ship_fee")),
                             sc_num(_r.get("free_ship_threshold")),
                             _date_or_none(_r.get("effective_to")),
                             (str(_r.get("note")).strip() or None)
                             if _r.get("note") is not None and pd.notna(_r.get("note"))
                             else None))
        conn.execute("DELETE FROM sourcing.supplier_brand_rule")
        if _ok_rows:
            conn.executemany(
                "INSERT INTO sourcing.supplier_brand_rule "
                "(supplier_name, maker, effective_from, min_order_amount, min_order_qty, "
                " ship_fee, free_ship_threshold, effective_to, note, updated_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,'page34') "
                "ON CONFLICT (supplier_name, maker, effective_from) DO UPDATE SET "
                "min_order_amount=EXCLUDED.min_order_amount, "
                "min_order_qty=EXCLUDED.min_order_qty, ship_fee=EXCLUDED.ship_fee, "
                "free_ship_threshold=EXCLUDED.free_ship_threshold, "
                "effective_to=EXCLUDED.effective_to, note=EXCLUDED.note, "
                "updated_by='page34', updated_at=NOW()", _ok_rows)
        conn.commit()
        _msgb = t("✅ 已保存") + f"（{len(_ok_rows)}）"
        if _skip_b:
            _msgb += " · " + t("跳过 {m} 行（缺 供货商/JAN/单价）").format(m=_skip_b)
        st.success(_msgb)
        st.rerun()
