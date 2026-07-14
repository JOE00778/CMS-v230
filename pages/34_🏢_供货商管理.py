"""模块 #34 供货商管理 · 4 tab（Boss 2026-07-07 重整 · 2026-07-14 改名）.

  📋 仕入詳細     商品×采购价格台账（最新PO单价/主·次供货商(待上传)/最低报价/现状比）
  🔍 見直し必要    采购价 ÷ 建议零售价(nst.item_msrp) = 仕入折数 → 需重新谈价一览
  📤 見積書UP     报价上传（手动输入·xlsx/csv·仕入先管理リスト一括·PO 实绩种子）
  🏢 仕入先データ  供货商主档（起订金额/纳期/预付/启用）
  📊 仕入先予測    供货商×月推算订货金额（Σ 当月销量×仕入单价）+ SKU数 + 免送料閾値
  🏷️ ブランド分析  品牌(maker)×供货商:近12个月PO实绩 SKU数+平均仕入折数

データ: PG sourcing schema（本ページが idempotent 建表）+ nst.*（NST 直连）。
  sourcing.supplier        供货商主档（起订金额/纳期/预付/启用）
  sourcing.supplier_quote  报价（supplier×jan×price×moq×lot×lead×quote_date·append）
ロジック: shared/sourcing.py（純関数·tests/test_sourcing.py）。
旧「🏆 比价/采购决策」（权重打分）tab は 2026-07-07 廃止（sourcing.py の
Weights/recommend は tests 付きのため保留）。
"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t
from shared import sourcing as sc

st.set_page_config(page_title=t("供货商管理"), page_icon="🏢", layout="wide")
from shared.auth import require_password  # noqa: E402
require_password()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🏢 供货商管理"))
st.caption(t(
    "仕入詳細(采购价格台账) · 見直し必要(对比建议零售价) · "
    "見積書UP(报价上传) · 仕入先データ(供货商主档) · "
    "仕入先予測(月订货金额推算) · ブランド分析(品牌×供货商)。"
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
    return _p[["internal_id", "cur_supplier", "cur_rate"]]


def sc_int(v):
    _n = sc_num(v)
    return int(_n) if _n is not None else None


tab_data, tab_opt, tab_up, tab_sup, tab_ana, tab_brand = st.tabs(
    [t("📋 仕入詳細"), t("🔍 見直し必要"), t("📤 見積書UP"),
     t("🏢 仕入先データ"), t("📊 仕入先予測"), t("🏷️ ブランド分析")])

# ============================================================
# Tab：报价上传 / 种子
# ============================================================
with tab_up:
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
                    " lead_days, quote_date, source) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(_r["supplier_name"]).strip(), str(_r["jan"]).strip(),
                     _iname, sc_num(_r.get("price")), sc_num(_r.get("moq")),
                     sc_num(_r.get("order_lot")), sc_int(_r.get("lead_days")),
                     _hdate.isoformat(), "manual"))
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
        _po = _read(
            "SELECT q.vendor_name AS supplier_name, q.jan, q.display_name AS item_name, "
            "q.avg_unit_price AS price, q.year_month "
            "FROM nst.po_item_supplier_monthly q "
            "WHERE q.jan IS NOT NULL AND q.avg_unit_price IS NOT NULL AND q.vendor_name IS NOT NULL")
        if _po.empty:
            st.info(t("无 PO 实绩数据"))
        else:
            _po = sc.apply_supplier_alias(_po, _alias_map())
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
            # ① 简称改名 → 报价 / 主供货商 / 别名表 级联同步(旧名进别名表防回流)
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

# ============================================================
# Tab：仕入詳細（数据管理 · ABC产品 × 供货商价格 + 最新订货单价 + 最低价）
# ============================================================
with tab_data:
    st.caption(t("商品 × 采购价格台账：最新订货单价(NST PO) + 主/次供货商(待上传) + 最低价(报价) + 现状对比。"))
    _ranks = st.multiselect(t("等级"), ["Aランク", "Bランク", "Cランク", "NEW"],
                            default=["Aランク", "Bランク", "Cランク"], key="abc_ranks")
    if not _ranks:
        _ranks = ["Aランク", "Bランク", "Cランク"]
    _items = _read(
        "SELECT internal_id, jan, display_name, item_rank, last_purchase_cost "
        "FROM nst.item_master_raw WHERE jan IS NOT NULL AND item_rank IN ("
        + ",".join(["?"] * len(_ranks)) + ")", tuple(_ranks))
    if _items.empty:
        st.info(t("无该等级商品（NST item_master 未就绪？）"))
    else:
        _lq = _read("SELECT id, supplier_name, jan, price, quote_date FROM sourcing.supplier_quote")
        _wide = (sc.compare_wide(sc.latest_quotes(_lq)) if not _lq.empty
                 else pd.DataFrame(columns=["jan", "min_price", "cheapest_supplier"]))
        # 最新订货单价 = 该商品最近一次 PO 行的単価 rate=発注時原価（MAX(trandate)·
        # Boss 2026-07-14: 行総額 amount だと数量×単価で比価にならない）
        _pol = _read("SELECT item_internal_id, trandate, rate FROM nst.purchase_order_line "
                     "WHERE item_internal_id IS NOT NULL")
        if not _pol.empty:
            _pol["rate"] = pd.to_numeric(_pol["rate"], errors="coerce")
            _pol = (_pol.sort_values("trandate").drop_duplicates("item_internal_id", keep="last")
                    [["item_internal_id", "rate"]]
                    .rename(columns={"item_internal_id": "internal_id",
                                     "rate": "latest_po_price"}))
        else:
            _pol = pd.DataFrame(columns=["internal_id", "latest_po_price"])
        _pol["internal_id"] = _pol["internal_id"].astype(str)
        _items["internal_id"] = _items["internal_id"].astype(str)

        base = (_items.merge(_wide[[c for c in ("jan", "min_price") if c in _wide.columns]],
                             on="jan", how="left")
                if not _wide.empty else _items.assign(min_price=None))
        base = base.merge(_pol, on="internal_id", how="left")
        base["last_purchase_cost"] = pd.to_numeric(base["last_purchase_cost"], errors="coerce")
        base["min_price"] = pd.to_numeric(base.get("min_price"), errors="coerce")
        # (现状进货价 / 最低价 − 1)%：>0 = 现状比最低价贵
        base["ratio_vs_min"] = (base["last_purchase_cost"] / base["min_price"].where(
            base["min_price"] > 0) - 1) * 100
        # 主供货商 = 免送料判定文件の指定（sourcing.item_main_supplier · 見積書UP で更新）
        _ms = _read("SELECT jan, supplier_name AS main_supplier, price AS main_price "
                    "FROM sourcing.item_main_supplier")
        base = (base.merge(_ms.drop_duplicates("jan"), on="jan", how="left") if not _ms.empty
                else base.assign(main_supplier=None, main_price=None))
        base["main_price"] = pd.to_numeric(base.get("main_price"), errors="coerce")
        # 次供货商 2 列：留白占位（数据后期上传 · Boss 2026-07-07）
        for _c in ("sub_supplier", "sub_price"):
            base[_c] = None

        _c1, _c2 = st.columns([1, 2])
        _only_q = _c1.checkbox(t("只看有报价的"), value=False, key="abc_onlyq")
        _kw = _c2.text_input(t("🔍 JAN / 商品名 搜索"), key="abc_kw")
        if _only_q:
            base = base[base["min_price"].notna()]
        if _kw.strip():
            _k = _kw.strip()
            base = base[base["jan"].astype(str).str.contains(_k, na=False)
                        | base["display_name"].astype(str).str.contains(_k, case=False, na=False)]
            if base.empty:
                # 検索 0 件の原因を顕在化：報価はあるのに行が出ない=ランク外 or 主档外
                _k2 = f"%{_k}%"
                _im_hit = _read(
                    "SELECT item_rank FROM nst.item_master_raw "
                    "WHERE jan LIKE ? OR display_name LIKE ?", (_k2, _k2))
                _sq_hit = _read(
                    "SELECT COUNT(*) AS n FROM sourcing.supplier_quote "
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

        k1, k2, k3 = st.columns(3)
        k1.metric(t("商品数"), f"{len(base):,}")
        k2.metric(t("有报价"), f"{int(base['min_price'].notna().sum()):,}")
        k3.metric(t("现价>最低价"), f"{int((base['ratio_vs_min'] > 0).sum()):,}")

        _order = ["item_rank", "jan", "display_name", "latest_po_price",
                  "main_supplier", "main_price", "sub_supplier", "sub_price",
                  "min_price", "ratio_vs_min"]
        view = base[_order].sort_values(["item_rank", "ratio_vs_min"],
                                        ascending=[True, False], na_position="last")
        _ren = {"item_rank": t("RANK"), "jan": t("JAN"), "display_name": t("商品名"),
                "latest_po_price": t("最新订货单价"),
                "main_supplier": t("主供货商"), "main_price": t("主供货商价格"),
                "sub_supplier": t("次供货商"), "sub_price": t("次供货商价格"),
                "min_price": t("最低价格"), "ratio_vs_min": t("现状比最低价")}
        disp = view.rename(columns=_ren)
        st.dataframe(
            disp, hide_index=True, use_container_width=True, height=600,
            column_config={
                t("最新订货单价"): st.column_config.NumberColumn(format="¥%.0f"),
                t("主供货商价格"): st.column_config.NumberColumn(format="¥%.0f"),
                t("次供货商价格"): st.column_config.NumberColumn(format="¥%.0f"),
                t("最低价格"): st.column_config.NumberColumn(format="¥%.0f"),
                t("现状比最低价"): st.column_config.NumberColumn(format="%.0f%%"),
            })
        st.download_button(t("📥 仕入詳細 CSV"),
                           disp.to_csv(index=False).encode("utf-8-sig"),
                           file_name="sourcing_data.csv", mime="text/csv", key="abc_csv")
        st.caption(t("最新订货单价=NST 最近一次 PO 行单价(発注時原価) · 主供货商=免送料判定文件指定(見積書UP更新) · "
                     "最低价格=各供货商最新报价最小 · 现状比最低价=(NST 前回购入价 ÷ 最低价 − 1)(>0 现状偏贵) · "
                     "次供货商待上传"))

# ============================================================
# Tab：見直し必要（优化建议 · 采购价 vs 建议零售价 = 几折采购）
# ============================================================
with tab_opt:
    st.caption(t("采购价(NST 前回购入价·税抜) ÷ 建议零售价(nst.item_msrp·官方源·税抜换算) = 仕入折数。"
                 "折数越高利润越薄 → 需重新谈价。"))
    _ranks_o = st.multiselect(t("等级"), ["Aランク", "Bランク", "Cランク", "NEW"],
                              default=["Aランク", "Bランク", "NEW"], key="opt_ranks")
    if not _ranks_o:
        _ranks_o = ["Aランク", "Bランク", "NEW"]
    _mi = _read(
        "SELECT im.internal_id, im.jan, im.display_name, im.item_rank, "
        "im.last_purchase_cost, m.msrp_jpy, m.msrp_jpy_taxin, m.is_open_price "
        "FROM nst.item_master_raw im "
        "LEFT JOIN nst.item_msrp m ON m.jan = im.jan "
        "WHERE im.jan IS NOT NULL AND im.item_rank IN ("
        + ",".join(["?"] * len(_ranks_o)) + ")", tuple(_ranks_o))
    if _mi.empty:
        st.info(t("无数据（nst.item_msrp 未就绪？本地环境无 MSRP 表属正常）"))
    else:
        _mi["last_purchase_cost"] = pd.to_numeric(_mi["last_purchase_cost"], errors="coerce")
        _mi["msrp_jpy"] = pd.to_numeric(_mi["msrp_jpy"], errors="coerce")
        _mi["msrp_jpy_taxin"] = pd.to_numeric(_mi["msrp_jpy_taxin"], errors="coerce")
        # 希望小売価格を税抜に統一（Boss 2026-07-14 · 税率10%）：
        # msrp_jpy_taxin=税込換算値（税抜→×1.1済/税込→原値）÷1.1 → 税抜。
        # taxin が無い行（tax=unknown）は原値を税込とみなして ÷1.1。
        _mi["msrp_taxex"] = _mi["msrp_jpy_taxin"].fillna(_mi["msrp_jpy"]) / 1.1
        # 仕入折数 = 采购价(税抜) ÷ 建议零售价(税抜) × 10（7.0 = 7折 = 零售价的 70%）
        _mi["wari"] = _mi["last_purchase_cost"] / _mi["msrp_taxex"].where(_mi["msrp_taxex"] > 0) * 10
        # 现供货商 = NST 直近 PO 行の仕入先（共通 helper）
        _mi["internal_id"] = _mi["internal_id"].astype(str)
        _mi = _mi.merge(_latest_po_vendor()[["internal_id", "cur_supplier"]],
                        on="internal_id", how="left")
        _has = _mi[_mi["wari"].notna()].copy()
        _no = _mi[_mi["wari"].isna()]

        _th = st.slider(t("見直し阈值（仕入折数 ≥ X 折 = 需重新谈价）"),
                        0.0, 10.0, 7.0, 0.5, key="opt_th")
        _has["need"] = _has["wari"] >= _th

        k1, k2, k3 = st.columns(3)
        k1.metric(t("有参考价"), f"{len(_has):,}")
        k2.metric(t("見直し必要"), f"{int(_has['need'].sum()):,}")
        k3.metric(t("无参考価"), f"{len(_no):,}")

        _kw_o = st.text_input(t("🔍 JAN / 商品名 搜索"), key="opt_kw")
        _v = _has
        if _kw_o.strip():
            _k = _kw_o.strip()
            _v = _v[_v["jan"].astype(str).str.contains(_k, na=False)
                    | _v["display_name"].astype(str).str.contains(_k, case=False, na=False)]

        _v = _v.sort_values("wari", ascending=False, na_position="last").copy()
        _v["flag"] = _v["need"].map(lambda b: "⚠️" if b else "")
        _show_o = _v[["flag", "item_rank", "jan", "display_name",
                      "last_purchase_cost", "msrp_taxex", "wari",
                      "cur_supplier"]].rename(
            columns={"flag": t("見直し"), "item_rank": t("RANK"), "jan": t("JAN"),
                     "display_name": t("商品名"), "last_purchase_cost": t("采购价"),
                     "msrp_taxex": t("建议零售价(税抜)"), "wari": t("仕入折数"),
                     "cur_supplier": t("现供货商")})
        st.dataframe(
            _show_o, hide_index=True, use_container_width=True, height=560,
            column_config={
                t("采购价"): st.column_config.NumberColumn(format="¥%.0f"),
                t("建议零售价(税抜)"): st.column_config.NumberColumn(format="¥%.0f"),
                t("仕入折数"): st.column_config.NumberColumn(format="%.1f折"),
            })
        st.download_button(t("📥 見直し必要 CSV"),
                           _show_o.to_csv(index=False).encode("utf-8-sig"),
                           file_name="msrp_review.csv", mime="text/csv", key="opt_csv")
        st.caption(t("建议零售价=官方源 MSRP 税抜换算(税込÷1.1·税率10%·原值已税抜则不变) · "
                     "采购价=NST 前回购入价(税抜) · 现供货商=NST 最近一次 PO 的仕入先 · "
                     "仕入折数=采购价÷零售价×10 · 无参考価=オープン価格/未调查(C 档暂缓)"))
        if not _no.empty:
            with st.expander(t("无参考価商品一览") + f"（{len(_no):,}）"):
                _no_show = _no[["item_rank", "jan", "display_name", "last_purchase_cost"]].rename(
                    columns={"item_rank": t("RANK"), "jan": t("JAN"),
                             "display_name": t("商品名"), "last_purchase_cost": t("采购价")})
                st.dataframe(_no_show, hide_index=True, use_container_width=True, height=300,
                             column_config={
                                 t("采购价"): st.column_config.NumberColumn(format="¥%.0f")})

# ============================================================
# Tab：仕入先予測（供货商 × 月推算订货金额 = Σ 当月销量 × 仕入单价）
# ============================================================
with tab_ana:
    st.caption(t("按供货商推算月订货金额 = Σ(SKU 当月销量 × 仕入单价)。"
                 "供货商=主供货商指定优先、无指定用 NST 最近 PO 仕入先；"
                 "单价=主供货商价格优先、无则 NST 前回购入价(税抜)。"))
    _ai = _read("SELECT internal_id, jan, display_name, last_purchase_cost "
                "FROM nst.item_master_raw WHERE jan IS NOT NULL")
    if _ai.empty:
        st.info(t("无数据（NST item_master 未就绪？）"))
    else:
        _ai["internal_id"] = _ai["internal_id"].astype(str)
        _ai["last_purchase_cost"] = pd.to_numeric(_ai["last_purchase_cost"],
                                                  errors="coerce")
        _ms2 = _read("SELECT jan, supplier_name AS main_supplier, "
                     "price AS main_price FROM sourcing.item_main_supplier")
        if not _ms2.empty:
            _ai = _ai.merge(_ms2.drop_duplicates("jan"), on="jan", how="left")
        else:
            _ai["main_supplier"] = None
            _ai["main_price"] = None
        _ai = _ai.merge(_latest_po_vendor()[["internal_id", "cur_supplier"]],
                        on="internal_id", how="left")
        _ai["supplier"] = _ai["main_supplier"].fillna(_ai["cur_supplier"])
        _ai["main_price"] = pd.to_numeric(_ai["main_price"], errors="coerce")
        _ai["unit_price"] = _ai["main_price"].fillna(_ai["last_purchase_cost"])
        _ai = _ai[_ai["supplier"].notna()
                  & (_ai["supplier"].astype(str).str.strip() != "")]

        # 近 6 个月（含当月）销量 → 月推算金额
        _yms = []
        _cur = dt.date.today().replace(day=1)
        for _ in range(6):
            _yms.append(_cur.strftime("%Y-%m"))
            _cur = (_cur - dt.timedelta(days=1)).replace(day=1)
        _yms = sorted(_yms)
        _sd = _read(
            "SELECT item_internal_id, SUBSTR(CAST(sale_date AS TEXT),1,7) AS ym, "
            "SUM(qty_sold) AS qty FROM nst.sales_daily WHERE sale_date >= ? "
            "GROUP BY item_internal_id, SUBSTR(CAST(sale_date AS TEXT),1,7)",
            (_yms[0] + "-01",))
        if _sd.empty:
            st.info(t("无销量数据（nst.sales_daily 未就绪？）"))
        else:
            _sd["item_internal_id"] = _sd["item_internal_id"].astype(str)
            _sd["qty"] = pd.to_numeric(_sd["qty"], errors="coerce")
            _j = _sd.merge(
                _ai[["internal_id", "supplier", "unit_price", "jan", "display_name"]],
                left_on="item_internal_id", right_on="internal_id", how="inner")
            _j["amt"] = _j["qty"] * _j["unit_price"]
            _pv = (_j.pivot_table(index="supplier", columns="ym", values="amt",
                                  aggfunc="sum")
                   .reindex(columns=_yms).fillna(0).round(0))
            _skun = _ai.groupby("supplier")["jan"].nunique()

            k1, k2, k3 = st.columns(3)
            k1.metric(t("供货商数"), f"{len(_pv):,}")
            k2.metric(t("直近月推算合計"), f"¥{_pv[_yms[-1]].sum():,.0f}")
            k3.metric(t("取扱SKU計"), f"{int(_skun.sum()):,}")

            _tab_a = _pv.copy()
            _tab_a.insert(0, t("SKU数"), _skun)
            _tab_a = (_tab_a.sort_values(_yms[-1], ascending=False)
                      .reset_index().rename(columns={"supplier": t("供货商")}))
            # 金額は桁区切り表示（Boss 2026-07-14 · localized=千位分隔）
            st.dataframe(
                _tab_a, hide_index=True, use_container_width=True, height=560,
                column_config={c: st.column_config.NumberColumn(format="localized")
                               for c in _yms})
            st.download_button(t("📥 仕入先予測 CSV"),
                               _tab_a.to_csv(index=False).encode("utf-8-sig"),
                               file_name="supplier_monthly.csv",
                               mime="text/csv", key="ana_csv")
            st.caption(t("金额=当月销量×仕入单价的推算值(非 PO 实绩) · "
                         "SKU数=分配到该供货商的取扱 JAN 数"))

            _pick = st.selectbox(t("供货商 SKU 构成（选择查看明细）"),
                                 [""] + _tab_a[t("供货商")].tolist(),
                                 key="ana_pick")
            if _pick:
                _d = _j[_j["supplier"] == _pick]
                _dl = (_d[_d["ym"] == _yms[-1]]
                       .groupby(["jan", "display_name"], as_index=False)
                       .agg(qty_l=("qty", "sum"), amt_l=("amt", "sum")))
                _d6 = (_d.groupby(["jan", "display_name"], as_index=False)
                       .agg(amt6=("amt", "sum")))
                _dd = (_d6.merge(_dl, on=["jan", "display_name"], how="left")
                       .sort_values("amt6", ascending=False))
                _dd = _dd.rename(columns={
                    "jan": t("JAN"), "display_name": t("商品名"),
                    "qty_l": t("直近月販数"), "amt_l": t("直近月金額"),
                    "amt6": t("近6个月金額")})
                st.dataframe(
                    _dd[[t("JAN"), t("商品名"), t("直近月販数"),
                         t("直近月金額"), t("近6个月金額")]],
                    hide_index=True, use_container_width=True, height=400,
                    column_config={
                        t("直近月金額"): st.column_config.NumberColumn(format="localized"),
                        t("近6个月金額"): st.column_config.NumberColumn(format="localized"),
                        t("直近月販数"): st.column_config.NumberColumn(format="%.0f"),
                    })

# ============================================================
# Tab：ブランド分析（品牌 × 供货商 · 近12个月 PO 实绩 + 平均仕入折数）
# ============================================================
with tab_brand:
    st.caption(t("品牌(NST maker) × 供货商：近12个月 PO 实绩里该品牌从哪些供货商采购过、"
                 "各家 SKU 数与平均仕入折数(PO 单价 ÷ MSRP 税抜 × 10)。"))
    _bi = _read("SELECT internal_id, jan, maker FROM nst.item_master_raw "
                "WHERE jan IS NOT NULL AND maker IS NOT NULL")
    _pl = _read(
        "SELECT item_internal_id, trandate, vendor_name AS supplier_name, "
        "rate, quantity, amount "
        "FROM nst.purchase_order_line "
        "WHERE item_internal_id IS NOT NULL AND vendor_name IS NOT NULL "
        "AND rate IS NOT NULL AND trandate >= ?",
        ((dt.date.today() - dt.timedelta(days=365)).isoformat(),))
    if _bi.empty or _pl.empty:
        st.info(t("无数据（NST item_master / 近12个月 PO 未就绪？）"))
    else:
        _bi["internal_id"] = _bi["internal_id"].astype(str)
        _pl["item_internal_id"] = _pl["item_internal_id"].astype(str)
        for _c in ("rate", "quantity", "amount"):
            _pl[_c] = pd.to_numeric(_pl[_c], errors="coerce")
        _pl = sc.apply_supplier_alias(_pl, _alias_map())
        # 同一 商品×供货商 取最新一次 PO 的単価
        _pl = (_pl.sort_values("trandate")
               .drop_duplicates(["item_internal_id", "supplier_name"], keep="last"))
        _b = _pl.merge(_bi, left_on="item_internal_id",
                       right_on="internal_id", how="inner")
        _mp = _read("SELECT jan, msrp_jpy, msrp_jpy_taxin FROM nst.item_msrp")
        if not _mp.empty:
            _mp["msrp_jpy"] = pd.to_numeric(_mp["msrp_jpy"], errors="coerce")
            _mp["msrp_jpy_taxin"] = pd.to_numeric(_mp["msrp_jpy_taxin"],
                                                  errors="coerce")
            _mp["msrp_taxex"] = _mp["msrp_jpy_taxin"].fillna(_mp["msrp_jpy"]) / 1.1
            _b = _b.merge(_mp[["jan", "msrp_taxex"]].drop_duplicates("jan"),
                          on="jan", how="left")
        else:
            _b["msrp_taxex"] = None
        _b["msrp_taxex"] = pd.to_numeric(_b["msrp_taxex"], errors="coerce")
        _b["wari"] = _b["rate"] / _b["msrp_taxex"].where(_b["msrp_taxex"] > 0) * 10

        _g = (_b.groupby(["maker", "supplier_name"], as_index=False)
              .agg(sku_n=("item_internal_id", "nunique"),
                   msrp_n=("wari", "count"),
                   wari_mean=("wari", "mean")))
        # 直近仕入 = 当該 品牌×供货商 の最新 PO 行（金額/数量/年月）
        _last = (_b.sort_values("trandate")
                 .drop_duplicates(["maker", "supplier_name"], keep="last")
                 .assign(last_ym=lambda d: d["trandate"].astype(str).str[:7]))
        _g = _g.merge(
            _last[["maker", "supplier_name", "amount", "quantity", "last_ym"]],
            on=["maker", "supplier_name"], how="left")
        _g = _g.sort_values(["maker", "sku_n"], ascending=[True, False])

        k1, k2, k3 = st.columns(3)
        k1.metric(t("品牌数"), f"{_g['maker'].nunique():,}")
        k2.metric(t("供货商数"), f"{_g['supplier_name'].nunique():,}")
        k3.metric(t("品牌×供货商 组合"), f"{len(_g):,}")

        _kw_b = st.text_input(t("🔍 品牌 / 供货商 搜索"), key="brand_kw")
        _gv = _g
        if _kw_b.strip():
            _kb = _kw_b.strip()
            _gv = _gv[_gv["maker"].astype(str).str.contains(_kb, case=False, na=False)
                      | _gv["supplier_name"].astype(str)
                      .str.contains(_kb, case=False, na=False)]
        _show_b = _gv.rename(columns={
            "maker": t("品牌"), "supplier_name": t("供货商"),
            "sku_n": t("SKU数"), "msrp_n": t("有MSRP的SKU数"),
            "wari_mean": t("平均仕入折数"), "amount": t("直近仕入金額"),
            "quantity": t("直近ロット"), "last_ym": t("直近仕入月")})
        st.dataframe(
            _show_b, hide_index=True, use_container_width=True, height=560,
            column_config={
                t("平均仕入折数"): st.column_config.NumberColumn(format="%.1f折"),
                t("直近仕入金額"): st.column_config.NumberColumn(format="localized"),
                t("直近ロット"): st.column_config.NumberColumn(format="%.0f"),
            })
        st.download_button(t("📥 ブランド分析 CSV"),
                           _show_b.to_csv(index=False).encode("utf-8-sig"),
                           file_name="brand_supplier.csv",
                           mime="text/csv", key="brand_csv")
        st.caption(t("SKU数=近12个月从该供货商采购过的该品牌 SKU 数 · "
                     "平均仕入折数=各 SKU 最新 PO 单价÷MSRP 税抜×10 的平均"
                     "(仅有 MSRP 的 SKU 参与) · "
                     "直近仕入金額/ロット=该组合最近一次 PO 行的金额/数量,"
                     "直近仕入月=其年月 · 供货商名已按别名表归一"))
