"""模块 #23 Tag 管理 · Shopify 上架标签中央枢纽

两层 tag 体系：
  ┌─ 基础属性层（固化 · 不变）── item_shopify_tags 表
  │   品牌 / 集团 / PDP模板 / 类目 / 成分 / 功效 / IP / 内容Tag
  │   由 shopify_classifier 跑一次固化，Tab 3 偶尔重跑
  └─ 市场标签层（会变 · 不定期改）── item_market_tags 表
      GEO 搜索词 / 季节 / 促销 / 热度 / 优先级
      Tab 2「打便签」就是维护这层

3 个 Tab：
  📋 基础属性浏览   → item_shopify_tags 只读 + 搜索筛选
  🏷️ 市场标签管理   → 给产品打/改市场便签（CRUD）+ 输出合并 Shopify CSV
  🔄 重跑基础分类   → 重新跑分类器固化（管理员偶尔用）

依赖：
- shared/shopify_classifier.py
- item_master_netsuite 表（on_hand>0 取有库存）
- item_shopify_tags / item_market_tags 表（首次访问自动建）
- ~/.smikie-shopify-token（直推用）
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import streamlit as st

# Fleet C3 重组：shopify_classifier 在 shopify/lib/（业务规则归 shopify 仓库 · 与 CMS 解耦）
# 路径来源优先级：①环境变量 SHOPIFY_LIB_PATH ②~/CC/shopify/lib（Mac 开发态）
import os as _os, sys as _sys
_shopify_lib = _os.environ.get("SHOPIFY_LIB_PATH") or _os.path.expanduser("~/CC/shopify/lib")
if _os.path.isdir(_shopify_lib) and _shopify_lib not in _sys.path:
    _sys.path.insert(0, _shopify_lib)

from shared.auth import require_admin
from shared.db import get_connection
from shared.i18n import t

_SHOPIFY_CLASSIFIER_AVAILABLE = False
try:
    from shopify_classifier import (
        MARKET_TAG_NAMESPACES,
        SHOPIFY_FULL_CSV_HEADERS,
        add_market_tag,
        classify_sku,
        get_market_tags,
        init_item_shopify_tags_table,
        load_shopify_token,
        market_tags_to_shopify_tags,
        remove_market_tag,
        upsert_classification,
    )
    _SHOPIFY_CLASSIFIER_AVAILABLE = True
except ImportError:
    pass

st.set_page_config(page_title="Tag 管理", page_icon="🏷️", layout="wide")
require_admin()

if not _SHOPIFY_CLASSIFIER_AVAILABLE:
    st.error(
        "🚫 Shopify 分类器模块未加载\n\n"
        "本页依赖 `shopify_classifier`（位于 shopify 仓库的 lib/ 下），当前环境找不到。\n\n"
        "**解决方案**（任选其一）：\n"
        "- 设置环境变量 `SHOPIFY_LIB_PATH` 指向 shopify/lib 目录\n"
        "- 把 shopify/lib 部署到本机 `~/CC/shopify/lib`（仅 Mac 开发态）"
    )
    st.stop()

st.title(t("🏷️ Tag 管理"))
st.caption(t("基础属性（固化）+ 市场标签（打便签 · 不定期改）· Shopify 上架专用"))


@st.cache_resource
def _ensure_tables() -> None:
    init_item_shopify_tags_table(get_connection())


_ensure_tables()

TEMPLATE_OPTIONS = ["standard", "food", "otc", "ip", "bundle", "brand-collab", "age-gated"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


# ═══════════════════════════════════════════════════════════════════
tab_base, tab_market, tab_reclassify = st.tabs(
    ["📋 基础属性浏览", "🏷️ 市场标签管理（打便签）", "🔄 重跑基础分类"]
)

# ╔══════════════════════════════════════════════════════════════════╗
# ║ Tab 1 · 基础属性浏览（只读）                                       ║
# ╚══════════════════════════════════════════════════════════════════╝
with tab_base:
    st.subheader(t("📋 产品基础属性（固化层）"))
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) FROM item_shopify_tags").fetchone()[0]
    have_stock = conn.execute("SELECT COUNT(*) FROM item_master_netsuite WHERE on_hand > 0").fetchone()[0]
    classified_at = conn.execute("SELECT MAX(classified_at) FROM item_shopify_tags").fetchone()[0] or "—"

    c1, c2, c3 = st.columns(3)
    c1.metric("📦 有库存 SKU", f"{have_stock:,}")
    c2.metric("🏷️ 已固化基础属性", f"{total:,}")
    c3.metric("🕐 最后固化时间", str(classified_at)[:16])

    st.divider()
    with st.expander("🔍 搜索筛选", expanded=True):
        f1, f2, f3 = st.columns(3)
        kw = f1.text_input("商品名 / JAN 关键词")
        brand_kw = f2.text_input("子品牌 包含")
        parent_kw = f3.text_input("集团品牌 包含")
        f4, f5, f6 = st.columns(3)
        tmpl_f = f4.multiselect("PDP 模板", TEMPLATE_OPTIONS)
        cat_kw = f5.text_input("类目 (cat_l1 / cat_l2) 包含")
        min_conf = f6.slider("置信度 ≥", 0.0, 1.0, 0.0, 0.05)

    df = pd.read_sql_query(
        """SELECT t.jan, t.display_name, m.on_hand AS stock, t.brand, t.parent_brand,
                  t.template_suffix, t.cat_l1, t.cat_l2, t.ip, t.flags_csv,
                  t.content_tags_csv, t.confidence, t.pushed_to_shopify
           FROM item_shopify_tags t
           LEFT JOIN item_master_netsuite m
             ON COALESCE(NULLIF(m.upc,''), m.internal_id) = t.jan""",
        conn,
    )
    df["template"] = df["template_suffix"].str.replace("smikie-", "", regex=False)

    if kw:
        k = kw.lower()
        df = df[df["jan"].astype(str).str.lower().str.contains(k, na=False)
                | df["display_name"].astype(str).str.lower().str.contains(k, na=False)]
    if brand_kw:
        df = df[df["brand"].fillna("").str.contains(brand_kw, case=False, na=False)]
    if parent_kw:
        df = df[df["parent_brand"].fillna("").str.contains(parent_kw, case=False, na=False)]
    if tmpl_f:
        df = df[df["template"].isin(tmpl_f)]
    if cat_kw:
        df = df[df["cat_l1"].fillna("").str.contains(cat_kw, case=False, na=False)
                | df["cat_l2"].fillna("").str.contains(cat_kw, case=False, na=False)]
    if min_conf > 0:
        df = df[df["confidence"].fillna(0) >= min_conf]

    st.markdown(f"**过滤后 {len(df):,} 条**")
    st.dataframe(
        df[["jan", "display_name", "stock", "brand", "parent_brand", "template",
            "cat_l1", "cat_l2", "ip", "flags_csv", "content_tags_csv", "confidence", "pushed_to_shopify"]]
        .rename(columns={
            "jan": "JAN", "display_name": "商品名", "stock": "库存", "brand": "子品牌",
            "parent_brand": "集团", "template": "模板", "cat_l1": "类目L1", "cat_l2": "类目L2",
            "ip": "IP", "flags_csv": "Flags", "content_tags_csv": "内容Tag",
            "confidence": "置信", "pushed_to_shopify": "已推",
        }),
        use_container_width=True, hide_index=True, height=480,
    )
    st.download_button("📥 下载当前筛选结果 CSV", _df_to_csv_bytes(df),
                       file_name=f"base-attributes-{datetime.now():%Y%m%d-%H%M%S}.csv", mime="text/csv")


# ╔══════════════════════════════════════════════════════════════════╗
# ║ Tab 2 · 市场标签管理（打便签 · 会变）                              ║
# ╚══════════════════════════════════════════════════════════════════╝
with tab_market:
    st.subheader(t("🏷️ 给产品打市场便签"))
    st.caption(t("市场标签随市场波动 · 不定期改 · 命名空间：GEO 搜索词 / 季节 / 促销 / 热度 / 优先级"))
    conn = get_connection()

    # ── 命名空间统计 ──
    ns_stats = pd.read_sql_query(
        "SELECT namespace, COUNT(*) AS 标签数, COUNT(DISTINCT jan) AS 涉及SKU数 "
        "FROM item_market_tags WHERE active=1 GROUP BY namespace ORDER BY 标签数 DESC", conn,
    )
    if len(ns_stats):
        st.dataframe(ns_stats, hide_index=True, use_container_width=True)
    else:
        st.info("还没有任何市场标签。下面开始打第一批。")

    st.divider()

    # ── A. 批量打标签 ──
    st.markdown("### ➕ 批量打标签")
    pa1, pa2 = st.columns([1, 1])
    with pa1:
        ns = st.selectbox("命名空间 namespace", MARKET_TAG_NAMESPACES,
                          help="geo-xx = 该 SKU 在某市场的搜索词；season/promo/trend/priority = 季节/促销/热度/优先级")
    with pa2:
        if ns.startswith("geo-"):
            st.caption(f"`{ns}` → 输入该 SKU 在 {ns[4:].upper()} 市场的搜索关键词（逗号分隔，会拆成多个 tag）")
        else:
            st.caption(f"`{ns}` → 输入标签值，如 summer / 66-sale / viral / hero")

    sel_source = st.radio("选哪些 SKU", ["粘贴 JAN 清单", "按基础属性筛选选中", "上传 Excel/CSV（jan + tag_value 两列）"],
                          horizontal=True)

    targets: list[tuple[str, str]] = []  # (jan, tag_value)
    if sel_source == "粘贴 JAN 清单":
        jan_txt = st.text_area("每行一个 JAN", height=120)
        tag_val = st.text_input(f"统一打的标签值（{ns}）")
        note = st.text_input("备注（可选）")
        if st.button("➕ 打标签", type="primary") and jan_txt.strip() and tag_val.strip():
            jans = [x.strip() for x in jan_txt.splitlines() if x.strip()]
            ts = _now_iso()
            for j in jans:
                add_market_tag(conn, j, ns, tag_val.strip(), note, "page23", ts)
            conn.commit()
            st.success(f"✅ 给 {len(jans)} 个 SKU 打上 `{ns}` = `{tag_val.strip()}`")

    elif sel_source == "按基础属性筛选选中":
        ff1, ff2, ff3 = st.columns(3)
        bf = ff1.text_input("子品牌包含", key="mkt_bf")
        cf = ff2.text_input("类目包含", key="mkt_cf")
        tf = ff3.multiselect("PDP模板", TEMPLATE_OPTIONS, key="mkt_tf")
        q = "SELECT jan, display_name, brand, cat_l1, cat_l2 FROM item_shopify_tags WHERE 1=1"
        params: list[Any] = []
        if bf:
            q += " AND brand LIKE ?"; params.append(f"%{bf}%")
        if cf:
            q += " AND (cat_l1 LIKE ? OR cat_l2 LIKE ?)"; params += [f"%{cf}%", f"%{cf}%"]
        if tf:
            q += " AND template_suffix IN (" + ",".join(["?"] * len(tf)) + ")"
            params += [f"smikie-{t}" for t in tf]
        cand = pd.read_sql_query(q + " LIMIT 2000", conn, params=params)
        st.markdown(f"**命中 {len(cand)} 条**")
        st.dataframe(cand, hide_index=True, use_container_width=True, height=240)
        tag_val2 = st.text_input(f"给这批打的标签值（{ns}）", key="mkt_tagval2")
        note2 = st.text_input("备注（可选）", key="mkt_note2")
        if st.button("➕ 给筛选结果打标签", type="primary") and tag_val2.strip() and len(cand):
            ts = _now_iso()
            for j in cand["jan"]:
                add_market_tag(conn, str(j), ns, tag_val2.strip(), note2, "page23", ts)
            conn.commit()
            st.success(f"✅ 给 {len(cand)} 个 SKU 打上 `{ns}` = `{tag_val2.strip()}`")

    else:  # 上传
        up = st.file_uploader("上传 Excel/CSV（需含 jan 和 tag_value 两列，可选 note）", type=["xlsx", "xls", "csv"])
        if up:
            df_up = pd.read_csv(up, dtype=str) if up.name.endswith(".csv") else pd.read_excel(up, dtype=str)
            df_up.columns = [c.lower().strip() for c in df_up.columns]
            jc = next((c for c in df_up.columns if c in ("jan", "item_code", "barcode")), None)
            vc = next((c for c in df_up.columns if c in ("tag_value", "value", "tag", "搜索词")), None)
            nc = next((c for c in df_up.columns if c in ("note", "备注")), None)
            if not jc or not vc:
                st.error(f"❌ 缺列。识别到：{list(df_up.columns)}")
            else:
                st.dataframe(df_up.head(20), hide_index=True, use_container_width=True)
                if st.button("➕ 导入并打标签", type="primary"):
                    ts = _now_iso()
                    n = 0
                    for _, r in df_up.iterrows():
                        if pd.notna(r[jc]) and pd.notna(r[vc]):
                            add_market_tag(conn, str(r[jc]), ns, str(r[vc]),
                                           str(r[nc]) if nc and pd.notna(r[nc]) else "", "page23-upload", ts)
                            n += 1
                    conn.commit()
                    st.success(f"✅ 导入 {n} 条市场标签")

    st.divider()

    # ── B. 单 SKU 查看/删除 ──
    st.markdown("### 🔍 查看 / 删除单个 SKU 的市场标签")
    look_jan = st.text_input("输入 JAN", key="mkt_lookup")
    if look_jan:
        base = conn.execute(
            "SELECT display_name, brand, parent_brand, template_suffix, cat_l1, cat_l2 FROM item_shopify_tags WHERE jan=?",
            (look_jan,)).fetchone()
        if base:
            st.markdown(f"**{base[0]}** · {base[1]} / {base[2]} · {base[3]} · {base[4]}/{base[5]}")
        mts = get_market_tags(conn, look_jan)
        if mts:
            for nsx, valx, notex in mts:
                cc1, cc2, cc3, cc4 = st.columns([2, 3, 3, 1])
                cc1.code(nsx)
                cc2.write(valx)
                cc3.caption(notex or "—")
                if cc4.button("🗑", key=f"del_{nsx}_{valx}"):
                    remove_market_tag(conn, look_jan, nsx, valx)
                    conn.commit()
                    st.rerun()
        else:
            st.caption(t("该 SKU 暂无市场标签"))

    st.divider()

    # ── C. 导出最终 Shopify CSV（基础内容 tag + 市场 tag 合并）──
    st.markdown("### 📥 导出 Shopify 上传 CSV（基础内容 tag + 市场 tag 合并）")
    st.caption(t("Tags 列 = 内容性基础 tag + 当前所有 active 市场标签；Template Suffix 单独列；不含工程性 tag"))
    if st.button("🔧 生成合并 CSV"):
        base_df = pd.read_sql_query(
            "SELECT jan, display_name, handle, brand, template_suffix, content_tags_csv FROM item_shopify_tags", conn)
        mkt_df = pd.read_sql_query(
            "SELECT jan, namespace, tag_value, note FROM item_market_tags WHERE active=1", conn)
        mkt_by_jan: dict[str, list[tuple[str, str, str]]] = {}
        for _, r in mkt_df.iterrows():
            mkt_by_jan.setdefault(r["jan"], []).append((r["namespace"], r["tag_value"], r["note"] or ""))

        lite_rows, full_rows = [], []
        for _, r in base_df.iterrows():
            content = [t.strip() for t in (r["content_tags_csv"] or "").split(",") if t.strip()]
            mkt = market_tags_to_shopify_tags(mkt_by_jan.get(r["jan"], []))
            all_tags = list(dict.fromkeys(content + mkt))
            lite_rows.append({
                "Handle": r["handle"] or f"sku-{r['jan']}",
                "Tags": ", ".join(all_tags),
                "Template Suffix": r["template_suffix"],
            })
            full_rows.append({
                "Handle": r["handle"] or f"sku-{r['jan']}",
                "Title": r["display_name"], "Body (HTML)": "", "Vendor": r["brand"],
                "Product Category": "", "Type": "", "Tags": ", ".join(all_tags), "Published": "TRUE",
                "Option1 Name": "Title", "Option1 Value": "Default Title", "Variant SKU": r["jan"],
                "Variant Inventory Tracker": "shopify", "Variant Inventory Qty": "",
                "Variant Inventory Policy": "deny", "Variant Fulfillment Service": "manual",
                "Variant Price": "", "Variant Requires Shipping": "TRUE", "Variant Taxable": "TRUE",
                "Variant Barcode": r["jan"], "Gift Card": "FALSE", "SEO Title": "", "SEO Description": "",
                "Status": "draft", "Template Suffix": r["template_suffix"],
            })
        lite_df = pd.DataFrame(lite_rows)
        full_df = pd.DataFrame(full_rows, columns=SHOPIFY_FULL_CSV_HEADERS)
        ts_s = datetime.now().strftime("%Y%m%d-%H%M%S")
        d1, d2 = st.columns(2)
        d1.download_button("📤 轻量更新 CSV（Handle/Tags/Template）", _df_to_csv_bytes(lite_df),
                           file_name=f"shopify-tags-merged-lite-{ts_s}.csv", mime="text/csv")
        d2.download_button("📦 完整 products.csv", _df_to_csv_bytes(full_df),
                           file_name=f"shopify-products-{ts_s}.csv", mime="text/csv")
        st.success(f"✅ {len(lite_df)} 条 · 内容 tag + 市场 tag 已合并")

    # GraphQL 直推占位
    st.divider()
    token, store = load_shopify_token()
    if token:
        st.caption(f"☁️ Shopify token 已就绪（store `{store}`）—— 直推按钮下一版接入")
    else:
        st.caption(t("☁️ `~/.smikie-shopify-token` 未配置 —— 直推不可用"))


# ╔══════════════════════════════════════════════════════════════════╗
# ║ Tab 3 · 重跑基础分类（管理员偶尔用）                               ║
# ╚══════════════════════════════════════════════════════════════════╝
with tab_reclassify:
    st.subheader(t("🔄 重新跑基础属性分类并固化"))
    st.warning("⚠️ 这会覆盖 item_shopify_tags 表里的基础属性。只在分类规则更新后才需要重跑。市场标签（item_market_tags）不受影响。")
    conn = get_connection()

    src = st.radio("数据源", ["全量有库存（item_master_netsuite.on_hand>0）", "粘贴 JAN 清单", "上传 NetSuite XLS"])
    items: list[tuple[str, str, str | None]] = []

    if src.startswith("全量"):
        if st.button("📥 加载全量有库存"):
            rows = conn.execute(
                "SELECT COALESCE(NULLIF(upc,''), internal_id), display_name, maker "
                "FROM item_master_netsuite WHERE on_hand>0 AND display_name IS NOT NULL").fetchall()
            items = [(r[0], r[1] or "", r[2] or None) for r in rows]
            st.session_state["_reclassify_items"] = items
            st.success(f"✅ {len(items)} 条待重跑")
    elif src.startswith("粘贴"):
        txt = st.text_area("每行一个 JAN", height=120, key="recl_jans")
        if st.button("📥 解析") and txt.strip():
            jans = [x.strip() for x in txt.splitlines() if x.strip()]
            ph = ",".join(["?"] * len(jans))
            rows = conn.execute(
                f"SELECT COALESCE(NULLIF(upc,''),internal_id), display_name, maker FROM item_master_netsuite "
                f"WHERE upc IN ({ph}) OR internal_id IN ({ph})", jans + jans).fetchall()
            found = {r[0]: (r[1], r[2]) for r in rows}
            items = [(j, found.get(j, ("", None))[0] or "", found.get(j, ("", None))[1]) for j in jans]
            st.session_state["_reclassify_items"] = items
            st.success(f"✅ {len(items)} 条（{len(found)} 在主档）")
    else:
        up = st.file_uploader("NetSuite XLS", type=["xls", "xlsx"])
        if up:
            d = pd.read_excel(up, dtype=str)
            d.columns = [c.lower().strip() for c in d.columns]
            jc = next((c for c in d.columns if c in ("item_code", "jan", "upc", "barcode")), None)
            nc = next((c for c in d.columns if c in ("display_name", "name", "商品名")), None)
            mc = next((c for c in d.columns if c in ("maker", "vendor", "メーカー")), None)
            if jc and nc:
                items = [(str(r[jc]), str(r[nc]), str(r[mc]) if mc else None)
                         for _, r in d.iterrows() if pd.notna(r[jc]) and pd.notna(r[nc])]
                st.session_state["_reclassify_items"] = items
                st.success(f"✅ {len(items)} 条")
            else:
                st.error(f"❌ 缺列：{list(d.columns)}")

    items = st.session_state.get("_reclassify_items") or items
    if items and st.button(f"🔄 重跑分类并固化 {len(items)} 条", type="primary"):
        ts = _now_iso()
        with st.spinner(f"分类 {len(items)} 条…"):
            for j, n, m in items:
                upsert_classification(conn, classify_sku(j, n, m), ts)
            conn.commit()
        st.success(f"✅ 已固化 {len(items)} 条基础属性（classified_at={ts}）")
        st.cache_resource.clear()
