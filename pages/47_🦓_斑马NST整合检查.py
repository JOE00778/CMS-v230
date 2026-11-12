"""模块 #47 · 🦓 斑马・NST 整合检查（Boss 2026-11-12「做成一个模块，不放在商品登录里，单独做为一个独立的板块」）

page15 の「🔎 SKUID 空チェック」tab をそのまま独立させたもの（条件・既定値・書き戻しの方法は同じ）:
  🔎 SKUID 空      NST で SKUID（custitem_item_skuid＝斑马 SKUID・在庫同期用）が空の品 → 斑马の同じ型番 / JAN の SKUID、
                   無ければ斑马に登録して取る → NST 更新 CSV（型番 + SKUID・人が NST に取り込む）。shared/skuid_fill
  🩺 斑马登録内容  SKUID がある品の斑马 SKU で 三辺・重量・画像・英文名・HS の空 → CMS で直して斑马へ書き戻す。shared/banma_fix
                   （報関 = SKU 単位 API / 三辺・重量・画像 = SPU 丸ごと PUT・描述/包材ありの SPU は書かない・台帳 banma.cms_write_log）
条件（すべて選択可・既定 = 直近 30 日 / 輸出 2 部門 / 取扱中 / 有効 / 全ランク / 型番+JAN / 斑马 API / 重複は出さない / 仮値も空）。
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import streamlit as st

from data_warehouse.templates import nst_item_master as TPL
from shared import banma_client, hs_ai, hs_classify, image_search, nst_suiteql
from shared import banma_fix as BF
from shared import banma_register as BMR
from shared import item_register as REG
from shared import nst_choices as NC
from shared import skuid_fill as SF
from shared.auth import require_password
from shared.db import get_connection
from shared.i18n import lang_selector, t
from shared.theme import inject_theme

st.set_page_config(page_title=t("斑马・NST 整合检查"), page_icon="🦓", layout="wide")
require_password()
inject_theme()
lang_selector()

st.title(t("🦓 斑马・NST 整合检查"))

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_prefix_makers() -> dict[str, str]:
    with get_connection() as conn:
        return NC.load_prefix_makers(conn)


# 斑马の報関照会・画像検索は 1 件ずつだと数百件で数十分（2026-11-12 Boss「加速跑」）→ 同時 4 本。
# 斑马は名目 5 QPS・0.25s 間隔で 429（memory 2026-10-07）→ 1 本 1〜2 秒なら 4 本で 2〜4 QPS。429 は client が待って再試行
_WORKERS = 4


def _parallel(fn, keys: list[str], text: str) -> dict:
    """keys を fn で同時 _WORKERS 本 → {key: 結果 or 例外}。進捗バーはメインスレッドだけで触る。"""
    out: dict = {}
    if not keys:
        return out
    bar = st.progress(0.0, text=text)
    with ThreadPoolExecutor(_WORKERS) as ex:
        futs = {ex.submit(fn, k): k for k in keys}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                out[futs[f]] = f.result()
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない（呼び出し側が件数を出す）
                out[futs[f]] = e
            bar.progress(i / len(keys), text=f"{text} {i}/{len(keys)}")
    bar.empty()
    return out


def _issues_df(issues) -> pd.DataFrame:
    return pd.DataFrame([{t("行"): i.row or "", "型番 / JAN": i.jan,
                          t("区分"): "❌ error" if i.level == "error" else "⚠️ warn",
                          t("内容"): i.message} for i in issues])

# NST で SKUID（斑马 SKUID・在庫同期用）が空の品 → 斑马の同じ型番 / JAN の SKUID、無ければ斑马に登録して取る
# → NST 更新 CSV（型番 + SKUID）。NST へは書かない（CSV は人が取り込む）。Boss 2026-11-12

_SK_KEYS = ["p47s_result", "p47s_prep", "p47s_reg", "p47s_editor"]
_SK_SOURCES = {"api": "斑马 API（実時間・遅いと 50 件で 1 分ほど）", "pg": "PG の写し（毎日 04:00 時点・速い）"}
_SK_PKG = ["パッケージ重量(g)", "パッケージ高さ(cm)", "パッケージ幅(cm)", "パッケージ奥行(cm)"]


def _skuid_check(opts: dict) -> dict:
    """NST の SKUID 空 → 条件で絞る → 斑马に照会 → (hits, conflicts, misses)。照会の失敗は例外のまま（全件「無し」にしない）。"""
    with st.spinner(t("NST で SKUID が空の品を取得中…")):
        items = SF.blank_items(nst_suiteql.suiteql_all, days=SF.PERIODS[opts["period"]])
    picked = SF.pick(items, depts=opts["depts"], handlings=opts["handlings"], states=opts["states"],
                     ranks=opts["ranks"])
    keys = SF.lookup_keys(picked, by_jan=opts["by_jan"])
    bm: dict[str, str] = {}
    if keys:
        with st.spinner(t(f"斑马に {len(keys)} コードを照会中（{_SK_SOURCES[opts['source']]}）…")):
            if opts["source"] == "api":
                client = banma_client.BanmaClient.from_env()
                with get_connection() as conn:
                    banma_client.ensure_token(conn, client)
                bm = BMR.find_skus(client, keys)
            else:
                with get_connection() as conn:
                    bm = SF.mirror_skus(conn, keys)
    found = sorted({v for v in bm.values() if v})
    with st.spinner(t("同じ SKUID を持つ NST の品を確認中…")):
        in_use = SF.skuids_in_use(nst_suiteql.suiteql_all, found) if found else {}
    hits, conflicts, misses = SF.match(picked, bm, by_jan=opts["by_jan"], in_use=in_use)
    return {"opts": opts, "total": len(items), "picked": len(picked), "hits": hits, "conflicts": conflicts,
            "misses": misses}


def _skuid_prepare(misses: list[dict]) -> dict:
    """斑马に無い品 → 登録用の行（台帳 → NST の順）。HS は規則 + AI、画像は jancode → 画像検索 → 待上图（page15 と同じ）。"""
    with get_connection() as conn:
        ledger = SF.load_ledger(conn, [m["code"] for m in misses])
    try:
        prefix = _cached_prefix_makers()
    except Exception:  # noqa: BLE001 — HS の規則判定がブランド推定なしになるだけ
        prefix = {}
    rows, extra = SF.register_rows(
        misses, ledger, lambda n, m, j: hs_classify.classify_customs(n, m, j, prefix_map=prefix))
    stats: dict = {}
    issues = []
    need_img = [r for r in rows if not extra[REG.item_key(r)].get(REG.COL_IMAGE)]
    if need_img:
        with st.spinner(t(f"画像を探しています（{len(need_img)} 件）…")):
            stats["images"] = REG.fill_images(need_img, extra, image_search.search_jan)
    # AI 突合 = NST の値で作った品 + 台帳でも HS が空の品（台帳の HS は人が確認済みなので触らない）
    need_ai = [r for r in rows if extra[REG.item_key(r)]["source"] == "NST" or not extra[REG.item_key(r)].get(REG.COL_HS)]
    if need_ai and hs_ai.is_configured():
        with st.spinner(t(f"HS を AI でも判定中（{len(need_ai)} 件）…")):
            issues = REG.cross_check_hs(need_ai, extra, stats, hs_ai.judge)
    n_nst = sum(e["source"] == "NST" for e in extra.values())
    return {"rows": rows, "extra": extra, "issues": issues, "stats": stats,
            "n_ledger": len(rows) - n_nst, "n_nst": n_nst}


def _skuid_frame(prep: dict) -> pd.DataFrame:
    return pd.DataFrame([{
        "型番": REG.item_key(r), t("出典"): prep["extra"][REG.item_key(r)]["source"], "JANコード": r.get("JANコード", ""),
        "アイテム名": r.get("アイテム名", ""), "商品原価": r.get("商品原価", ""), **{c: r.get(c, "") for c in _SK_PKG},
        "HS": prep["extra"][REG.item_key(r)].get(REG.COL_HS, ""),
        "通関英文名": prep["extra"][REG.item_key(r)].get(REG.COL_NAME_EN, ""),
        "画像URL": prep["extra"][REG.item_key(r)].get(REG.COL_IMAGE, ""),
        t("HS 判定"): prep["extra"][REG.item_key(r)].get("hs_check", "")} for r in prep["rows"]])


def _skuid_register(rows: list[dict]) -> None:
    try:
        client = banma_client.BanmaClient.from_env()
        with get_connection() as conn:
            banma_client.ensure_token(conn, client)
    except Exception as e:  # noqa: BLE001
        st.session_state["p47s_reg"] = {"error": f"斑马に接続できません（登録していません）: {type(e).__name__}: {e}"}
        return
    bar = st.progress(0.0, text=t("斑马に登録中…"))
    with get_connection() as conn:   # 台帳に行がある品（page15 で登録失敗した品など）はその行に結果を書く
        results = BMR.register_all(client, rows, record=lambda code, r: REG.record_banma(conn, code, r),
                                   on_progress=lambda d, n, c: bar.progress(d / n, text=f"{d}/{n} {c}"))
    bar.empty()
    st.session_state["p47s_reg"] = {"rows": results}


# ── 🩺 斑马登録内容チェック（Boss 2026-11-12「三边・重量・清关英文・HScode・图片 为空的也检查，CMS 上修改后回传到斑马」）
# 書き戻しは shared/banma_fix（報関 = SKU 単位 API / 三辺・重量・画像 = SPU 丸ごと PUT・描述/包材ありの SPU は書かない）

_CC_KEYS = ["p47c_result", "p47c_reg", "p47c_editor"]
_CC_COLS = {"重量(g)": "Weight", "長(cm)": "Length", "幅(cm)": "Width", "高(cm)": "Height", "画像URL": "Image",
            "HS": "Code", "英文名": "NameEn"}
_CC_INT = ("Weight", "Length", "Width", "Height")
_JDL_SQL = ("SELECT jan, wms_gross_weight_g, wms_length_mm, wms_width_mm, wms_height_mm FROM jdl.v_goods_dimensions "
            "WHERE jan IN ({})")


def _cc_suggest(item: dict, jdl: dict | None) -> dict:
    """重量・三辺の提案（page46 と同じ: 重量 JDL 実測 > NST 包装重量・倍率と異常値の閘門 / 三辺 JDL 実測 > NST 包装寸法）。"""
    from shared.banma_dims import _single_weight, to_cm
    out = {}
    w, src, _ = _single_weight((jdl or {}).get("wms_gross_weight_g"), item.get("pkg_weight"))
    if w:
        out["Weight"] = (w, src)
    d = to_cm(tuple((jdl or {}).get(k) for k in ("wms_length_mm", "wms_width_mm", "wms_height_mm")))
    if d:
        out["dims"] = (d, "JDL")
    else:
        try:
            n = tuple(sorted((math.ceil(float(item.get(k) or 0)) for k in ("pkg_height", "pkg_width", "pkg_depth")),
                             reverse=True))
        except ValueError:
            n = ()
        if n and all(x > 0 for x in n) and n not in BF.D_PH:
            out["dims"] = (n, "NST")
    return out


def _content_check(opts: dict, checks: tuple, placeholder: bool) -> dict:
    """NST で SKUID がある品（条件で絞る）→ 斑马 SKU（型番 / JAN）→ 報関 → 空の品だけ・提案を入れた行。"""
    with st.spinner(t("NST で SKUID がある品を取得中…")):
        items = SF.blank_items(nst_suiteql.suiteql_all, days=SF.PERIODS[opts["period"]], has_skuid=True)
    picked = SF.pick(items, depts=opts["depts"], handlings=opts["handlings"], states=opts["states"],
                     ranks=opts["ranks"])
    client = banma_client.BanmaClient.from_env()
    with get_connection() as conn:
        banma_client.ensure_token(conn, client)
    keys = SF.lookup_keys(picked, by_jan=opts["by_jan"])
    with st.spinner(t(f"斑马の SKU を照会中（{len(keys)} コード）…")):
        skus = BMR.get_skus(client, keys) if keys else {}
    found, not_found, combos = [], [], 0
    for r in picked:
        s = skus.get(str(r.get("code") or "")) or (opts["by_jan"] and skus.get(str(r.get("jan") or ""))) or None
        if not s:
            not_found.append(r["code"])
        elif s.get("Type") == "组合":
            combos += 1          # 組合品の重量・三辺は page46 の規則（単品 × 件数）で扱う
        else:
            found.append((r, s))
    customs: dict = {}
    cus_err: dict[str, str] = {}
    need = [str(s["SKUID"]) for _, s in found] if {"英文名", "HS"} & set(checks) else []
    if need and opts["source"] == "pg":
        with get_connection() as conn:
            customs = BF.mirror_customs(conn, need)
    rest = [x for x in need if x not in customs]
    for sid, v in _parallel(lambda sid: BF.get_customs(client, sid), rest, t("斑马の報関情報を照会中…")).items():
        if isinstance(v, Exception):   # その品は報関を判定しない（件数は画面に出す）
            cus_err[sid] = f"{type(v).__name__}: {v}"
        else:
            customs[sid] = v
    rows = []
    for r, s in found:
        sid = str(s["SKUID"])
        chk = tuple(c for c in checks if not (sid in cus_err and c in ("英文名", "HS")))
        probs = BF.problems(s, customs.get(sid), checks=chk, placeholder=placeholder)
        if probs:
            c = customs.get(sid) or {}
            rows.append({"item": r, "sku_id": sid, "sku_code": s.get("Code") or r["code"], "probs": probs,
                         "cur": {**{k: s.get(k) for k in BF.PRODUCT_FIELDS}, "Code": c.get("Code") or "",
                                 "NameEn": c.get("NameEn") or ""},
                         "note": ("NST の SKUID " + str(r.get("sku_id")) + " ≠ 斑马 " + sid) if str(r.get("sku_id")) != sid
                         else ("報関の照会に失敗: " + cus_err[sid]) if sid in cus_err else ""})
    if rows:
        _cc_fill_suggestions(rows)
        with get_connection() as conn:
            spu = BF.spu_ids(conn, [x["sku_id"] for x in rows])
        for x in rows:
            x["spu_id"] = spu.get(x["sku_id"], "")
    return {"key": (opts, checks, placeholder), "total": len(items), "picked": len(picked), "found": len(found),
            "not_found": not_found, "combos": combos, "cus_err": len(cus_err), "rows": rows}


def _cc_fill_suggestions(rows: list[dict]) -> None:
    """空の項目に提案を入れる（重量・三辺 = JDL > NST / 画像 = 画像検索 / HS・英文名 = 規則 + AI）。new = 書く候補。"""
    jans = [str(x["item"].get("jan") or "") for x in rows if {"重量", "三辺"} & set(x["probs"])]
    jdl = {}
    if jans:
        with get_connection() as conn:
            jdl = {r["jan"]: dict(r) for r in conn.execute(_JDL_SQL.format(",".join("?" for _ in jans)),
                                                           tuple(jans)).fetchall()}
    try:
        prefix = _cached_prefix_makers()
    except Exception:  # noqa: BLE001
        prefix = {}
    img_jans = sorted({str(x["item"]["jan"]) for x in rows if "画像" in x["probs"] and x["item"].get("jan")})
    images = _parallel(image_search.search_jan, img_jans, t("画像を検索中…"))
    ai_rows, extra = [], {}
    for x in rows:
        it, new, src = x["item"], dict(x["cur"]), []
        sug = _cc_suggest(it, jdl.get(str(it.get("jan") or "")))
        if "重量" in x["probs"] and "Weight" in sug:
            new["Weight"] = sug["Weight"][0]
            src.append(f"重量={sug['Weight'][1]}")
        if "三辺" in x["probs"] and "dims" in sug:
            new.update(zip(("Length", "Width", "Height"), sug["dims"][0]))
            src.append(f"三辺={sug['dims'][1]}")
        if "画像" in x["probs"] and it.get("jan"):
            url = images.get(str(it["jan"]))
            if isinstance(url, str) and url.startswith("https://"):
                new["Image"] = url
                src.append("画像=画像検索")
        if {"英文名", "HS"} & set(x["probs"]) and it.get("name"):
            cus = hs_classify.classify_customs(str(it["name"]), str(it.get("maker") or ""), str(it.get("jan") or ""),
                                               prefix_map=prefix)
            code = str(it["code"])
            extra[code] = {REG.COL_HS: cus.hs or "", REG.COL_NAME_EN: cus.name_en or ""}
            ai_rows.append(({"型番": code, "アイテム名": str(it["name"]), "メーカー名": str(it.get("maker") or "")}, x))
        x["new"], x["src"] = new, src
    if ai_rows and hs_ai.is_configured():
        with st.spinner(t(f"HS を AI でも判定中（{len(ai_rows)} 件）…")):
            REG.cross_check_hs([r for r, _ in ai_rows], extra, {}, hs_ai.judge)
    for r, x in ai_rows:
        ex = extra[r["型番"]]
        if "HS" in x["probs"] and ex.get(REG.COL_HS):
            x["new"]["Code"] = ex[REG.COL_HS]
            x["src"].append("HS=" + ("AI" if ex.get("hs_basis") == "ai" else "規則"))
        if "英文名" in x["probs"] and ex.get(REG.COL_NAME_EN):
            x["new"]["NameEn"] = ex[REG.COL_NAME_EN]
            x["src"].append("英文名=" + ("AI" if ex.get("hs_basis") == "ai" else "規則"))


def _cc_value(col: str, v):
    """表の値 → 斑马に送る値。(値, エラー)。空 = 変えない。"""
    s = "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v).strip()
    if not s:
        return None, ""
    if col in _CC_INT:
        try:
            n = math.ceil(float(s))
        except ValueError:
            return None, f"{col} が数値ではありません「{s}」"
        return (n, "") if n > 0 else (None, f"{col} は 1 以上")
    if col == "Image" and not s.startswith("https://"):
        return None, "画像URL は https:// で始まる URL"
    if col == "Code" and not BF._HS.fullmatch(s):
        return None, f"HS は 6/8/10 桁の数字「{s}」"
    return s, ""


def _cc_changes(rows: list[dict], recs: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """表 → 各行の変更（product / customs）と入力エラー。現値と同じ値は送らない。"""
    out, errs = [], []
    for x, rec in zip(rows, recs):
        prod, cus = {}, {}
        for col, key in _CC_COLS.items():
            v, err = _cc_value(key, rec.get(col))
            if err:
                errs.append((str(x["item"]["code"]), err))
                continue
            cur = x["cur"].get(key)
            if v is None or str(v) == str(cur if cur is not None else "").strip():
                continue
            (cus if key in ("Code", "NameEn") else prod)[key] = v
        if prod or cus:
            out.append({**x, "prod": prod, "cus": cus})
    return out, errs


def _cc_write(changes: list[dict]) -> list[dict]:
    """斑马へ書く。SPU ごと（描述/包材ありは skip → Excel）→ mismatch が出たら以降の SPU は止める。報関は SKU ごと。"""
    client = banma_client.BanmaClient.from_env()
    out = []
    by_spu: dict[str, list[dict]] = {}
    for c in changes:
        if c["prod"]:
            if c["spu_id"]:
                by_spu.setdefault(c["spu_id"], []).append(c)
            else:
                out.append({"型番": c["item"]["code"], "対象": "三辺・重量・画像", "結果": "skipped", "excel": c,
                            "内容": "SPUID 不明（斑马の写しは毎日 04:00 更新・翌日もう一度）→ 三辺・重量は Excel"})
    n = len(by_spu) + sum(bool(c["cus"]) for c in changes)
    bar = st.progress(0.0, text=t("斑马に書込中…"))
    done, stop = 0, ""
    with get_connection() as conn:
        banma_client.ensure_token(conn, client)
        for spu, cs in by_spu.items():
            codes = ",".join(str(c["item"]["code"]) for c in cs)
            if stop:
                r = {"status": "skipped", "message": f"中止（{stop} で回読が一致しなかったため）"}
            else:
                r = BF.write_product(client, conn, spu, {c["sku_id"]: c["prod"] for c in cs}, codes)
                if r["status"] == "mismatch":
                    stop = f"SPU {spu}"
            for c in cs:
                out.append({"型番": c["item"]["code"], "対象": "三辺・重量・画像", "結果": r["status"],
                            "内容": r.get("message", ""), "excel": c if r["status"] == "skipped" else None})
            done += 1
            bar.progress(done / n, text=f"{done}/{n} SPU {spu}")
        for c in changes:
            if not c["cus"]:
                continue
            r = BF.write_customs(client, conn, c["sku_id"], str(c["item"]["code"]), c["cus"],
                                 name_cn=str(c["item"].get("name") or ""),
                                 weight=c["prod"].get("Weight") or c["cur"].get("Weight") or 0)
            out.append({"型番": c["item"]["code"], "対象": "報関", "結果": r["status"], "内容": r.get("message", "")})
            done += 1
            bar.progress(done / n, text=f"{done}/{n} {c['item']['code']}")
    bar.empty()
    return out


def _render_content_result(res: dict) -> None:
    rows = res["rows"]
    cnt = {p: sum(p in x["probs"] for x in rows) for p in BF.CHECKS}
    st.info(t(f"SKUID あり（{res['key'][0]['period']}・部門 輸出）{res['total']} 件 → 条件に合う {res['picked']} 件 → "
              f"斑马で照合 {res['found']} 件（見つからない {len(res['not_found'])} · 組合せ品は対象外 {res['combos']}）→ "
              f"空あり {len(rows)} 件（" + " · ".join(f"{k} {v}" for k, v in cnt.items() if k in res["key"][1]) + "）"
              + (f" · 報関の照会失敗 {res['cus_err']} 件（その品は報関を判定していません）" if res["cus_err"] else "")))
    if res["not_found"]:
        with st.expander(t(f"斑马で見つからない {len(res['not_found'])} 件（型番 / JAN）")):
            st.write("・".join(map(str, res["not_found"])))
    if not rows:
        st.success(t("空の項目はありません"))
        return
    st.caption(t("白い列を直して「🦓 斑马に反映」。提案の列 = 自動で入れた値の出典（重量・三辺 = JDL 実測 > NST 包装 / 画像 = 画像検索 / "
                 "HS・英文名 = 規則 + AI）。空欄・現値と同じ値は送りません。三辺・重量・画像は SPU 丸ごとの書込のため、"
                 "描述・包材がある SPU には書かず Excel（三辺・重量）か斑马の画面（画像）に回します"))
    df = pd.DataFrame([{"型番": x["item"]["code"], t("アイテム名"): x["item"].get("name") or "",
                        t("空の項目"): "・".join(x["probs"]),
                        **{col: ("" if x["new"].get(key) is None else str(x["new"].get(key))) for col, key in _CC_COLS.items()},
                        t("提案"): " · ".join(x["src"]), t("備考"): x["note"]} for x in rows])
    edited = st.data_editor(df, key="p47c_editor", hide_index=True, num_rows="fixed", use_container_width=True,
                            disabled=["型番", t("アイテム名"), t("空の項目"), t("提案"), t("備考")],
                            column_config={"英文名": st.column_config.TextColumn(max_chars=hs_classify.MAX_NAME_EN),
                                           "画像URL": st.column_config.LinkColumn("画像URL")})
    changes, errs = _cc_changes(rows, edited.to_dict("records"))
    for code, e in errs[:20]:
        st.error(f"❌ {code}: {e}")
    n_p = sum(bool(c["prod"]) for c in changes)
    n_c = sum(bool(c["cus"]) for c in changes)
    if changes and not errs and st.button(t(f"🦓 斑马に反映（三辺・重量・画像 {n_p} 件 · 報関 {n_c} 件）"), type="primary",
                                          key="p47c_write"):
        st.session_state["p47c_reg"] = _cc_write(changes)
    reg = st.session_state.get("p47c_reg")
    if reg:
        c = {k: sum(r["結果"] == k for r in reg) for k in ("ok", "created", "skipped", "fail", "mismatch")}
        msg = (f"ok={c['ok'] + c['created']}（報関の新規作成 {c['created']}）skipped={c['skipped']} fail={c['fail']} "
               f"回読不一致={c['mismatch']}（計 {len(reg)} 件）")
        (st.error if c["fail"] or c["mismatch"] else st.warning if c["skipped"] else st.success)(msg)
        if c["mismatch"]:
            st.error(t("❌ 回読が一致しない SPU があったため、以降の SPU は書いていません。斑马で該当 SPU を確認してください"
                       "（書く前の値は台帳 banma.cms_write_log にあります）"))
        st.dataframe(pd.DataFrame([{k: v for k, v in r.items() if k != "excel"} for r in reg]),
                     use_container_width=True, hide_index=True)
        xl = [{"SKU编码": r["excel"]["sku_code"],
               **{h: r["excel"]["prod"].get(k) for h, k in (("重量(g)", "Weight"), ("长(cm)", "Length"),
                                                         ("宽(cm)", "Width"), ("高(cm)", "Height"))}}
              for r in reg if r.get("excel") and {"Weight", "Length", "Width", "Height"} & set(r["excel"]["prod"])]
        if xl:
            st.download_button(t(f"⬇️ 斑马「批量修改SKU信息」Excel（API で書かなかった三辺・重量 {len(xl)} 件）"),
                               data=BF.dims_xlsx(xl), file_name=f"斑马_批量修改SKU信息_{datetime.now():%Y%m%d}.xlsx",
                               mime=_XLSX_MIME, key="p47c_xlsx")
        if any(r.get("excel") and "Image" in r["excel"]["prod"] for r in reg):
            st.info(t("画像を API で書かなかった品は斑马の画面で入れてください（上の表の skipped）"))


def _render_skuid_tab() -> None:
    """⚠️ st.stop() 禁止（他タブが消える）。抜けるときは return。"""
    st.caption(t("🔎 NST で SKUID（斑马 SKUID・在庫同期用）が空の品を探し、斑马の同じ型番 / JAN の SKUID を取る。"
                 "斑马に無ければ斑马に登録してから取る → NST 更新 CSV（型番 + SKUID）を手で NST に取り込む。"
                 "🩺 SKUID がある品は斑马の三辺・重量・画像・HS・英文名の空を探し、CMS で直して斑马へ書き戻す"))
    d = SF.DEFAULTS
    c1, c2, c3 = st.columns(3)
    period = c1.selectbox(t("作成日"), list(SF.PERIODS), index=list(SF.PERIODS).index(d["period"]), key="p47s_period")
    depts = c2.multiselect(t("部門"), SF.DEPTS, default=list(d["depts"]), key="p47s_depts")
    handlings = c3.multiselect(t("取扱区分"), SF.HANDLINGS, default=list(d["handlings"]), key="p47s_handlings")
    c4, c5, c6 = st.columns(3)
    states = c4.multiselect(t("状態"), SF.STATES, default=list(d["states"]), key="p47s_states")
    ranks = c5.multiselect(t("商品ランク"), SF.RANKS, default=list(d["ranks"]), key="p47s_ranks")
    by_jan = c6.radio(t("斑马での照合"), [True, False], index=0, key="p47s_by_jan", horizontal=True,
                      format_func=lambda v: t("型番 + JAN") if v else t("型番のみ"),
                      help=t("型番で見つからなければ JAN（斑马の単品 SKU コード = JAN）でも探す"))
    c7, c8 = st.columns(2)
    source = c7.radio(t("斑马の照会元"), list(_SK_SOURCES), index=0, key="p47s_source",
                      format_func=lambda k: t(_SK_SOURCES[k]))
    allow_dup = c8.checkbox(t("同じ斑马 SKUID を複数の品に入れる"), value=d["allow_dup"], key="p47s_allow_dup",
                            help=t("既定は入れない（1 つの斑马 SKU に NST の 2 品をつなぐと在庫同期が崩れる）。"
                                   "入れると「重複で保留」の品も CSV に出します"))
    opts = {"period": period, "depts": tuple(depts), "handlings": tuple(handlings), "states": tuple(states),
            "ranks": tuple(ranks), "by_jan": by_jan, "source": source}
    c9, c10 = st.columns(2)
    checks = c9.multiselect(t("🩺 斑马の検査項目"), BF.CHECKS, default=list(BF.CHECKS), key="p47c_checks")
    placeholder = c10.checkbox(t("仮値も空とみなす（重量 0/1/10/100/200/300/500g・三辺 1×1×1/10×10×10・待上图の画像）"),
                               value=True, key="p47c_placeholder", help=t("page46 と同じ口径（Boss 2026-10-07〜09）"))
    b1, b2, b3 = st.columns(3)
    if b1.button(t("🔍 SKUID 空をチェックする"), type="primary", key="p47s_run", use_container_width=True):
        for k in _SK_KEYS:
            st.session_state.pop(k, None)
        try:
            st.session_state["p47s_result"] = _skuid_check(opts)
        except Exception as e:  # noqa: BLE001 — 照会失敗は「斑马に無し」にしない
            st.error(t(f"❌ チェックに失敗（何も出力していません）: {type(e).__name__}: {e}"))
    if b2.button(t("🩺 斑马の登録内容をチェックする"), type="primary", key="p47c_run", use_container_width=True):
        for k in _CC_KEYS:
            st.session_state.pop(k, None)
        try:
            st.session_state["p47c_result"] = _content_check(opts, tuple(checks), placeholder)
        except Exception as e:  # noqa: BLE001
            st.error(t(f"❌ チェックに失敗（何も書いていません）: {type(e).__name__}: {e}"))
    if b3.button(t("🗑️ 清除结果"), key="p47s_clear", use_container_width=True):
        for k in _SK_KEYS + _CC_KEYS:
            st.session_state.pop(k, None)
        st.rerun()
    res, cres = st.session_state.get("p47s_result"), st.session_state.get("p47c_result")
    if not res and not cres:
        st.info(t("条件を選んで「🔍 SKUID 空をチェックする」か「🩺 斑马の登録内容をチェックする」"))
    if res:
        st.subheader(t("🔎 SKUID 空"))
        if res["opts"] != opts:
            st.warning(t("条件が変わりました。「🔍 SKUID 空をチェックする」を押してください（前回の結果は隠しています）"))
        else:
            _render_skuid_result(res, allow_dup)
    if cres:
        st.subheader(t("🩺 斑马の登録内容"))
        if cres["key"] != (opts, tuple(checks), placeholder):
            st.warning(t("条件が変わりました。「🩺 斑马の登録内容をチェックする」を押してください（前回の結果は隠しています）"))
        else:
            _render_content_result(cres)


def _render_skuid_result(res: dict, allow_dup: bool) -> None:
    hits, conflicts, misses = res["hits"], res["conflicts"], res["misses"]
    n_code = sum(h["key"] == "型番" for h in hits + conflicts)
    st.info(t(f"SKUID 空（{res['opts']['period']}・部門 輸出）{res['total']} 件 → 条件に合う {res['picked']} 件 → "
              f"斑马に有り {len(hits) + len(conflicts)} 件（型番一致 {n_code} · JAN 一致 {len(hits) + len(conflicts) - n_code}）"
              f"／うち同じ SKUID の重複 {len(conflicts)} 件 · 斑马に無し {len(misses)} 件"))
    if hits:
        st.subheader(t(f"✅ 斑马に有り（{len(hits)} 件・CSV に出す）"))
        st.dataframe(pd.DataFrame([{"型番": h["code"], "JANコード": h["jan"], t("アイテム名"): h["name"],
                                    t("照合"): h["key"], "斑马 SKUID": h["sku_id"]} for h in hits]),
                     use_container_width=True, hide_index=True)
    if conflicts:
        st.subheader(t(f"⚠️ 同じ斑马 SKUID の重複（{len(conflicts)} 件・"
                       + ("CSV に出す" if allow_dup else "CSV に出さない") + "）"))
        st.dataframe(pd.DataFrame([{"型番": c["code"], "JANコード": c["jan"], t("アイテム名"): c["name"],
                                    "斑马 SKUID": c["sku_id"], t("内容"): c["note"]} for c in conflicts]),
                     use_container_width=True, hide_index=True)

    reg = st.session_state.get("p47s_reg") or {}
    if misses:
        st.subheader(t(f"🦓 斑马に無し（{len(misses)} 件）→ 斑马に登録してから SKUID を取る"))
        prep = st.session_state.get("p47s_prep")
        if not prep:
            st.dataframe(pd.DataFrame([{"型番": m["code"], "JANコード": m["jan"], t("アイテム名"): m["name"],
                                        t("作成日"): m.get("created"), t("商品ランク"): m.get("item_rank") or ""}
                                       for m in misses]), use_container_width=True, hide_index=True)
            if st.button(t(f"📝 登録の準備（{len(misses)} 件・page15 の台帳 → NST の値・HS と画像を自動で埋める）"),
                         key="p47s_prep_btn"):
                try:
                    st.session_state["p47s_prep"] = _skuid_prepare(misses)
                except Exception as e:  # noqa: BLE001
                    st.error(t(f"❌ 準備に失敗: {type(e).__name__}: {e}"))
                    return
                st.rerun()
        else:
            st.caption(t(f"出典: page15 の台帳 {prep['n_ledger']} 件（登録時に人が確認した値）· NST {prep['n_nst']} 件"
                         + (" · 画像 " + " ".join(f"{k}={v}" for k, v in prep["stats"]["images"].items())
                            if prep["stats"].get("images") else "")))
            if prep["issues"]:
                with st.expander(t(f"⚠️ HS の要確認 {len(prep['issues'])} 件")):
                    st.dataframe(_issues_df(prep["issues"]), use_container_width=True, hide_index=True)
            df = _skuid_frame(prep)
            edited = st.data_editor(
                df, key="p47s_editor", hide_index=True, num_rows="fixed", use_container_width=True,
                disabled=["型番", t("出典"), "JANコード", t("HS 判定")],
                column_config={"アイテム名": st.column_config.TextColumn(max_chars=REG.NAME_MAX),
                               "通関英文名": st.column_config.TextColumn(max_chars=hs_classify.MAX_NAME_EN)})
            recs = edited.to_dict("records")
            rows = [{"型番": r["型番"], "アイテム名": str(r["アイテム名"] or "").strip(), "商品原価": r["商品原価"],
                     **{c: r[c] for c in _SK_PKG}, REG.COL_HS: str(r["HS"] or "").strip(),
                     REG.COL_NAME_EN: str(r["通関英文名"] or "").strip(), REG.COL_IMAGE: str(r["画像URL"] or "").strip()}
                    for r in recs]
            no_name = [r["型番"] for r in rows if not r["アイテム名"]]
            no_cus = sum(not r[REG.COL_HS] or not r[REG.COL_NAME_EN] for r in rows)
            no_wt = sum(not str(r["パッケージ重量(g)"] or "").strip() for r in rows)
            if no_wt:
                st.warning(t(f"⚠️ パッケージ重量(g) が空の {no_wt} 件は 1 g で登録されます（分かれば表に入れてください）"))
            if no_name:
                st.error(t(f"❌ アイテム名が空の {len(no_name)} 件は登録しません: {'・'.join(no_name[:20])}"))
            if no_cus:
                st.warning(t(f"⚠️ HS か通関英文名が空の {no_cus} 件は報関情報なしで登録されます（斑马で後から入れる）"))
            ok_rows = [r for r in rows if r["アイテム名"]]
            if ok_rows and st.button(t(f"🦓 斑马に登録して SKUID を取る（{len(ok_rows)} 件）"), type="primary",
                                     key="p47s_reg_btn"):
                _skuid_register(ok_rows)
                reg = st.session_state.get("p47s_reg") or {}
        if reg.get("error"):
            st.error(reg["error"])
        elif reg.get("rows"):
            cnt = {k: sum(r["status"] == k for _, r in reg["rows"]) for k in ("ok", "partial", "skipped", "fail")}
            msg = (f"ok={cnt['ok']} 要確認={cnt['partial']} 既存のため未登録={cnt['skipped']} 失敗={cnt['fail']}"
                   f"（計 {len(reg['rows'])} 件）")
            (st.error if cnt["fail"] else st.warning if cnt["partial"] or cnt["skipped"] else st.success)(msg)
            st.dataframe(pd.DataFrame([{"型番": c, t("結果"): r["status"], "SKUID": r.get("sku_id", ""),
                                        t("内容"): r.get("message", "")} for c, r in reg["rows"]]),
                         use_container_width=True, hide_index=True)
            if cnt["fail"]:
                st.info(t("失敗分はもう一度「🦓 斑马に登録」を押せば、未登録の品だけ登録されます（登録済みは SKUID だけ取ります）"))

    pairs = ([(h["code"], h["sku_id"]) for h in hits] + ([(c["code"], c["sku_id"]) for c in conflicts] if allow_dup else [])
             + [(c, r["sku_id"]) for c, r in reg.get("rows", []) if r.get("sku_id")])
    st.divider()
    if pairs:
        st.download_button(t(f"⬇️ NST SKUID 更新 CSV（{len(pairs)} 件）"), data=SF.update_csv(pairs),
                           file_name=TPL.dated_filename(registrant="SKUID更新"), mime="text/csv", type="primary",
                           key="p47s_csv_dl")
        st.caption(t("NST の CSV インポート（更新・型番で照合）で取り込んでください。斑马に無い品は登録後にこの CSV に加わります"))
    else:
        st.info(t("CSV に出す品はまだありません"))


_render_skuid_tab()


