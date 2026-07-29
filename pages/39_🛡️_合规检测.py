"""模块 #39 合规检测 · 各国上架前单品合规判定(Boss 2026-07-28 指示).

数据源(元川 PG `compliance` schema,cms_reader 只读):
  ingredient_rule(US/PH/CA 禁限用成分) / keyword_rule(名称·宣称·品类词表)
  shopify_item(JAN→标题/成分文本,周次同步) / source_snapshot(官方源监控)
商品检索走 nst.item_master_raw(JAN/商品名),连接=get_readonly_connection()。

判定=纯函数 shared/compliance_engine.py;本页无写路径、无外部 API 调用。
数据更新机制:官方源月次快照+hash diff(cms_compliance_scheduler),变化只告警
不自动改规则;规则表经审查过的迁移文件更新。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.i18n import lang_selector, t, get_lang

st.set_page_config(page_title=t("合规检测"), page_icon="🛡️", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
from shared.db import get_readonly_connection
from shared.compliance_engine import judge
from shared.jan_lookup import lookup as jan_lookup

require_password()
inject_theme()
lang_selector()

_ja = get_lang() == "ja"


def _dl(zh: str, ja: str) -> str:
    return ja if _ja else zh


st.title(_dl("🛡️ 合规检测", "🛡️ コンプライアンス確認"))

# 官方源清单(source_key → 展示名/URL/抓取方式)。与 database/compliance_api/source_watch.py 对齐。
SOURCE_META = {
    "ecfr_700": ("US·eCFR 21 CFR 700(化妆品禁限用成分)", "https://www.ecfr.gov/current/title-21/chapter-I/subchapter-G/part-700/subpart-B", "官方API·全自动"),
    "ecfr_colors": ("US·eCFR 21 CFR 73/74(色素清单)", "https://www.ecfr.gov/current/title-21/part-73", "官方API·全自动"),
    "ecfr_cpsc_1110": ("US·16 CFR 1110(CPSC CPC证书规则)", "https://www.ecfr.gov/current/title-16/part-1110", "官方API·全自动"),
    "fda_ia_53": ("US·FDA Import Alerts(53系+66-41)", "https://www.accessdata.fda.gov/cms_ia/industry_53.html", "HTML diff·全自动"),
    "cpsc_recalls": ("US·CPSC 召回", "https://www.saferproducts.gov/RestWebServices/Recall?format=json", "官方API·全自动"),
    "hsa_acd_pdf": ("PH·ASEAN 化妆品指令 Annexes(HSA 合并PDF)", "https://www.hsa.gov.sg/cosmetic-products/asean-cosmetic-directive", "PDF hash diff·变化后人工解析"),
    "fdaph_circulars": ("PH·FDA Circular 列表(ACD 修订文号)", "https://www.fda.gov.ph/", "HTML·可失败源(反爬)"),
    "hc_hotlist": ("CA·Health Canada Hotlist", "https://www.canada.ca/en/health-canada/services/consumer-product-safety/cosmetics/cosmetic-ingredient-hotlist-prohibited-restricted-ingredients/hotlist.html", "Wayback diff·变化后人工解析"),
}

COUNTRY_NOTES = {
    "US": (
        "- **名称含医药品字眼**(第X類医薬品/医薬品/药效词)→ 未批准新药(Import Alert 66-41),不可上架\n"
        "- **防晒(SPF)/美白治疗性宣称/含氟牙膏/制汗/去屑** = OTC 医药品管辖\n"
        "- **儿童/婴童/玩具**:需 CPSC 儿童产品证书(CPC)+第三方检测,2026-07-08 起通关 eFiling 强制\n"
        "- **食品/补充剂**:需 FDA 食品设施注册 + Prior Notice\n"
        "- 化妆品成分:21 CFR 700 禁限用 + 色素只可用 73/74 批准清单"),
    "PH": (
        "- **化妆品不得医疗宣称**(cure/treat/relieve 等 drug claims 全禁)\n"
        "- 成分按 ASEAN 化妆品指令 Annex II 禁用清单(以 EU CosIng 为基准)\n"
        "- 上市需 FDA PH 化妆品 notification(跨境零售灰区,宣称仍按化妆品标准)\n"
        "- OTC 医药品不可跨境零售"),
    "CA": (
        "- 成分按 **Health Canada Hotlist**(禁用约500项+限用约90项,限用注意限量/警示语)\n"
        "- 化妆品-药品边界:治疗性宣称(cosmetic-drug interface)会被要求按药品注册\n"
        "- 标签需英法双语;儿童产品有加拿大消费品安全法(CCPSA)要求\n"
        "- OTC 医药品不可跨境零售"),
}


def _conn():
    return get_readonly_connection()


@st.cache_data(ttl=1800, show_spinner=False)
def _load_rules():
    """规则表 → list[dict](引擎输入)。compliance schema 不存在(本地)→ None。"""
    try:
        conn = _conn()
        kw = pd.read_sql_query(
            "SELECT country, category, pattern, severity, note FROM compliance.keyword_rule "
            "WHERE enabled = TRUE", conn)
        ing = pd.read_sql_query(
            "SELECT country, ingredient, match_terms, cas, rule_type, condition_note, "
            "source, source_ref, source_version FROM compliance.ingredient_rule", conn)
        return kw.to_dict("records"), ing.to_dict("records")
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def _load_overview():
    try:
        conn = _conn()
        ing = pd.read_sql_query(
            "SELECT country, rule_type, COUNT(*) AS n FROM compliance.ingredient_rule "
            "GROUP BY country, rule_type", conn)
        kw = pd.read_sql_query(
            "SELECT country, severity, COUNT(*) AS n FROM compliance.keyword_rule "
            "WHERE enabled = TRUE GROUP BY country, severity", conn)
        snap = pd.read_sql_query(
            "SELECT source_key, fetched_at, http_status, changed, diff_note FROM ("
            "  SELECT s.*, ROW_NUMBER() OVER (PARTITION BY source_key ORDER BY fetched_at DESC) rn"
            "  FROM compliance.source_snapshot s) x WHERE rn = 1", conn)
        cov = pd.read_sql_query(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN ingredient_text IS NOT NULL "
            "AND ingredient_text <> '' THEN 1 ELSE 0 END) AS with_ing, MAX(updated_at) AS last_sync "
            "FROM compliance.shopify_item", conn)
        return {"ing": ing, "kw": kw, "snap": snap, "cov": cov}
    except Exception:
        return None


def _rows(sql: str, params: list) -> list[tuple]:
    """单表查询;表不存在(本地/未迁移)→ 空,不阻断判定。"""
    try:
        return _conn().execute(sql, params).fetchall()
    except Exception:
        return []


def _is_jan(s: str) -> bool:
    return s.isdigit() and len(s) >= 8


def resolve_by_jan(jan: str) -> dict:
    """JAN → 已知的商品名/成分。

    优先级:**飞书内容表 → Shopify 收录 → NST(仅补日文名,不是门槛)**。
    飞书表是人按固定列规范填的上架内容表,JAN 行成分充足率≈100%,且含大量未上架品,
    精度高于任何外部抓取,故排最前。
    """
    sh = _rows("SELECT title, ingredient_text, sheet_name FROM compliance.sheet_item "
               "WHERE jan = ?", [jan])
    sp = _rows("SELECT title, ingredient_text, product_status FROM compliance.shopify_item "
               "WHERE jan = ?", [jan])
    ns = _rows("SELECT display_name, maker FROM nst.item_master_raw WHERE jan = ? LIMIT 1", [jan])
    out = {"jan": jan, "name_en": "", "name_ja": "", "maker": "", "ingredient": "", "sources": []}
    if sh:
        out["name_ja"] = sh[0][0] or ""
        out["ingredient"] = sh[0][1] or ""
        out["sources"].append(_dl(f"飞书内容表({sh[0][2] or '-'})", f"飛書コンテンツ表({sh[0][2] or '-'})"))
    if sp:
        out["name_en"] = sp[0][0] or ""
        # 飞书表の成分が既にあれば上書きしない(優先度: 飞书 > Shopify)
        out["ingredient"] = out["ingredient"] or (sp[0][1] or "")
        out["sources"].append(_dl("Shopify 已上架", "Shopify 出品済"))
    if ns:
        out["name_ja"] = out["name_ja"] or (ns[0][0] or "")
        out["maker"] = ns[0][1] or ""
        out["sources"].append(_dl("NST 商品主档", "NST 商品マスタ"))
    return out


@st.cache_data(ttl=86400, show_spinner=False)
def _external(jan: str) -> dict:
    """外部按 JAN 取商品名/全成分(楽天24→楽天スーパー→Yahoo)。缓存 1 天。"""
    return jan_lookup(jan)


def search_candidates(kw: str) -> list[dict]:
    """关键词 → 候选(Shopify 与 NST 并集,按 JAN 去重)。JAN 输入直接单条返回。"""
    kw = kw.strip()
    if not kw:
        return []
    if _is_jan(kw):
        return [resolve_by_jan(kw)]
    like = f"%{kw.lower()}%"
    found: dict[str, dict] = {}
    for jan, name in _rows(
            "SELECT jan, title FROM compliance.shopify_item "
            "WHERE LOWER(COALESCE(title,'')) LIKE ? ORDER BY jan LIMIT 30", [like]):
        found.setdefault(jan, {"jan": jan, "label": name or jan})
    for jan, name, maker in _rows(
            "SELECT jan, display_name, maker FROM nst.item_master_raw "
            "WHERE LOWER(COALESCE(display_name,'')) LIKE ? OR LOWER(COALESCE(maker,'')) LIKE ? "
            "ORDER BY item_code LIMIT 30", [like, like]):
        if jan:
            found.setdefault(jan, {"jan": jan, "label": f"{name or ''} {maker or ''}".strip()})
    return list(found.values())[:30]


VERDICT_UI = {
    "red": ("error", _dl("🔴 禁止/高风险——不可上架", "🔴 禁止/高リスク——出品不可")),
    "yellow": ("warning", _dl("🟡 注意——有条件/需核对", "🟡 注意——条件付き/要確認")),
    "green": ("success", _dl("🟢 未检出问题", "🟢 問題未検出")),
}


def _render_result(res: dict):
    # 成分が無いまま緑を出すと「安全」と誤読される。名称だけ通過は別状態として出す。
    if res["verdict"] == "green" and not res["ingredient_checked"]:
        st.warning(_dl("⚪ 判定未完成——名称/宣称无问题,但**成分未取得**,成分维度未判定",
                       "⚪ 判定未完了——名称/表現は問題なし,ただし**成分未取得**のため成分は未判定"))
        st.caption(_dl("→ 粘贴成分表(商品包装/厂商页的全成分)后可完成判定",
                       "→ 成分表(パッケージ/メーカー页の全成分)を貼れば判定を完了できます"))
        return
    fn, label = VERDICT_UI[res["verdict"]]
    getattr(st, fn)(label)
    if not res["ingredient_checked"]:
        st.caption(_dl("⚠️ 成分未取得——以上仅为名称/宣称判定,成分维度未覆盖",
                       "⚠️ 成分未取得——名称/表現判定のみ,成分は未カバー"))
    if res["hits"]:
        df = pd.DataFrame(res["hits"])
        df["severity"] = df["severity"].map({"red": "🔴", "yellow": "🟡", "info": "ℹ️"})
        df = df[["severity", "kind", "field", "note", "matched"]]
        df.columns = [_dl("级别", "レベル"), _dl("维度", "区分"), _dl("命中字段", "対象"),
                      _dl("注意事项", "注意事項"), _dl("命中文本", "該当箇所")]
        st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(_dl("※ 未命中≠合规:成分为自由文本匹配,清单外风险(宣称语境/浓度/剂型)仍需人工判断",
                   "※ ヒットなし≠適法:成分はテキスト照合のため,リスト外リスク(表現文脈/濃度/剤形)は人による確認が必要"))


def _country_tab(country: str, rules):
    """JAN 直判:输入 JAN → 已知数据自动填充 → 立即判定;未收录也能粘贴后判定。

    商品是否在 NST/Shopify 里**不作门槛**——上架前的新品本来就不在任何库。
    """
    kw_rules, ing_rules = rules
    st.markdown(_dl(f"##### {country} 单品判定", f"##### {country} 単品判定"))
    q = st.text_input(
        _dl("① 输入 JAN 码 → 自动取商品名与成分并判定", "① JANコード入力 → 商品名と成分を自動取得して判定"),
        key=f"q_{country}", placeholder="4909978147105",
        help=_dl("也可输入商品名关键词,但关键词只查自社数据库(自建站/NST);"
                 "外部自动取数只对 JAN 有效",
                 "商品名キーワードも可。ただし自社DB(自社サイト/NST)のみ検索。外部自動取得は JAN のみ"))
    q = q.strip()

    item = {"jan": "", "name_en": "", "name_ja": "", "maker": "", "ingredient": "", "sources": []}
    if q:
        cands = search_candidates(q)
        if len(cands) > 1:
            labels = {f"{c['jan']} | {c.get('label', '')}": c["jan"] for c in cands}
            picked = st.selectbox(_dl("候选商品", "候補商品"), list(labels), key=f"sel_{country}")
            item = resolve_by_jan(labels[picked])
        elif cands:
            item = cands[0] if "sources" in cands[0] else resolve_by_jan(cands[0]["jan"])

        # 库里没有(上架前的新品=常态),或库里有但缺成分 → 按 JAN 去外部取
        if _is_jan(q) and (not item.get("sources") or not item["ingredient"]):
            with st.spinner(_dl("外部检索中(楽天/Yahoo)…", "外部検索中(楽天/Yahoo)…")):
                ext = _external(q)
            if ext:
                item["jan"] = q
                item["name_ja"] = item["name_ja"] or ext["name"]
                item["ingredient"] = item["ingredient"] or ext["ingredient"]
                item["sources"].append(
                    ext["source"] + ("" if ext["ingredient"] else _dl("(名称のみ)", "(名称のみ)")))

        if item.get("sources"):
            st.caption(_dl(f"数据来源:{' + '.join(item['sources'])}"
                           + ("" if item["ingredient"] else " ・成分未取得(可在下方粘贴)"),
                           f"データ元:{' + '.join(item['sources'])}"
                           + ("" if item["ingredient"] else " ・成分未取得")))
        else:
            st.info(_dl(
                "该 JAN 在自建站/NST 与外部(楽天24・楽天スーパー・Yahoo)均查不到——"
                "多为未在日本零售流通的品;在下方粘贴商品名与成分表即可判定",
                "このJANは自社DB・外部(楽天24/楽天スーパー/Yahoo)いずれにも見つかりません——"
                "下に商品名と成分表を貼り付ければ判定できます"))

    # 判定输入:已知数据预填,可改;未收录时手动粘贴。key 含 JAN → 换商品自动刷新预填。
    slot = item.get("jan") or "manual"
    name = st.text_input(
        _dl("② 商品名(自动填入·可修改)", "② 商品名(自動入力·修正可)"),
        value=item["name_ja"] or item["name_en"], key=f"n_{country}_{slot}")
    name_alt = item["name_en"] if (item["name_ja"] and item["name_en"]) else ""
    ing = st.text_area(
        _dl("③ 成分表(自动取到则填入;取不到时粘贴包装/厂商页的全成分)",
            "③ 成分表(自動取得できれば自動入力;取れない場合はパッケージ/メーカーの全成分を貼付)"),
        value=item["ingredient"], key=f"i_{country}_{slot}", height=110)

    if name.strip() or ing.strip():
        fields = {_dl("商品名", "商品名"): name}
        if name_alt:
            fields[_dl("商品名(EN)", "商品名(EN)")] = name_alt
        _render_result(judge(kw_rules, ing_rules, country, fields, ing or None))

    with st.expander(_dl(f"📌 {country} 通用注意事项", f"📌 {country} 共通注意事項"), expanded=False):
        st.markdown(COUNTRY_NOTES[country])


rules = _load_rules()
tab0, tab_us, tab_ph, tab_ca = st.tabs(
    [_dl("📚 判定标准", "📚 判定基準"), "🇺🇸 US", "🇵🇭 PH", "🇨🇦 CA"])

with tab0:
    ov = _load_overview()
    if ov is None:
        st.info(_dl("compliance schema 未接入(本地环境或迁移未执行)——判定标准总览不可用",
                    "compliance schema 未接続(ローカル環境または未マイグレーション)"))
    else:
        c1, c2, c3 = st.columns(3)
        ing_p = ov["ing"].set_index(["country", "rule_type"])["n"] if not ov["ing"].empty else {}
        for col, country in zip((c1, c2, c3), ("US", "PH", "CA")):
            with col:
                p = int(ing_p.get((country, "prohibited"), 0)) if len(ov["ing"]) else 0
                r = int(ing_p.get((country, "restricted"), 0)) if len(ov["ing"]) else 0
                st.metric(f"{country} " + _dl("成分规则", "成分ルール"),
                          f"{p + r}", _dl(f"禁用 {p} / 限用 {r}", f"禁止 {p} / 制限 {r}"),
                          delta_color="off")
        n_kw = int(ov["kw"]["n"].sum()) if not ov["kw"].empty else 0
        cov = ov["cov"].iloc[0] if not ov["cov"].empty else None
        total = int(cov["total"] or 0) if cov is not None else 0
        with_ing = int(cov["with_ing"] or 0) if cov is not None else 0
        st.caption(_dl(
            f"名称/宣称词表 {n_kw} 条(三国通用+国别) · Shopify 成分收录 {with_ing}/{total} 件"
            + (f" · 成分同步 {pd.to_datetime(cov['last_sync']).strftime('%m-%d %H:%M')}" if cov is not None and cov["last_sync"] else ""),
            f"名称/表現ルール {n_kw} 件 · Shopify 成分収録 {with_ing}/{total} 件"))

        st.markdown(_dl("##### 官方数据源监控(月次自动快照,变化→人工核对入库)",
                        "##### 公式データソース監視(月次自動スナップショット)"))
        snap = ov["snap"]
        changed = snap[snap["changed"] == True] if not snap.empty else pd.DataFrame()  # noqa: E712
        if not changed.empty:
            for r in changed.itertuples():
                meta = SOURCE_META.get(r.source_key, (r.source_key, "", ""))
                st.error(_dl(f"🔔 数据源有更新待核对:{meta[0]} — {r.diff_note or ''}",
                             f"🔔 ソース更新あり(要確認):{meta[0]}"))
        rows = []
        snap_by_key = {r.source_key: r for r in snap.itertuples()} if not snap.empty else {}
        for key, (label, url, mode) in SOURCE_META.items():
            s = snap_by_key.get(key)
            rows.append({
                _dl("数据源", "ソース"): label,
                _dl("抓取方式", "取得方式"): mode,
                _dl("最近检查", "最終チェック"): pd.to_datetime(s.fetched_at).strftime("%Y-%m-%d %H:%M") if s is not None else "—",
                "HTTP": int(s.http_status) if s is not None and pd.notna(s.http_status) else None,
                _dl("有变化", "変化"): ("🔴" if s is not None and s.changed else "—"),
                "URL": url,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                     column_config={"URL": st.column_config.LinkColumn("URL")})
        st.caption(_dl(
            "成分规则只经审查过的迁移文件更新(官方源变化→告警→人工解析入库);"
            "宣称/名称词表按季度对照各国指南人工核对",
            "成分ルールはレビュー済みマイグレーションでのみ更新;表現ルールは四半期ごとに人手照合"))

if rules is None:
    for tb in (tab_us, tab_ph, tab_ca):
        with tb:
            st.info(_dl("规则表未接入(compliance schema 缺失)——无法判定",
                        "ルール未接続(compliance schema なし)——判定不可"))
else:
    with tab_us:
        _country_tab("US", rules)
    with tab_ph:
        _country_tab("PH", rules)
    with tab_ca:
        _country_tab("CA", rules)
