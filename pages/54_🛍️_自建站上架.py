"""模块 #54 自建站上架（美国 · 单品）· Boss 2026-11-20。

CMS 不调 AI：下载「上架包」Excel（README 规则 + 商品模板 + 字段说明 + 选项 + 样例 + 禁用词）→ 同事的 AI 填内容
→ 上传回 CMS 自动检查 → 建 Shopify 草稿（DRAFT）→ 另行上传图片挂到商品页。
改上架（ACTIVE）由人在 Shopify 后台做，本页不做（Boss 2026-11-20「不用等我批准上架」）。
本体：shared/shopify_listing/（spec 口径 · validate 检查 · workbook Excel · shopify 读写 · candidates 候选/台账）。
"""
from __future__ import annotations

import datetime as dt
import io
import re
import zipfile

import pandas as pd
import streamlit as st

from shared.i18n import get_lang, lang_selector, t

st.set_page_config(page_title=t("自建站上架"), page_icon="🛍️", layout="wide")
from shared.auth import require_password  # noqa: E402
from shared.db import get_connection, get_readonly_connection  # noqa: E402
from shared.theme import inject_theme  # noqa: E402
from shared.compliance_engine import judge  # noqa: E402
from shared.shopify_order import ShopifyNotConfigured  # noqa: E402
from shared.shopify_listing import candidates as C  # noqa: E402
from shared.shopify_listing import shopify as SH  # noqa: E402
from shared.shopify_listing import spec as S  # noqa: E402
from shared.shopify_listing import validate as V  # noqa: E402
from shared.shopify_listing import workbook as W  # noqa: E402

require_password()
inject_theme()
lang_selector()


def _u(zh: str, ja: str) -> str:
    return ja if get_lang() == "ja" else zh


st.title(t("🛍️ 自建站上架"))
st.caption(_u("美国 · 单品（组合品不在此页）。流程：① 下载上架包 → 同事的 AI 按 README 填内容 → ② 上传 Excel 检查并建草稿 → "
              "③ 上传图片挂到商品页。本页只建草稿（draft），改上架请在 Shopify 后台操作。",
              "米国 · 単品（セット品は対象外）。流れ：① 出品パックをダウンロード → 同僚の AI が README に従って記入 → "
              "② Excel をアップロードして検査・下書き作成 → ③ 画像をアップロードして商品ページに掲載。"
              "本ページは下書き（draft）まで。公開は Shopify 管理画面で行ってください。"))

OPERATOR = (st.session_state.get("__lark_user") or {}).get("name") or "password-login"


@st.cache_data(ttl=600, show_spinner=False)
def _existing_jans() -> set:
    return SH.existing_jans()


@st.cache_data(ttl=600, show_spinner=False)
def _live_cat_tags() -> set:
    return SH.live_cat_tags()


@st.cache_data(ttl=600, show_spinner=False)
def _cands() -> tuple[list, dict]:
    return C.load(get_readonly_connection())


@st.cache_data(ttl=1800, show_spinner=False)
def _compliance_rules():
    conn = get_readonly_connection()
    kw = pd.read_sql_query("SELECT country, category, pattern, severity, note FROM compliance.keyword_rule "
                           "WHERE enabled = TRUE", conn)
    ing = pd.read_sql_query("SELECT country, ingredient, match_terms, cas, rule_type, condition_note, "
                            "source, source_ref, source_version FROM compliance.ingredient_rule", conn)
    cat = pd.read_sql_query("SELECT country, category, match_terms, exclude_terms, severity, note, blocking, enabled "
                            "FROM compliance.category_rule WHERE enabled = TRUE", conn)
    return kw.to_dict("records"), ing.to_dict("records"), cat.to_dict("records")


def _shopify_ready() -> bool:
    try:
        _existing_jans()
        return True
    except ShopifyNotConfigured as e:
        st.error(str(e))
    except Exception as e:  # noqa: BLE001
        st.error(_u("Shopify 读取失败：", "Shopify 読み取り失敗：") + str(e)[:300])
    return False


tab_dl, tab_up, tab_img, tab_log = st.tabs([
    _u("📦 ① 下载上架包", "📦 ① 出品パック DL"), _u("⬆️ ② 上传内容·建草稿", "⬆️ ② 内容アップ・下書き作成"),
    _u("🖼️ ③ 上传图片", "🖼️ ③ 画像アップ"), _u("📜 台账", "📜 履歴")])

# ============================================================
# ① 下载上架包
# ============================================================
with tab_dl:
    if _shopify_ready():
        with st.spinner(_u("读取候选品与 Shopify 现有商品…", "候補品と Shopify 既存商品を読み込み中…")):
            cands, skipped = _cands()
            have = _existing_jans()
        todo = [c for c in cands if c["jan"] not in have]
        st.caption(_u("候选 = NST 商品等级 NEW · 取扱中 · 非無効 · JAN 8/13 位（整箱 14 位排除）· 定義原価已录 · 厂商已定 · Shopify 还没有这个 JAN。",
                      "候補 = NST ランク NEW · 取扱中 · 無効でない · JAN 8/13 桁（ケース 14 桁は除外）· 定義原価あり · メーカー確定 · Shopify 未登録。"))
        m1, m2, m3 = st.columns(3)
        m1.metric(_u("可上架候选", "出品候補"), f"{len(todo):,}")
        m2.metric(_u("Shopify 已有", "Shopify 登録済"), f"{len(cands) - len(todo):,}")
        m3.metric(_u("排除（原価/厂商）", "除外（原価/メーカー）"), f"{sum(skipped.values()):,}",
                  " · ".join(f"{k} {v}" for k, v in skipped.items()), delta_color="off")
        f1, f2 = st.columns([2, 1])
        kw = f1.text_input(_u("筛选（JAN / 商品名 / 厂商 / 品牌）", "絞り込み（JAN / 商品名 / メーカー / ブランド）"))
        only_stock = f2.toggle(_u("只看有库存", "在庫ありのみ"), value=True)
        df = pd.DataFrame(todo)
        if not df.empty:
            if kw:
                m = df[["jan", "nst_name", "vendor", "brand"]].astype(str).apply(
                    lambda s: s.str.contains(kw, case=False, regex=False)).any(axis=1)
                df = df[m]
            if only_stock:
                df = df[pd.to_numeric(df["stock"], errors="coerce").fillna(0) > 0]
            df = df.sort_values(["vendor", "brand", "jan"]).reset_index(drop=True)
            df.insert(0, "sel", False)
            ed = st.data_editor(
                df[["sel", "jan", "nst_name", "vendor", "brand", "price_usd", "stock"]],
                column_config={"sel": st.column_config.CheckboxColumn(_u("选", "選択")),
                               "jan": "JAN", "nst_name": _u("NST 商品名", "NST 商品名"),
                               "vendor": _u("厂商", "メーカー"), "brand": _u("品牌", "ブランド"),
                               "price_usd": st.column_config.NumberColumn(_u("美国价", "米国価格"), format="$%.2f"),
                               "stock": _u("可用库存", "利用可能在庫")},
                disabled=["jan", "nst_name", "vendor", "brand", "price_usd", "stock"],
                hide_index=True, use_container_width=True, height=420, key="dl_editor")
            picked = df[ed["sel"].values]
            st.caption(_u("共 {n} 件，已选 {k} 件。建议一个上架包 30 件以内，AI 一次能查证的量有限。",
                          "全 {n} 件、選択 {k} 件。1 パック 30 件以内を推奨（AI が一度に調べられる量に限りがあるため）。")
                       .format(n=len(df), k=len(picked)))
            if len(picked):
                data = W.build(picked.drop(columns=["sel"]).to_dict("records"))
                st.download_button(
                    _u("⬇️ 下载上架包（Excel · {k} 件）", "⬇️ 出品パックをダウンロード（Excel · {k} 件）").format(k=len(picked)),
                    data, file_name=f"上架包_US_{dt.datetime.now():%Y%m%d_%H%M}_{len(picked)}件.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
        else:
            st.info(_u("没有可上架的候选品。", "出品候補はありません。"))
        with st.expander(_u("📖 交给 AI 的说法（复制给同事）", "📖 AI への渡し方（同僚にコピー）")):
            st.code("请先完整阅读这个 Excel 的 README 工作表，再看「字段说明」「选项」「样例」「禁用词」工作表，"
                    "然后按 README 的规则逐行填写「商品」工作表的空白列（灰色列不要改）。"
                    "每件商品都要按 JAN 去官网查证，查不到的写「待查」。填完后把整个 Excel 文件交回给我。",
                    language=None)

# ============================================================
# ② 上传内容 · 建草稿
# ============================================================
with tab_up:
    up = st.file_uploader(_u("上传填好的上架包（Excel）", "記入済みの出品パック（Excel）"), type=["xlsx"], key="up_xlsx")
    if up is not None and _shopify_ready():
        try:
            rows = W.read(up.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
            rows = []
        if rows:
            with st.spinner(_u("检查中（查重 / handle / 集合 / 合规）…", "検査中（重複 / handle / コレクション / コンプラ）…")):
                cands, _ = _cands()
                exp = {c["jan"]: c for c in cands}
                res = V.check_all(rows, exp, existing_jans=_existing_jans(),
                                  existing_handles=SH.handles_taken([V._s(r.get("handle")) for r in rows]),
                                  live_cat_tags=_live_cat_tags())
                try:
                    kwr, ingr, catr = _compliance_rules()
                    for r, rs in zip(rows, res):
                        cj = judge(kwr, ingr, "US", {"title": V._s(r.get("title"))}, V._s(r.get("ingredients")) or None,
                                   category_rules=catr, category_signals={
                                       "brand": V._s(r.get("brand")), "L1": V._s(r.get("l1")), "L2": V._s(r.get("l2")),
                                       "name": V._s(r.get("title"))})
                        for h in cj["hits"]:
                            if h["severity"] in ("red", "yellow"):
                                rs["warns"].append(("合规", f"{'🔴' if h['severity'] == 'red' else '🟡'} "
                                                          f"{h['kind']}·{h['category']}：{h['note']}"))
                except Exception as e:  # noqa: BLE001
                    st.warning(_u("合规规则读取失败，本次未做合规判定：", "コンプラ規則の読込失敗、今回は判定なし：") + str(e)[:200])
            table = pd.DataFrame([{
                "jan": rs["jan"], "title": V._s(r.get("title"))[:70],
                "状态": "❌" if rs["errors"] else ("⚠️" if rs["warns"] else "✅"),
                "问题": "\n".join(f"[{f}] {m}" for f, m in rs["errors"] + rs["warns"]),
                "确认": False} for r, rs in zip(rows, res)])
            n_err = int((table["状态"] == "❌").sum())
            n_warn = int((table["状态"] == "⚠️").sum())
            a, b, c = st.columns(3)
            a.metric(_u("✅ 可建草稿", "✅ 作成可"), f"{len(table) - n_err - n_warn}")
            b.metric(_u("⚠️ 需人工确认", "⚠️ 要確認"), f"{n_warn}")
            c.metric(_u("❌ 有错误（回 Excel 改）", "❌ エラー（Excel で修正）"), f"{n_err}")
            st.caption(_u("❌ 不能建草稿，请让 AI 按「问题」改好再上传；⚠️ 看过没问题就在「确认」打勾。",
                          "❌ は下書き作成不可。「問題」に沿って AI に直させて再アップ。⚠️ は確認済みなら「確認」にチェック。"))
            ed = st.data_editor(
                table, hide_index=True, use_container_width=True, key="up_editor",
                disabled=["jan", "title", "状态", "问题"],
                column_config={"问题": st.column_config.TextColumn(_u("问题", "問題"), width="large"),
                               "确认": st.column_config.CheckboxColumn(_u("确认", "確認")),
                               "状态": _u("状态", "状態")})
            go = [i for i in range(len(rows)) if not res[i]["errors"] and (not res[i]["warns"] or ed["确认"].iloc[i])]
            st.download_button(_u("⬇️ 下载检查结果（CSV）", "⬇️ 検査結果をダウンロード（CSV）"),
                               table.drop(columns=["确认"]).to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"检查结果_{dt.datetime.now():%Y%m%d_%H%M}.csv", mime="text/csv")
            if st.button(_u("🚀 建 Shopify 草稿（{k} 件）", "🚀 Shopify 下書き作成（{k} 件）").format(k=len(go)),
                         type="primary", disabled=not go):
                conn = get_connection()
                C.ensure_ledger(conn)
                batch = f"{dt.datetime.now():%Y%m%d-%H%M%S} {up.name}"
                bar = st.progress(0.0)
                out = []
                for n, i in enumerate(go, 1):
                    r, e = rows[i], exp[V._s(rows[i].get("jan"))]
                    try:
                        cr = SH.create_draft(r, e)
                        bad = SH.verify(r, e, SH.readback(cr["product_id"], cr["variant_id"])) if cr.get("product_id") else []
                        ok = cr["ok"] and not bad
                        detail = cr["error"] or ("; ".join(bad) if bad else "ok")
                    except Exception as ex:  # noqa: BLE001
                        cr, ok, detail = {}, False, f"{type(ex).__name__}: {ex}"
                    C.log(conn, operator=OPERATOR, batch=batch, action="create_draft", jan=V._s(r.get("jan")),
                          product_id=cr.get("product_id") or "", handle=cr.get("handle") or "", ok=ok, detail=detail)
                    out.append({"jan": r.get("jan"), "handle": cr.get("handle"), "ok": "✅" if ok else "❌",
                                "detail": detail})
                    bar.progress(n / len(go))
                _existing_jans.clear()
                okn = sum(1 for o in out if o["ok"] == "✅")
                (st.success if okn == len(out) else st.warning)(
                    _u("建草稿完成：成功 {a} · 失败 {b}（已记入台账）", "下書き作成完了：成功 {a} · 失敗 {b}（履歴に記録）")
                    .format(a=okn, b=len(out) - okn))
                st.dataframe(pd.DataFrame(out), hide_index=True, use_container_width=True)

# ============================================================
# ③ 上传图片
# ============================================================
_IMG_NAME = re.compile(r"^(\d{8}|\d{13})(?:_(\d{1,2}))?\.webp$", re.I)


def _collect(files) -> tuple[dict, list]:
    """上传文件（webp / zip）→ ({jan: [(pos, name, bytes)]}, [不合规文件])。"""
    got, bad = {}, []

    def take(name, data):
        base = name.rsplit("/", 1)[-1]
        if base.startswith(".") or base.startswith("__MACOSX"):
            return
        m = _IMG_NAME.match(base)
        if not m:
            bad.append((base, _u("文件名要是 <JAN>.webp 或 <JAN>_2.webp", "ファイル名は <JAN>.webp か <JAN>_2.webp")))
            return
        pos = int(m.group(2) or 1)
        got.setdefault(m.group(1), []).append((pos, base, data))

    for f in files or []:
        if f.name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(f.getvalue())) as z:
                for n in z.namelist():
                    if not n.endswith("/"):
                        take(n, z.read(n))
        else:
            take(f.name, f.getvalue())
    return got, bad


def _img_check(items: list) -> list[str]:
    from PIL import Image
    errs = []
    poss = [p for p, _, _ in items]
    if len(poss) != len(set(poss)):
        errs.append(_u("同一编号有两张图", "同じ番号の画像が 2 枚"))
    if 1 not in poss:
        errs.append(_u("没有主图（<JAN>.webp）", "メイン画像（<JAN>.webp）がない"))
    for p, n, b in items:
        try:
            im = Image.open(io.BytesIO(b))
            if im.format != "WEBP":
                errs.append(f"{n}: " + _u("不是 WebP", "WebP ではない"))
            elif p == 1 and im.size != (1024, 1024):
                errs.append(f"{n}: " + _u("主图要 1024×1024，现在 {w}×{h}", "メインは 1024×1024、現在 {w}×{h}").format(w=im.size[0], h=im.size[1]))
        except Exception:  # noqa: BLE001
            errs.append(f"{n}: " + _u("打不开（不是图片）", "開けない（画像ではない）"))
        if len(b) > 20 * 1024 * 1024:
            errs.append(f"{n}: " + _u("超过 20MB", "20MB 超"))
    return errs


with tab_img:
    st.caption(_u("文件名：主图 <JAN>.webp，详情图 <JAN>_2.webp、<JAN>_3.webp…（按编号排序）。可多选或打包 zip。"
                  "主图标准：1024×1024、白底、商品占 90%、WebP。按 JAN 找 Shopify 商品（草稿或在售都可）。",
                  "ファイル名：メイン <JAN>.webp、詳細 <JAN>_2.webp、<JAN>_3.webp…（番号順）。複数選択または zip。"
                  "メイン基準：1024×1024・白背景・商品 90%・WebP。JAN で Shopify 商品を検索（下書き・販売中どちらも可）。"))
    imgs = st.file_uploader(_u("上传图片（webp / zip）", "画像をアップロード（webp / zip）"), type=["webp", "zip"],
                            accept_multiple_files=True, key="up_img")
    if imgs and _shopify_ready():
        got, badf = _collect(imgs)
        for n, why in badf:
            st.error(f"{n}：{why}")
        if got:
            prods = SH.products_by_jan(list(got))
            recs = []
            for jan, items in sorted(got.items()):
                items.sort()
                p = prods.get(jan)
                errs = _img_check(items) + ([] if p else [_u("Shopify 找不到这个 JAN 的商品", "Shopify にこの JAN の商品がない")])
                n_old = len(p["media"]) if p else 0
                recs.append({"jan": jan, "商品": (p or {}).get("title", "")[:60], "状态": (p or {}).get("status", ""),
                             "现有图": n_old, "新图": len(items), "检查": "；".join(errs) or "✅",
                             "操作": (_u("不处理", "何もしない") if errs else
                                    (_u("追加", "追加") if n_old == 0 else _u("不处理", "何もしない")))})
            st.caption(_u("已有图片的商品默认「不处理」：请逐件选「追加」（加在现有图后面）或「替换」（新图挂好后删掉旧图）。",
                          "画像がある商品は既定「何もしない」：1 件ずつ「追加」（既存の後ろに追加）か「差し替え」（新画像掲載後に旧画像を削除）を選択。"))
            opts = [_u("不处理", "何もしない"), _u("追加", "追加"), _u("替换", "差し替え")]
            ied = st.data_editor(pd.DataFrame(recs), hide_index=True, use_container_width=True, key="img_editor",
                                 disabled=["jan", "商品", "状态", "现有图", "新图", "检查"],
                                 column_config={"操作": st.column_config.SelectboxColumn(_u("操作", "操作"), options=opts)})
            todo = [r for _, r in ied.iterrows() if r["操作"] != opts[0] and r["检查"] == "✅"]
            if st.button(_u("🖼️ 挂图（{k} 件）", "🖼️ 画像を掲載（{k} 件）").format(k=len(todo)), type="primary", disabled=not todo):
                conn = get_connection()
                C.ensure_ledger(conn)
                batch = f"{dt.datetime.now():%Y%m%d-%H%M%S} images"
                out = []
                bar = st.progress(0.0)
                for n, r in enumerate(todo, 1):
                    p = prods[r["jan"]]
                    mode = "replace" if r["操作"] == opts[2] else "append"
                    files = [(nm, b) for _, nm, b in sorted(got[r["jan"]])]
                    try:
                        res = SH.attach_images(p["id"], p["title"], files, mode, [m["id"] for m in p["media"]])
                    except Exception as ex:  # noqa: BLE001
                        res = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
                    C.log(conn, operator=OPERATOR, batch=batch, action=f"images_{mode}", jan=r["jan"], product_id=p["id"],
                          handle=p["handle"], ok=res["ok"], detail=res["error"] or f"{len(files)} 张")
                    out.append({"jan": r["jan"], "mode": mode, "ok": "✅" if res["ok"] else "❌", "detail": res["error"] or f"{len(files)}"})
                    bar.progress(n / len(todo))
                okn = sum(1 for o in out if o["ok"] == "✅")
                (st.success if okn == len(out) else st.warning)(
                    _u("挂图完成：成功 {a} · 失败 {b}（已记入台账）", "掲載完了：成功 {a} · 失敗 {b}（履歴に記録）")
                    .format(a=okn, b=len(out) - okn))
                st.dataframe(pd.DataFrame(out), hide_index=True, use_container_width=True)

# ============================================================
# 台账
# ============================================================
with tab_log:
    try:
        conn = get_connection()
        C.ensure_ledger(conn)
        lg = pd.DataFrame(C.recent(conn))
        if lg.empty:
            st.info(_u("还没有记录。", "記録はまだありません。"))
        else:
            st.dataframe(lg, hide_index=True, use_container_width=True)
    except Exception as e:  # noqa: BLE001
        st.error(str(e)[:300])
