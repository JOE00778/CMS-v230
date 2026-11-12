"""模块 #24 不良品処分 CSV 生成器.

替代 Lark 共有 Excel 模板「【確定版】不良処分用_注文書・配送伝票作成フォーマット_原本」.
两步流程:
  Step A: 注文書 CSV  →  上传 NetSuite CSV Import ID:164 → 得 SO 番号
  Step B: SO 番号 + 同一処分リスト → 配送伝票 CSV → 上传 ID:165

输入: 処分リスト Excel (5 列: jan / 表示名 / 保管棚 / ステータス / 数量)
      可以直接传原本 .xlsx (自动找 '処分リスト' sheet),
      也可以只传简版 .xlsx (只含 5 列的 sheet1).
"""
from __future__ import annotations

import io
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st

from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("不良品処分 CSV"), page_icon="♻️", layout="wide")

from shared.auth import require_admin
require_admin()
from shared.theme import inject_theme
inject_theme()
lang_selector()


# ============================================================
# 下拉选项 (来自原 Excel 「初期設定」リストデータ列)
# ============================================================
CUSTOMER_OPTIONS = [
    "C000495 三金商事株式会社（不良品処分専用）",
]
LOCATION_OPTIONS = [
    "弁天倉庫",   # Boss 2026-11-04 京东仓不再使用
]
DEPARTMENT_OPTIONS = [
    "輸出事業部",
    "輸出事業部 : 輸出（ASEAN）",
    "輸出事業部 : 輸出（USA）",
    "輸出事業部 : 輸出（中国）",
]
EMPLOYEE_OPTIONS = [
    "005 川崎里子",
    "037 米澤和敏",
    "043 徐越",
    "079 隋艶偉",
    "031 斎藤裕史",
]
CARRIER_OPTIONS = ["社員便", "ヤマト運輸", "佐川急便"]

ORDER_COLUMNS = [
    "外部ID", "顧客:プロジェクト", "日付", "営業担当者", "部門",
    "出荷倉庫", "配送業者", "アイテム", "金額", "単価/率",
    "税額", "総額", "価格水準", "数量", "メモ", "説明",
]
SHIPPING_COLUMNS = [
    "外部ID", "日付", "作成元", "アイテム", "メモ",
    "部門", "場所", "数量", "保管棚", "ステータス",
]


# ============================================================
# 工具函数
# ============================================================
def _load_disposal_list(file_bytes: bytes) -> pd.DataFrame:
    """从上传 .xlsx 中提取「処分リスト」5 列数据。

    优先找 sheet name = '処分リスト', 找不到则用第 1 个 sheet。
    返回标准列名 DataFrame: jan / display_name / bin / status / qty
    """
    xls = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
    sheet = "処分リスト" if "処分リスト" in xls.sheet_names else xls.sheet_names[0]
    df = pd.read_excel(xls, sheet_name=sheet, dtype=str)
    # 容忍多种列名 (原表中文/日文混用)
    col_map = {}
    for c in df.columns:
        s = str(c).strip()
        if s in ("アイテム", "JAN", "jan", "UPC", "upc"):
            col_map[c] = "jan"
        elif s in ("表示名", "商品名", "name", "display_name"):
            col_map[c] = "display_name"
        elif s in ("保管棚番号", "保管棚", "bin", "棚番"):
            col_map[c] = "bin"
        elif s in ("ステータス", "status", "状态"):
            col_map[c] = "status"
        elif s in ("手持", "数量", "qty", "quantity"):
            col_map[c] = "qty"
    df = df.rename(columns=col_map)
    required = {"jan", "display_name", "bin", "status", "qty"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(t("必須列が見つかりません: {missing}。実際の列: {cols}").format(
            missing=missing, cols=list(df.columns)))
    df = df[["jan", "display_name", "bin", "status", "qty"]].dropna(subset=["jan"])
    df = df[df["jan"].astype(str).str.strip() != ""].reset_index(drop=True)
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)
    return df


def _build_order_csv(items: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """注文書 CSV (16 列)。各行 = 処分リストの 1 アイテム。

    Excel 原表のロジック:
      外部ID/顧客/日付/担当/部門/倉庫/配送業者/メモ/説明 → 初期設定 固定値
      アイテム = jan + ' ' + 表示名
      金額/単価/税額/総額 = '0', 価格水準 = 'カスタム'
      数量 = 処分リスト.手持
    """
    rows = []
    for _, r in items.iterrows():
        rows.append({
            "外部ID": cfg["order_external_id"],
            "顧客:プロジェクト": cfg["customer"],
            "日付": cfg["date"],
            "営業担当者": cfg["employee"],
            "部門": cfg["department"],
            "出荷倉庫": cfg["location"],
            "配送業者": cfg["carrier"],
            "アイテム": f"{r['jan']} {r['display_name']}".strip(),
            "金額": "0",
            "単価/率": "0",
            "税額": "0",
            "総額": "0",
            "価格水準": "カスタム",
            "数量": int(r["qty"]),
            "メモ": cfg["memo"],
            "説明": cfg["description"],
        })
    return pd.DataFrame(rows, columns=ORDER_COLUMNS)


def _build_shipping_csv(items: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """配送伝票 CSV (10 列)。"""
    sales_order = cfg["sales_order"].strip()
    source = f"注文書 #{sales_order}" if not sales_order.startswith("注文書") else sales_order
    rows = []
    for _, r in items.iterrows():
        rows.append({
            "外部ID": cfg["shipping_external_id"],
            "日付": cfg["date"],
            "作成元": source,
            "アイテム": r["jan"],
            "メモ": cfg["memo"],
            "部門": cfg["department"],
            "場所": cfg["location"],
            "数量": int(r["qty"]),
            "保管棚": r["bin"],
            "ステータス": r["status"] or "不良品",
        })
    return pd.DataFrame(rows, columns=SHIPPING_COLUMNS)


def _to_csv_bytes(df: pd.DataFrame) -> bytes:
    """UTF-8 BOM CSV (NetSuite 日本語環境互換)。"""
    return df.to_csv(index=False).encode("utf-8-sig")


def _default_external_id(prefix_dt: datetime | None = None) -> str:
    """伝票 ID 例: 202602161555。"""
    dt = prefix_dt or datetime.now(timezone(timedelta(hours=9)))
    return dt.strftime("%Y%m%d%H%M")


def _build_template_xlsx() -> bytes:
    """処分リスト 空テンプレート (見出し行のみ) を xlsx として返す。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "処分リスト"
    ws.append(["アイテム", "表示名", "保管棚番号", "ステータス", "手持"])
    # ヘッダ太字
    from openpyxl.styles import Font
    for c in ws[1]:
        c.font = Font(bold=True)
    # 列幅
    for col, w in zip(["A", "B", "C", "D", "E"], [16, 40, 12, 10, 8]):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ============================================================
# UI
# ============================================================
st.title(t("♻️ 不良品処分 CSV 生成"))
st.caption(t(
    "処分リスト Excel をアップロード → 初期設定を入力 → "
    "注文書 / 配送伝票 CSV をダウンロード (NetSuite CSV Import ID:164 / ID:165)"
))

with st.expander(t("📖 操作フロー (5 ステップ)"), expanded=False):
    st.markdown(t("""
1. **アイテムリスト準備**: 弁天倉庫不良棚整理 Lark グループのマニュアル参照、処分対象 SKU を 5 列形式 (jan / 表示名 / 保管棚 / ステータス / 数量) で Excel 化
2. **アップロード + 初期設定**: 下記フォームで Excel をアップロードし、配送業者・担当者などを入力
3. **注文書 CSV ダウンロード**: 「📦 注文書」タブからダウンロード → NetSuite CSV Import **ID:164** にアップロード
4. **SO 番号取得**: NetSuite で生成された注文書の SO 番号 (例: SO00499319) を控える
5. **配送伝票 CSV ダウンロード**: 「🚚 配送伝票」タブで SO 番号を入力 → ダウンロード → NetSuite CSV Import **ID:165** にアップロード
    """))

st.divider()

# ---------- Step 1: アップロード + 初期設定 ----------
col_up, col_cfg = st.columns([1, 2])

with col_up:
    st.subheader(t("① 処分リスト Excel"))
    st.download_button(
        t("📥 空テンプレートをダウンロード"),
        data=_build_template_xlsx(),
        file_name="処分リスト_template.xlsx",
        mime="application/vnd.openxmlformats-officedrawing.spreadsheetml.sheet",
        help=t("見出し行のみの空 xlsx。記入後にアップロードしてください"),
        use_container_width=True,
    )
    uploaded = st.file_uploader(
        t("Excel ファイル (.xlsx)"),
        type=["xlsx"],
        help=t("『処分リスト』sheet を自動検出。または 5 列のみの簡易 .xlsx でも可"),
    )
    if uploaded:
        try:
            df_items = _load_disposal_list(uploaded.getvalue())
            st.session_state["disposal_items"] = df_items
            st.success(t("✅ {n} 件のアイテム読込完了").format(n=len(df_items)))
        except Exception as e:
            st.session_state.pop("disposal_items", None)
            st.error(f"❌ {e}")

    if "disposal_items" in st.session_state:
        st.dataframe(
            st.session_state["disposal_items"],
            use_container_width=True, hide_index=True, height=260,
        )

with col_cfg:
    st.subheader(t("② 初期設定"))
    today_jst = datetime.now(timezone(timedelta(hours=9)))

    c1, c2 = st.columns(2)
    with c1:
        date_val = st.date_input(t("日付"), value=today_jst.date())
        order_eid = st.text_input(
            t("注文書 外部ID"), value=_default_external_id(today_jst),
            help=t("伝票がなければ日付＆時間 (例: 202602161555)"),
        )
        ship_eid = st.text_input(
            t("配送伝票 外部ID"),
            value=_default_external_id(today_jst + timedelta(minutes=1)),
            help=t("注文書とは異なる ID にすること"),
        )
        customer = st.selectbox(t("顧客"), CUSTOMER_OPTIONS, index=0)
        location = st.selectbox(t("場所 (出荷倉庫)"), LOCATION_OPTIONS, index=0)
    with c2:
        department = st.selectbox(t("部門"), DEPARTMENT_OPTIONS, index=1)
        employee = st.selectbox(t("従業員 (営業担当者)"), EMPLOYEE_OPTIONS, index=4)
        carrier = st.selectbox(t("配送業者"), CARRIER_OPTIONS, index=0)
        memo = st.text_input(t("メモ"), value="輸出 不良棚処分")
        description = st.text_input(t("説明"), value="現地顧客処分")

    cfg_base = {
        "date": date_val.strftime("%Y-%m-%d 00:00:00"),
        "order_external_id": order_eid.strip(),
        "shipping_external_id": ship_eid.strip(),
        "customer": customer,
        "location": location,
        "department": department,
        "employee": employee,
        "carrier": carrier,
        "memo": memo,
        "description": description,
    }
    st.session_state["disposal_cfg"] = cfg_base

st.divider()

# ---------- Step 2: タブで 2 種類の CSV を出力 ----------
df_items = st.session_state.get("disposal_items")
cfg = st.session_state.get("disposal_cfg", {})
ready = df_items is not None and len(df_items) > 0 and cfg.get("order_external_id")

tab_order, tab_ship = st.tabs([t("📦 注文書 CSV (ID:164)"), t("🚚 配送伝票 CSV (ID:165)")])

with tab_order:
    if not ready:
        st.info(t("← 先に Excel をアップロードして初期設定を入力してください"))
    else:
        df_order = _build_order_csv(df_items, cfg)
        st.dataframe(df_order, use_container_width=True, hide_index=True, height=360)
        c1, c2 = st.columns([1, 3])
        with c1:
            st.download_button(
                t("⬇️ 注文書 CSV ダウンロード"),
                data=_to_csv_bytes(df_order),
                file_name=f"order_{cfg['order_external_id']}.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
            )
        with c2:
            st.caption(t(
                "行数: {n} · 数量合計: {q}  ·  "
                "NetSuite 設定＞保存済 CSV インポート ID:164 にアップロード"
            ).format(n=len(df_order), q=df_order['数量'].astype(int).sum()))

with tab_ship:
    if not ready:
        st.info(t("← 先に Excel をアップロードして初期設定を入力してください"))
    else:
        sales_order = st.text_input(
            t("注文書 SO 番号"),
            value=st.session_state.get("disposal_so", ""),
            placeholder="SO00499319",
            help=t("ID:164 で生成された注文書の SO 番号を入力"),
        )
        st.session_state["disposal_so"] = sales_order
        if not sales_order.strip():
            st.warning(t("⚠️ SO 番号を入力してください"))
        else:
            cfg_ship = {**cfg, "sales_order": sales_order}
            df_ship = _build_shipping_csv(df_items, cfg_ship)
            st.dataframe(df_ship, use_container_width=True, hide_index=True, height=360)
            c1, c2 = st.columns([1, 3])
            with c1:
                st.download_button(
                    t("⬇️ 配送伝票 CSV ダウンロード"),
                    data=_to_csv_bytes(df_ship),
                    file_name=f"shipping_{cfg['shipping_external_id']}.csv",
                    mime="text/csv",
                    type="primary",
                    use_container_width=True,
                )
            with c2:
                st.caption(t(
                    "行数: {n} · 数量合計: {q}  ·  "
                    "NetSuite 設定＞保存済 CSV インポート ID:165 にアップロード"
                ).format(n=len(df_ship), q=df_ship['数量'].astype(int).sum()))
