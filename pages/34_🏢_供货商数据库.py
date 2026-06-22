"""模块 #34 供货商数据库 · 供货商比价 / 采购决策.

报价(supplier_quote·append 留历史)を貯め、JAN ごとに最新报价を綜合加权(价格/纳期/
预付/起订量)して「どの仕入先から買うのが最合理か」を判定する。新报价は上传で自動累積。

データ: PG sourcing schema（本ページが idempotent 建表）。
  sourcing.supplier        供货商主档（起订金额/纳期/预付/启用）
  sourcing.supplier_quote  报价（supplier×jan×price×moq×lot×lead×quote_date·append）
ロジック: shared/sourcing.py（純関数·tests/test_sourcing.py）。Boss 2026-06-22。
"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t
from shared import sourcing as sc

st.set_page_config(page_title=t("供货商数据库"), page_icon="🏢", layout="wide")
from shared.auth import require_password  # noqa: E402
require_password()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🏢 供货商数据库（供货商比价 / 采购决策）"))
st.caption(t(
    "报价累积(留历史) → 每个 JAN 取最新报价 → 价格/纳期/预付/起订量 综合加权 → "
    "判断从哪个供货商采购最合理。新报价上传即自动增加/更新。"
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


tab_dec, tab_list, tab_up, tab_sup = st.tabs(
    [t("🏆 比价 / 采购决策"), t("📋 ABC 比价列表"), t("📤 报价上传 / 种子"), t("🏢 供货商主档")])

# ============================================================
# Tab：报价上传 / 种子
# ============================================================
with tab_up:
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
    st.markdown("##### " + t("🌱 从 NST PO 实绩导入报价（种子）"))
    st.caption(t("用 po_item_supplier_monthly 每个 供货商×JAN 的最新月加重平均单价 作为一条 source=po 报价。"))
    if st.button(t("从 PO 实绩导入/刷新"), key="sq_seed_po"):
        _po = _read(
            "SELECT q.vendor_name AS supplier_name, q.jan, q.display_name AS item_name, "
            "q.avg_unit_price AS price, q.year_month "
            "FROM nst.po_item_supplier_monthly q "
            "WHERE q.jan IS NOT NULL AND q.avg_unit_price IS NOT NULL AND q.vendor_name IS NOT NULL")
        if _po.empty:
            st.info(t("无 PO 实绩数据"))
        else:
            _po = _po.sort_values("year_month").drop_duplicates(
                subset=["supplier_name", "jan"], keep="last")
            _ensure_suppliers(_po["supplier_name"].tolist())
            _n = 0
            for _, _r in _po.iterrows():
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

# ============================================================
# Tab：供货商主档
# ============================================================
with tab_sup:
    st.markdown("##### " + t("🏢 供货商主档（起订金额 / 纳期 / 预付 / 启用）"))
    # 把报价里出现但主档没有的供货商补进来
    _seen = _read("SELECT DISTINCT supplier_name FROM sourcing.supplier_quote")
    if not _seen.empty:
        _ensure_suppliers(_seen["supplier_name"].tolist())
    _sup_df = _read(
        "SELECT supplier_name, min_order_amount, default_lead_days, is_prepay, active, note "
        "FROM sourcing.supplier ORDER BY supplier_name")
    if _sup_df.empty:
        st.info(t("还没有供货商。先在「报价上传」导入或上传报价。"))
    else:
        _sup_df["is_prepay"] = _sup_df["is_prepay"].fillna(False).astype(bool)
        _sup_df["active"] = _sup_df["active"].fillna(True).astype(bool)
        _ed = st.data_editor(
            _sup_df, hide_index=True, use_container_width=True, key="sup_editor",
            column_config={
                "supplier_name": st.column_config.TextColumn(t("供货商"), disabled=True),
                "min_order_amount": st.column_config.NumberColumn(t("起订金额(¥)"), format="%.0f"),
                "default_lead_days": st.column_config.NumberColumn(t("纳期(日)"), format="%d"),
                "is_prepay": st.column_config.CheckboxColumn(t("预付(现金支付)")),
                "active": st.column_config.CheckboxColumn(t("启用")),
                "note": st.column_config.TextColumn(t("备注")),
            })
        if st.button(t("💾 保存供货商主档"), key="save_sup"):
            for _, _r in _ed.iterrows():
                conn.execute(
                    "INSERT INTO sourcing.supplier "
                    "(supplier_name, min_order_amount, default_lead_days, is_prepay, active, note, updated_at) "
                    "VALUES (?,?,?,?,?,?, NOW()) "
                    "ON CONFLICT (supplier_name) DO UPDATE SET "
                    "min_order_amount=EXCLUDED.min_order_amount, "
                    "default_lead_days=EXCLUDED.default_lead_days, "
                    "is_prepay=EXCLUDED.is_prepay, active=EXCLUDED.active, "
                    "note=EXCLUDED.note, updated_at=NOW()",
                    (str(_r["supplier_name"]).strip(), sc_num(_r.get("min_order_amount")),
                     sc_int(_r.get("default_lead_days")), bool(_r.get("is_prepay")),
                     bool(_r.get("active")), _r.get("note")))
            conn.commit()
            st.success(t("✅ 已保存"))

# ============================================================
# Tab：比价 / 采购决策
# ============================================================
with tab_dec:
    _w1, _w2, _w3, _w4 = st.columns(4)
    _wp = _w1.slider(t("价格权重"), 0.0, 1.0, 0.70, 0.05, key="w_price")
    _wl = _w2.slider(t("纳期权重"), 0.0, 1.0, 0.10, 0.05, key="w_lead")
    _wpp = _w3.slider(t("预付权重(预付=扣分)"), 0.0, 1.0, 0.10, 0.05, key="w_prepay")
    _wm = _w4.slider(t("起订量权重"), 0.0, 1.0, 0.10, 0.05, key="w_moq")
    _weights = sc.Weights(price=_wp, lead=_wl, prepay=_wpp, moq=_wm)

    _q = _read(
        "SELECT q.id, q.supplier_name, q.jan, q.item_name, q.price, q.moq, q.order_lot, "
        "q.lead_days, q.quote_date, q.source, "
        "COALESCE(s.is_prepay, FALSE) AS is_prepay, s.default_lead_days, "
        "COALESCE(s.active, TRUE) AS active "
        "FROM sourcing.supplier_quote q "
        "LEFT JOIN sourcing.supplier s ON s.supplier_name = q.supplier_name")
    if _q.empty:
        st.info(t("报价库为空。先到「📤 报价上传 / 种子」上传报价、或从 PO 实绩导入。"))
    else:
        _q = _q[_q["active"] != False]  # noqa: E712  停用供货商不参与
        for _c in ("price", "moq", "order_lot"):
            _q[_c] = pd.to_numeric(_q[_c], errors="coerce")
        _q["lead_days"] = pd.to_numeric(_q["lead_days"], errors="coerce").fillna(
            pd.to_numeric(_q["default_lead_days"], errors="coerce"))
        _latest = sc.latest_quotes(_q)
        _scored = sc.recommend(_latest, _weights)

        k1, k2, k3 = st.columns(3)
        k1.metric(t("覆盖 SKU"), f"{_scored['jan'].nunique():,}")
        k2.metric(t("供货商数"), f"{_scored['supplier_name'].nunique():,}")
        k3.metric(t("报价条数(最新)"), f"{len(_scored):,}")

        _jan_kw = st.text_input(t("🔍 按 JAN / 商品名 搜索（留空=全部）"), key="dec_kw")
        view = _scored
        if _jan_kw.strip():
            _kw = _jan_kw.strip()
            view = view[view["jan"].astype(str).str.contains(_kw, na=False)
                        | view["item_name"].astype(str).str.contains(_kw, case=False, na=False)]

        st.markdown("##### " + t("🏆 采购推荐（每 JAN 综合得分最低者=🏆）"))
        _disp = view.sort_values(["jan", "score"]).copy()
        _disp["rec"] = _disp["is_recommended"].map(lambda b: "🏆" if b else "")
        _show = _disp[["rec", "jan", "item_name", "supplier_name", "price", "moq",
                       "order_lot", "lead_days", "is_prepay", "score", "quote_date", "source"]].rename(
            columns={"rec": t("推荐"), "jan": t("JAN"), "item_name": t("商品名"),
                     "supplier_name": t("供货商"), "price": t("采购价"), "moq": t("起订量"),
                     "order_lot": t("订货批量"), "lead_days": t("纳期(日)"),
                     "is_prepay": t("预付"), "score": t("综合得分"),
                     "quote_date": t("报价日"), "source": t("来源")})
        st.dataframe(_show, hide_index=True, use_container_width=True, height=560,
                     column_config={
                         t("采购价"): st.column_config.NumberColumn(format="¥%.2f"),
                         t("综合得分"): st.column_config.NumberColumn(format="%.3f"),
                     })
        st.caption(t("得分越低越推荐 · 价格/纳期/起订量在同 JAN 内归一化 · 预付供货商按权重扣分"))

# ============================================================
# Tab：ABC 比价列表（ABC产品 × 各供货商报价 + 现在进货 + 最低价）
# ============================================================
with tab_list:
    st.caption(t("ABC 等级产品 × 各供货商最新报价(宽表) + 现在进货价/最近订货数 + 最低价/最安供货商。"))
    _ranks = st.multiselect(t("等级"), ["Aランク", "Bランク", "Cランク"],
                            default=["Aランク", "Bランク", "Cランク"], key="abc_ranks")
    if not _ranks:
        _ranks = ["Aランク", "Bランク", "Cランク"]
    _items = _read(
        "SELECT jan, display_name, maker, item_rank, last_purchase_cost "
        "FROM nst.item_master_raw WHERE jan IS NOT NULL AND item_rank IN ("
        + ",".join(["?"] * len(_ranks)) + ")", tuple(_ranks))
    if _items.empty:
        st.info(t("无 ABC 等级商品（NST item_master 未就绪？）"))
    else:
        _lq = _read("SELECT id, supplier_name, jan, price, quote_date FROM sourcing.supplier_quote")
        _wide = (sc.compare_wide(sc.latest_quotes(_lq)) if not _lq.empty
                 else pd.DataFrame(columns=["jan", "min_price", "cheapest_supplier"]))
        _poq = _read("SELECT jan, year_month, qty_ordered FROM nst.po_item_supplier_monthly "
                     "WHERE jan IS NOT NULL")
        if not _poq.empty:
            _poq["qty_ordered"] = pd.to_numeric(_poq["qty_ordered"], errors="coerce")
            _pm = _poq.groupby(["jan", "year_month"], as_index=False)["qty_ordered"].sum()
            _pm = (_pm.sort_values("year_month").drop_duplicates("jan", keep="last")
                   [["jan", "qty_ordered"]].rename(columns={"qty_ordered": "recent_qty"}))
        else:
            _pm = pd.DataFrame(columns=["jan", "recent_qty"])

        base = _items.merge(_wide, on="jan", how="left").merge(_pm, on="jan", how="left")
        base["last_purchase_cost"] = pd.to_numeric(base["last_purchase_cost"], errors="coerce")
        base["min_price"] = pd.to_numeric(base.get("min_price"), errors="coerce")
        base["save_vs_min"] = base["last_purchase_cost"] - base["min_price"]

        _c1, _c2 = st.columns([1, 2])
        _only_q = _c1.checkbox(t("只看有报价的"), value=True, key="abc_onlyq")
        _kw = _c2.text_input(t("🔍 JAN / 商品名 搜索"), key="abc_kw")
        if _only_q:
            base = base[base["min_price"].notna()]
        if _kw.strip():
            _k = _kw.strip()
            base = base[base["jan"].astype(str).str.contains(_k, na=False)
                        | base["display_name"].astype(str).str.contains(_k, case=False, na=False)]

        k1, k2, k3 = st.columns(3)
        k1.metric(t("ABC 商品"), f"{len(base):,}")
        k2.metric(t("有报价"), f"{int(base['min_price'].notna().sum()):,}")
        k3.metric(t("现价>最低价(可省)"), f"{int((base['save_vs_min'] > 0).sum()):,}")

        _supcols = [c for c in _wide.columns if c not in ("jan", "min_price", "cheapest_supplier")]
        _core = ["item_rank", "jan", "display_name", "maker", "last_purchase_cost",
                 "recent_qty", "min_price", "cheapest_supplier", "save_vs_min"]
        _order = [c for c in _core if c in base.columns] + [c for c in _supcols if c in base.columns]
        view = base[_order].sort_values(["item_rank", "save_vs_min"],
                                        ascending=[True, False], na_position="last")
        _ren = {"item_rank": t("等级"), "jan": t("JAN"), "display_name": t("商品名"),
                "maker": t("厂商"), "last_purchase_cost": t("现在进货价"),
                "recent_qty": t("最近订货数"), "min_price": t("最低价"),
                "cheapest_supplier": t("最安供货商"), "save_vs_min": t("现价-最低(可省)")}
        disp = view.rename(columns=_ren)
        st.dataframe(
            disp, hide_index=True, use_container_width=True, height=600,
            column_config={
                t("现在进货价"): st.column_config.NumberColumn(format="¥%.0f"),
                t("最低价"): st.column_config.NumberColumn(format="¥%.0f"),
                t("现价-最低(可省)"): st.column_config.NumberColumn(format="¥%.0f"),
            })
        st.download_button(t("📥 ABC比价列表 CSV"),
                           disp.to_csv(index=False).encode("utf-8-sig"),
                           file_name="abc_supplier_compare.csv", mime="text/csv", key="abc_csv")
        st.caption(t("现在进货价=NST 前回购入价格 · 最低价=各供货商最新报价最小 · "
                     "可省=现在进货价−最低价(>0 值得换最安) · 后面列=各供货商报价"))
