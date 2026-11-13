"""模块 #47 · 🦓 斑马・NST 整合检查（Boss 2026-11-12「做成一个模块，不放在商品登录里，单独做为一个独立的板块」）

page15 の「🔎 SKUID 空チェック」tab をそのまま独立させたもの（条件・既定値・書き戻しの方法は同じ）:
  🔎 SKUID 空      NST で SKUID（custitem_item_skuid＝斑马 SKUID・在庫同期用）が空の品 → 斑马の同じ型番 / JAN の SKUID、
                   無ければ斑马に登録して取る → NST 更新 CSV（型番 + SKUID・人が NST に取り込む）。shared/skuid_fill
  🩺 斑马登録内容  新品（NST 作成 30 日以内・存量は page46 · Boss 2026-11-13「按对象分开」）のうち SKUID がある品の斑马 SKU で 三辺・重量・画像・英文名・HS の空 → CMS で直して斑马へ書き戻す。shared/banma_fix
                   （報関 = SKU 単位 API / 三辺・重量・画像 = SPU 丸ごと PUT・描述/包材ありの SPU は書かない・台帳 banma.cms_write_log）
条件（すべて選択可・既定 = 直近 30 日 / 輸出 2 部門 / 取扱中 / 有効 / 全ランク / 型番+JAN / 斑马 API / 重複は出さない / 仮値も空）。
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from data_warehouse.templates import nst_item_master as TPL
from shared import banma_client, hs_ai, hs_classify, image_search, nst_suiteql
from shared import banma_fix as BF
from shared.banma_dims import NEW_DAYS
from shared import banma_register as BMR
from shared import item_register as REG
from shared import nst_choices as NC
from shared import skuid_fill as SF
from shared.auth import require_password
from shared.db import get_connection
from shared.editor_state import lang_stable_editor
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


# 1 件ずつだと数百件で数十分（2026-11-12 Boss「加速跑」・斑马網関は 1 回数十秒の日がある）→ 並列。
# 斑马は名目 5 QPS・0.25s 間隔で 429（memory 2026-10-07）→ 同時 8 本・開始間隔 0.3s（≤3.3 QPS）。429 は client が待って再試行
# 画像検索（楽天 API ほか）は 4 本・間隔なし（page22 と同じ源）。
_BM = {"workers": 8, "gap": 0.3}


def _parallel(fn, keys: list, text: str, *, workers: int = 4, gap: float = 0.0) -> dict:
    """keys を fn で同時 workers 本・開始は gap 秒以上あける → {key: 結果 or 例外}。進捗バーはメインスレッドだけで触る。"""
    out: dict = {}
    if not keys:
        return out
    lock, nxt = threading.Lock(), [0.0]
    ctx = get_script_run_ctx()   # 工作スレッドでも t() / get_lang がこのセッションの言語を読めるように

    def run(k):
        add_script_run_ctx(threading.current_thread(), ctx)
        if gap:
            with lock:
                now = time.monotonic()
                start = max(now, nxt[0])
                nxt[0] = start + gap
            time.sleep(start - now)
        return fn(k)
    bar = st.progress(0.0, text=text)
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(run, k): k for k in keys}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                out[futs[f]] = f.result()
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない（呼び出し側が件数を出す）
                out[futs[f]] = e
            bar.progress(i / len(keys), text=f"{text} {i}/{len(keys)}")
    bar.empty()
    return out


def _bm_skus(client, keys: list[str], label: str) -> tuple[dict, set[str]]:
    """斑马 SKU を 50 件ずつ並列照会 → (コード→SKU, 照会できなかったコード)。
    元川→斑马は越境経路のロスで塊ごと落ちることがある（2026-11-12 実測）→ 1 塊の失敗でチェック全体を止めない。"""
    chunks = [tuple(keys[i:i + 50]) for i in range(0, len(keys), 50)]
    got: dict = {}
    failed: set[str] = set()
    for ch, v in _parallel(lambda ch: BMR.get_skus(client, list(ch)), chunks, label, **_BM).items():
        if isinstance(v, Exception):
            failed.update(ch)
        else:
            got.update(v)
    return got, failed


def _unchecked(r: dict, got: dict, failed: set[str], by_jan: bool) -> bool:
    """照会できなかった品（斑马に無いとは言えない → 「無し」にも「有り」にも入れない）。"""
    code, jan = str(r.get("code") or ""), str(r.get("jan") or "")
    if code in failed:
        return True
    return bool(by_jan and jan and jan in failed and not got.get(code))


def _issues_df(issues) -> pd.DataFrame:
    return pd.DataFrame([{t("行"): i.row or "", t("型番 / JAN"): i.jan,
                          t("区分"): "❌ error" if i.level == "error" else "⚠️ warn",
                          t("内容"): i.message} for i in issues])

# NST で SKUID（斑马 SKUID・在庫同期用）が空の品 → 斑马の同じ型番 / JAN の SKUID、無ければ斑马に登録して取る
# → NST 更新 CSV（型番 + SKUID）。NST へは書かない（CSV は人が取り込む）。Boss 2026-11-12

_SK_KEYS = ["p47s_result", "p47s_prep", "p47s_reg", "p47s_editor"]
_SK_SOURCES = {"api": "斑马 API（実時間・遅いと 50 件で 1 分ほど）", "pg": "PG の写し（毎日 04:00 時点・速い）"}
_SK_PKG = ["パッケージ重量(g)", "パッケージ高さ(cm)", "パッケージ幅(cm)", "パッケージ奥行(cm)"]


def _skuid_check(opts: dict, only: set[str] | None = None) -> dict:
    """NST の SKUID 空 → 条件で絞る → 斑马に照会 → (hits, conflicts, misses, unchecked)。
    照会できなかった品は unchecked（「斑马に無し」にしない＝重複登録を防ぐ）。only = その型番だけ再照会。"""
    with st.spinner(t("NST で SKUID が空の品を取得中…")):
        items = SF.blank_items(nst_suiteql.suiteql_all, days=SF.PERIODS[opts["period"]])
    picked = SF.pick(items, depts=opts["depts"], handlings=opts["handlings"], states=opts["states"],
                     ranks=opts["ranks"])
    n_picked = len(picked)
    if only is not None:
        picked = [r for r in picked if str(r.get("code") or "") in only]
    keys = SF.lookup_keys(picked, by_jan=opts["by_jan"])
    bm: dict[str, str] = {}
    failed: set[str] = set()
    if keys:
        label = t("斑马に {n} コードを照会中（{src}）…").format(n=len(keys), src=t(_SK_SOURCES[opts["source"]]))
        if opts["source"] == "api":
            client = banma_client.BanmaClient.from_env()
            with get_connection() as conn:
                banma_client.ensure_token(conn, client)
            got, failed = _bm_skus(client, keys, label)
            bm = {c: str(x.get("SKUID") or "") for c, x in got.items()}
        else:
            with st.spinner(label), get_connection() as conn:
                bm = SF.mirror_skus(conn, keys)
    unchecked = [r for r in picked if _unchecked(r, bm, failed, opts["by_jan"])]
    picked = [r for r in picked if r not in unchecked]
    found = sorted({v for v in bm.values() if v})
    with st.spinner(t("同じ SKUID を持つ NST の品を確認中…")):
        in_use = SF.skuids_in_use(nst_suiteql.suiteql_all, found) if found else {}
    hits, conflicts, misses = SF.match(picked, bm, by_jan=opts["by_jan"], in_use=in_use)
    return {"opts": opts, "total": len(items), "picked": n_picked, "hits": hits, "conflicts": conflicts,
            "misses": misses, "unchecked": unchecked}


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
        with st.spinner(t("画像を探しています（{n} 件）…").format(n=len(need_img))):
            stats["images"] = REG.fill_images(need_img, extra, image_search.search_jan)
    # AI 突合 = NST の値で作った品 + 台帳でも HS が空の品（台帳の HS は人が確認済みなので触らない）
    need_ai = [r for r in rows if extra[REG.item_key(r)]["source"] == "NST" or not extra[REG.item_key(r)].get(REG.COL_HS)]
    if need_ai and hs_ai.is_configured():
        with st.spinner(t("HS を AI でも判定中（{n} 件）…").format(n=len(need_ai))):
            issues = REG.cross_check_hs(need_ai, extra, stats, hs_ai.judge)
    n_nst = sum(e["source"] == "NST" for e in extra.values())
    return {"rows": rows, "extra": extra, "issues": issues, "stats": stats,
            "n_ledger": len(rows) - n_nst, "n_nst": n_nst}


def _skuid_frame(prep: dict) -> pd.DataFrame:
    return pd.DataFrame([{
        "型番": REG.item_key(r), t("出典"): t(prep["extra"][REG.item_key(r)]["source"]), "JANコード": r.get("JANコード", ""),
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
        st.session_state["p47s_reg"] = {"error": t("斑马に接続できません（登録していません）: {err}").format(
            err=f"{type(e).__name__}: {e}")}
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
def _cc_suggest(item: dict) -> dict:
    """重量・三辺の提案（page46 と同じ: NST 包装重量・異常値の閘門 / NST 包装寸法 · JDL は 2026-11-13 停用）。"""
    from shared.banma_dims import _single_weight, nst_dims
    out = {}
    w, src, _ = _single_weight(item.get("pkg_weight"))
    if w:
        out["Weight"] = (w, src)
    if d := nst_dims(tuple(item.get(k) for k in ("pkg_height", "pkg_width", "pkg_depth"))):
        out["dims"] = (d, "NST")
    return out


def _content_check(opts: dict, checks: tuple, placeholder: bool, only: set[str] | None = None) -> dict:
    """NST で SKUID がある品（条件で絞る）→ 斑马 SKU（型番 / JAN）→ 報関 → 空の品だけ・提案を入れた行。
    斑马に照会できなかった品は unchecked（チェック全体は止めない）。only = その型番だけ再照会。"""
    # 新品だけ（Boss 2026-11-13「按对象分开」: 存量は page46）→ 作成日は最長 NEW_DAYS 日
    days = min(SF.PERIODS[opts["period"]] or NEW_DAYS, NEW_DAYS)
    with st.spinner(t("NST で SKUID がある品を取得中…")):
        items = SF.blank_items(nst_suiteql.suiteql_all, days=days, has_skuid=True)
    picked = SF.pick(items, depts=opts["depts"], handlings=opts["handlings"], states=opts["states"],
                     ranks=opts["ranks"])
    n_picked = len(picked)
    if only is not None:
        picked = [r for r in picked if str(r.get("code") or "") in only]
    client = banma_client.BanmaClient.from_env()
    with get_connection() as conn:
        banma_client.ensure_token(conn, client)
    keys = SF.lookup_keys(picked, by_jan=opts["by_jan"])
    lap = {}
    t0 = time.monotonic()
    skus, failed = _bm_skus(client, keys, t("斑马の SKU を照会中（{n} コード）…").format(n=len(keys)))
    lap["斑马 SKU"] = time.monotonic() - t0
    unchecked = [r["code"] for r in picked if _unchecked(r, skus, failed, opts["by_jan"])]
    found, not_found, combos = [], [], 0
    for r in picked:
        if r["code"] in unchecked:
            continue
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
    t0 = time.monotonic()
    if need and opts["source"] == "pg":   # 写しの範囲（Export・NST 等級 A/B/C/NEW）は写しで決める。範囲外だけ API
        ranked = {str(s["SKUID"]) for r, s in found if r.get("item_rank") in ("Aランク", "Bランク", "Cランク", "NEW")}
        with get_connection() as conn:
            customs = BF.mirror_customs(conn, need, ranked)
    rest = [x for x in need if x not in customs]
    for sid, v in _parallel(lambda sid: BF.get_customs(client, sid), rest,
                            t("斑马の報関情報を照会中（API {n} 件）…").format(n=len(rest)), **_BM).items():
        if isinstance(v, Exception):   # その品は報関を判定しない（件数は画面に出す）
            cus_err[sid] = f"{type(v).__name__}: {v}"
        else:
            customs[sid] = v
    lap["報関"] = time.monotonic() - t0
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
                         "note": t("NST の SKUID {nst} ≠ 斑马 {bm}").format(nst=r.get("sku_id"), bm=sid)
                         if str(r.get("sku_id")) != sid
                         else t("報関の照会に失敗: {err}").format(err=cus_err[sid]) if sid in cus_err else ""})
    if rows:
        lap.update(_cc_fill_suggestions(rows))
        with get_connection() as conn:
            spu = BF.spu_ids(conn, [x["sku_id"] for x in rows])
        for x in rows:
            x["spu_id"] = spu.get(x["sku_id"], "")
    return {"key": (opts, checks, placeholder), "total": len(items), "picked": n_picked, "found": len(found),
            "not_found": not_found, "combos": combos, "cus_err": len(cus_err), "rows": rows,
            "n_api_customs": len(rest), "lap": lap, "unchecked": unchecked}


def _cc_fill_suggestions(rows: list[dict]) -> dict:
    """空の項目に提案を入れる（重量・三辺 = NST 包装 / 画像 = 画像検索 / HS・英文名 = 規則 + AI）。new = 書く候補。"""
    try:
        prefix = _cached_prefix_makers()
    except Exception:  # noqa: BLE001
        prefix = {}
    lap = {}
    t0 = time.monotonic()
    img_jans = sorted({str(x["item"]["jan"]) for x in rows if "画像" in x["probs"] and x["item"].get("jan")})
    images = _parallel(image_search.search_jan, img_jans, t("画像を検索中（{n} 件）…").format(n=len(img_jans)))
    lap["画像検索"] = time.monotonic() - t0
    ai_rows, extra = [], {}
    for x in rows:
        it, new, src = x["item"], dict(x["cur"]), []
        sug = _cc_suggest(it)
        if "重量" in x["probs"] and "Weight" in sug:
            new["Weight"] = sug["Weight"][0]
            src.append(f"{t('重量')}={sug['Weight'][1]}")
        if "三辺" in x["probs"] and "dims" in sug:
            new.update(zip(("Length", "Width", "Height"), sug["dims"][0]))
            src.append(f"{t('三辺')}={sug['dims'][1]}")
        if "画像" in x["probs"] and it.get("jan"):
            url = images.get(str(it["jan"]))
            if isinstance(url, str) and url.startswith("https://"):
                new["Image"] = url
                src.append(f"{t('画像')}={t('画像検索')}")
        if {"英文名", "HS"} & set(x["probs"]) and it.get("name"):
            cus = hs_classify.classify_customs(str(it["name"]), str(it.get("maker") or ""), str(it.get("jan") or ""),
                                               prefix_map=prefix)
            code = str(it["code"])
            extra[code] = {REG.COL_HS: cus.hs or "", REG.COL_NAME_EN: cus.name_en or ""}
            ai_rows.append(({"型番": code, "アイテム名": str(it["name"]), "メーカー名": str(it.get("maker") or "")}, x))
        x["new"], x["src"] = new, src
    t0 = time.monotonic()
    if ai_rows and hs_ai.is_configured():   # 25 件ずつの AI 呼び出しを同時 4 本（extra は品ごとに別キー）
        parts = [tuple(range(i, min(i + hs_ai.BATCH, len(ai_rows)))) for i in range(0, len(ai_rows), hs_ai.BATCH)]
        _parallel(lambda ix: REG.cross_check_hs([ai_rows[i][0] for i in ix], extra, {}, hs_ai.judge), parts,
                  t("HS を AI でも判定中（{n} 件）…").format(n=len(ai_rows)))
    lap["AI 判定"] = time.monotonic() - t0
    for r, x in ai_rows:
        ex = extra[r["型番"]]
        if "HS" in x["probs"] and ex.get(REG.COL_HS):
            x["new"]["Code"] = ex[REG.COL_HS]
            x["src"].append("HS=" + ("AI" if ex.get("hs_basis") == "ai" else t("規則")))
        if "英文名" in x["probs"] and ex.get(REG.COL_NAME_EN):
            x["new"]["NameEn"] = ex[REG.COL_NAME_EN]
            x["src"].append(t("英文名") + "=" + ("AI" if ex.get("hs_basis") == "ai" else t("規則")))
    return lap


def _cc_value(col: str, v):
    """表の値 → 斑马に送る値。(値, エラー)。空 = 変えない。"""
    s = "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v).strip()
    if not s:
        return None, ""
    if col in _CC_INT:
        try:
            n = math.ceil(float(s))
        except ValueError:
            return None, t("{col} が数値ではありません「{s}」").format(col=col, s=s)
        return (n, "") if n > 0 else (None, t("{col} は 1 以上").format(col=col))
    if col == "Image" and not s.startswith("https://"):
        return None, t("画像URL は https:// で始まる URL")
    if col == "Code" and not BF._HS.fullmatch(s):
        return None, t("HS は 6/8/10 桁の数字「{s}」").format(s=s)
    return s, ""


def _cc_changes(rows: list[dict], recs: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """表 → 各行の変更（product / customs）と入力エラー。現値と同じ値は送らない。"""
    out, errs = [], []
    for x, rec in zip(rows, recs):
        prod, cus = {}, {}
        for col, key in _CC_COLS.items():
            cur = x["cur"].get(key)
            if BF._same(rec.get(col), cur):    # 触っていない値（斑马の現値 0 など）は検査も送信もしない
                continue
            v, err = _cc_value(key, rec.get(col))
            if err:
                errs.append((str(x["item"]["code"]), err))
                continue
            if v is None or BF._same(v, cur):
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
                out.append({"型番": c["item"]["code"], "対象": t("三辺・重量・画像"), "結果": "skipped", "excel": c,
                            "内容": t("SPUID 不明（斑马の写しは毎日 04:00 更新・翌日もう一度）→ 三辺・重量は Excel")})
    n = len(by_spu) + sum(bool(c["cus"]) for c in changes)
    bar = st.progress(0.0, text=t("斑马に書込中…"))
    done, stop = 0, ""
    with get_connection() as conn:
        banma_client.ensure_token(conn, client)
        for spu, cs in by_spu.items():
            codes = ",".join(str(c["item"]["code"]) for c in cs)
            if stop:
                r = {"status": "skipped", "message": t("中止（{at} で回読が一致しなかったため）").format(at=stop)}
            else:
                r = BF.write_product(client, conn, spu, {c["sku_id"]: c["prod"] for c in cs}, codes,
                                     expect={c["sku_id"]: c["cur"] for c in cs})
                if r["status"] == "mismatch":
                    stop = f"SPU {spu}"
            for c in cs:
                out.append({"型番": c["item"]["code"], "対象": t("三辺・重量・画像"), "結果": r["status"],
                            "内容": r.get("message", ""), "excel": c if r["status"] in ("skipped", "fail") else None})
            done += 1
            bar.progress(done / n, text=f"{done}/{n} SPU {spu}")
        for c in changes:
            if not c["cus"]:
                continue
            r = BF.write_customs(client, conn, c["sku_id"], str(c["item"]["code"]), c["cus"],
                                 name_cn=str(c["item"].get("name") or ""),
                                 weight=c["prod"].get("Weight") or c["cur"].get("Weight") or 0, expect=c["cur"])
            out.append({"型番": c["item"]["code"], "対象": t("報関"), "結果": r["status"], "内容": r.get("message", "")})
            done += 1
            bar.progress(done / n, text=f"{done}/{n} {c['item']['code']}")
    bar.empty()
    return out


def _render_content_result(res: dict) -> None:
    rows = res["rows"]
    cnt = {p: sum(p in x["probs"] for x in rows) for p in BF.CHECKS}
    st.info(t("SKUID あり（{period}・部門 輸出）{total} 件 → 条件に合う {picked} 件 → "
              "斑马で照合 {found} 件（見つからない {nf} · 組合せ品は対象外 {combos}）→ 空あり {n} 件（{detail}）").format(
                period=t(res["key"][0]["period"]), total=res["total"], picked=res["picked"], found=res["found"],
                nf=len(res["not_found"]), combos=res["combos"], n=len(rows),
                detail=" · ".join(f"{t(k)} {v}" for k, v in cnt.items() if k in res["key"][1]))
            + (" · " + t("報関の照会失敗 {n} 件（その品は報関を判定していません）").format(n=res["cus_err"])
               if res["cus_err"] else ""))
    lap = res.get("lap") or {}
    st.caption(t("所要") + ": " + " · ".join(t("{k} {v:.0f} 秒").format(k=t(k), v=v) for k, v in lap.items())
               + " · " + t("報関の API 照会 {n} 件").format(n=res.get("n_api_customs", 0))
               + (t("（PG 写し 04:00 時点を優先・書く前に斑马の現値を確かめ、チェック時と違えば書きません）")
                  if res["key"][0]["source"] == "pg" else ""))
    if res.get("unchecked"):
        _retry_block(res["unchecked"], "p47c_retry", lambda codes: _merge_content(
            res, _content_check(*res["key"], only=set(codes))), "p47c_result", ("p47c_reg",))
    if res["not_found"]:
        with st.expander(t("斑马で見つからない {n} 件（型番 / JAN）").format(n=len(res["not_found"]))):
            st.write("・".join(map(str, res["not_found"])))
    if not rows:
        st.success(t("空の項目はありません"))
        return
    st.caption(t("白い列を直して「🦓 斑马に反映」。提案の列 = 自動で入れた値の出典（重量・三辺 = NST 包装 / 画像 = 画像検索 / "
                 "HS・英文名 = 規則 + AI）。空欄・現値と同じ値は送りません。三辺・重量・画像は SPU 丸ごとの書込のため、"
                 "描述・包材がある SPU には書かず Excel（三辺・重量）か斑马の画面（画像）に回します"))
    df = pd.DataFrame([{"型番": x["item"]["code"], t("アイテム名"): x["item"].get("name") or "",
                        t("空の項目"): "・".join(t(p) for p in x["probs"]),
                        **{col: ("" if x["new"].get(key) is None else str(x["new"].get(key))) for col, key in _CC_COLS.items()},
                        t("提案"): " · ".join(x["src"]), t("備考"): x["note"]} for x in rows])
    edited = lang_stable_editor(df, key="p47c_editor", reset=tuple(df["型番"]), hide_index=True, num_rows="fixed",
                                use_container_width=True,
                                disabled=["型番", t("アイテム名"), t("空の項目"), t("提案"), t("備考")],
                                # 列名は原文のまま（_cc_changes が列名で読む）。表示名だけ t()
                                column_config={**{c: st.column_config.Column(t(c)) for c in ("型番", *_CC_COLS)},
                                               "英文名": st.column_config.TextColumn(t("英文名"),
                                                                                  max_chars=hs_classify.MAX_NAME_EN),
                                               "画像URL": st.column_config.LinkColumn(t("画像URL"))})
    changes, errs = _cc_changes(rows, edited.to_dict("records"))
    for code, e in errs[:20]:
        st.error(f"❌ {code}: {e}")
    n_p = sum(bool(c["prod"]) for c in changes)
    n_c = sum(bool(c["cus"]) for c in changes)
    if changes and not errs and st.button(t("🦓 斑马に反映（三辺・重量・画像 {p} 件 · 報関 {c} 件）").format(p=n_p, c=n_c),
                                          type="primary",
                                          key="p47c_write"):
        st.session_state["p47c_reg"] = _cc_write(changes)
    reg = st.session_state.get("p47c_reg")
    if reg:
        c = {k: sum(r["結果"] == k for r in reg) for k in ("ok", "created", "skipped", "fail", "mismatch")}
        msg = t("ok={ok}（報関の新規作成 {created}）skipped={skipped} fail={fail} 回読不一致={mismatch}（計 {n} 件）").format(
            ok=c["ok"] + c["created"], created=c["created"], skipped=c["skipped"], fail=c["fail"],
            mismatch=c["mismatch"], n=len(reg))
        (st.error if c["fail"] or c["mismatch"] else st.warning if c["skipped"] else st.success)(msg)
        if c["mismatch"]:
            st.error(t("❌ 回読が一致しない SPU があったため、以降の SPU は書いていません。斑马で該当 SPU を確認してください"
                       "（書く前の値は台帳 banma.cms_write_log にあります）"))
        st.dataframe(pd.DataFrame([{t(k): v for k, v in r.items() if k != "excel"} for r in reg]),
                     use_container_width=True, hide_index=True)
        xl = [{"SKU编码": r["excel"]["sku_code"],
               **{h: r["excel"]["prod"].get(k) for h, k in (("重量(g)", "Weight"), ("长(cm)", "Length"),
                                                         ("宽(cm)", "Width"), ("高(cm)", "Height"))}}
              for r in reg if r.get("excel") and {"Weight", "Length", "Width", "Height"} & set(r["excel"]["prod"])]
        if xl:
            st.download_button(t("⬇️ 斑马「批量修改SKU信息」Excel（API で書かなかった三辺・重量 {n} 件）").format(n=len(xl)),
                               data=BF.dims_xlsx(xl), file_name=f"斑马_批量修改SKU信息_{datetime.now():%Y%m%d}.xlsx",
                               mime=_XLSX_MIME, key="p47c_xlsx")
        if any(r.get("excel") and "Image" in r["excel"]["prod"] for r in reg):
            st.info(t("画像を API で書かなかった品は斑马の画面で入れてください（上の表の skipped / fail）"))


def _retry_block(codes: list[str], key: str, rerun, state_key: str, drop: tuple = ()) -> None:
    """斑马に照会できなかった品の件数・型番と「未チェックだけ再照会」ボタン。再照会の結果は今の結果に足す。"""
    st.warning(t("⚠️ 斑马に照会できなかった {n} 件は未チェック（斑马との通信エラー・時間をおいて再照会）: {codes}").format(
        n=len(codes), codes="・".join(map(str, codes[:30])) + (" …" if len(codes) > 30 else "")))
    if st.button(t("🔁 未チェックの {n} 件だけ再照会").format(n=len(codes)), key=key):
        try:
            st.session_state[state_key] = rerun(codes)
        except Exception as e:  # noqa: BLE001
            st.error(t("❌ チェックに失敗（何も出力していません）: {err}").format(err=f"{type(e).__name__}: {e}"))
            return
        for k in drop:   # 斑马に無し の品が増えたら登録準備は作り直し
            st.session_state.pop(k, None)
        st.rerun()


def _merge_skuid(res: dict, new: dict) -> dict:
    return {**res, **{k: res[k] + new[k] for k in ("hits", "conflicts", "misses")}, "unchecked": new["unchecked"]}


def _merge_content(res: dict, new: dict) -> dict:
    lap = dict(res.get("lap") or {})
    for k, v in (new.get("lap") or {}).items():
        lap[k] = lap.get(k, 0) + v
    return {**res, "rows": res["rows"] + new["rows"], "found": res["found"] + new["found"],
            "not_found": res["not_found"] + new["not_found"], "combos": res["combos"] + new["combos"],
            "cus_err": res["cus_err"] + new["cus_err"], "n_api_customs": res.get("n_api_customs", 0)
            + new.get("n_api_customs", 0), "lap": lap, "unchecked": new["unchecked"]}


def _render_skuid_tab() -> None:
    """⚠️ st.stop() 禁止（他タブが消える）。抜けるときは return。"""
    st.caption(t("🔎 NST で SKUID（斑马 SKUID・在庫同期用）が空の品を探し、斑马の同じ型番 / JAN の SKUID を取る。"
                 "斑马に無ければ斑马に登録してから取る → NST 更新 CSV（型番 + SKUID）を手で NST に取り込む。"
                 "🩺 SKUID がある品は斑马の三辺・重量・画像・HS・英文名の空を探し、CMS で直して斑马へ書き戻す"))
    d = SF.DEFAULTS
    c1, c2, c3 = st.columns(3)
    # 選択肢の値は原文のまま（SF.pick が値で絞る）。表示だけ format_func=t
    period = c1.selectbox(t("作成日"), list(SF.PERIODS), index=list(SF.PERIODS).index(d["period"]), key="p47s_period",
                          format_func=t)
    depts = c2.multiselect(t("部門"), SF.DEPTS, default=list(d["depts"]), key="p47s_depts", format_func=t)
    handlings = c3.multiselect(t("取扱区分"), SF.HANDLINGS, default=list(d["handlings"]), key="p47s_handlings",
                               format_func=t)
    c4, c5, c6 = st.columns(3)
    states = c4.multiselect(t("状態"), SF.STATES, default=list(d["states"]), key="p47s_states", format_func=t)
    ranks = c5.multiselect(t("商品ランク"), SF.RANKS, default=list(d["ranks"]), key="p47s_ranks", format_func=t)
    by_jan = c6.radio(t("斑马での照合"), [True, False], index=0, key="p47s_by_jan", horizontal=True,
                      format_func=lambda v: t("型番 + JAN") if v else t("型番のみ"),
                      help=t("型番で見つからなければ JAN（斑马の単品 SKU コード = JAN）でも探す"))
    # 1 つの斑马 SKUID を複数の品に入れる選択肢は置かない（在庫同期が崩れる・Boss 2026-11-12「这个功能本身就是错误的」）
    source = st.radio(t("斑马の照会元"), list(_SK_SOURCES), index=0, key="p47s_source",
                      format_func=lambda k: t(_SK_SOURCES[k]))
    opts = {"period": period, "depts": tuple(depts), "handlings": tuple(handlings), "states": tuple(states),
            "ranks": tuple(ranks), "by_jan": by_jan, "source": source}
    c9, c10 = st.columns(2)
    checks = c9.multiselect(t("🩺 斑马の検査項目"), BF.CHECKS, default=list(BF.CHECKS), key="p47c_checks", format_func=t)
    placeholder = c10.checkbox(t("仮値も空とみなす（重量 0/1/10/100/200/300/500g・三辺 1×1×1/10×10×10・待上图の画像）"),
                               value=True, key="p47c_placeholder", help=t("page46 と同じ口径（Boss 2026-10-07〜09）"))
    b1, b2, b3 = st.columns(3)
    if b1.button(t("🔍 NST で SKUID が空の品をチェックする"), type="primary", key="p47s_run", use_container_width=True):
        for k in _SK_KEYS:
            st.session_state.pop(k, None)
        try:
            st.session_state["p47s_result"] = _skuid_check(opts)
        except Exception as e:  # noqa: BLE001 — 照会失敗は「斑马に無し」にしない
            st.error(t("❌ チェックに失敗（何も出力していません）: {err}").format(err=f"{type(e).__name__}: {e}"))
    if b2.button(t("🩺 斑马の登録内容をチェックする"), type="primary", key="p47c_run", use_container_width=True):
        for k in _CC_KEYS:
            st.session_state.pop(k, None)
        try:
            st.session_state["p47c_result"] = _content_check(opts, tuple(checks), placeholder)
        except Exception as e:  # noqa: BLE001
            st.error(t("❌ チェックに失敗（何も書いていません）: {err}").format(err=f"{type(e).__name__}: {e}"))
    if b3.button(t("🗑️ 清除结果"), key="p47s_clear", use_container_width=True):
        for k in _SK_KEYS + _CC_KEYS:
            st.session_state.pop(k, None)
        st.rerun()
    res, cres = st.session_state.get("p47s_result"), st.session_state.get("p47c_result")
    if not res and not cres:
        st.info(t("条件を選んで「🔍 NST で SKUID が空の品をチェックする」か「🩺 斑马の登録内容をチェックする」"))
    if res:
        st.subheader(t("🔎 NST で SKUID が空の品"))
        if res["opts"] != opts:
            st.warning(t("条件が変わりました。「🔍 NST で SKUID が空の品をチェックする」を押してください（前回の結果は隠しています）"))
        else:
            _render_skuid_result(res)
    if cres:
        st.subheader(t("🩺 斑马の登録内容"))
        st.caption(t("新品だけ（NST 作成 {n} 日以内・作成日の条件が長くても {n} 日まで）。それより前の品・組合品は「🩺 斑马信息健康度检查」").format(n=NEW_DAYS))
        if cres["key"] != (opts, tuple(checks), placeholder):
            st.warning(t("条件が変わりました。「🩺 斑马の登録内容をチェックする」を押してください（前回の結果は隠しています）"))
        else:
            _render_content_result(cres)


def _render_skuid_result(res: dict) -> None:
    hits, conflicts, misses = res["hits"], res["conflicts"], res["misses"]
    n_code = sum(h["key"] == "型番" for h in hits + conflicts)
    st.info(t("NST で SKUID が空（{period}・部門 輸出）{total} 件 → 条件に合う {picked} 件 → "
              "斑马に有り {found} 件（型番一致 {n_code} · JAN 一致 {n_jan}）"
              "／うち同じ SKUID の重複 {dup} 件 · 斑马に無し {miss} 件").format(
                period=t(res["opts"]["period"]), total=res["total"], picked=res["picked"],
                found=len(hits) + len(conflicts), n_code=n_code, n_jan=len(hits) + len(conflicts) - n_code,
                dup=len(conflicts), miss=len(misses)))
    if res.get("unchecked"):
        _retry_block([r["code"] for r in res["unchecked"]], "p47s_retry", lambda codes: _merge_skuid(
            res, _skuid_check(res["opts"], only=set(codes))), "p47s_result", ("p47s_prep", "p47s_editor"))
    if hits:
        st.subheader(t("✅ 斑马に有り（{n} 件・CSV に出す）").format(n=len(hits)))
        st.dataframe(pd.DataFrame([{t("型番"): h["code"], t("JANコード"): h["jan"], t("アイテム名"): h["name"],
                                    t("照合"): t(h["key"]), t("斑马 SKUID"): h["sku_id"]} for h in hits]),
                     use_container_width=True, hide_index=True)
    if conflicts:
        st.subheader(t("⚠️ 同じ斑马 SKUID の重複（{n} 件・{how}）").format(
            n=len(conflicts), how=t("CSV に出さない")))
        st.dataframe(pd.DataFrame([{t("型番"): c["code"], t("JANコード"): c["jan"], t("アイテム名"): c["name"],
                                    t("斑马 SKUID"): c["sku_id"], t("内容"): c["note"]} for c in conflicts]),
                     use_container_width=True, hide_index=True)

    reg = st.session_state.get("p47s_reg") or {}
    if misses:
        st.subheader(t("🦓 斑马に無し（{n} 件）→ 斑马に登録してから SKUID を取る").format(n=len(misses)))
        prep = st.session_state.get("p47s_prep")
        if not prep:
            st.dataframe(pd.DataFrame([{t("型番"): m["code"], t("JANコード"): m["jan"], t("アイテム名"): m["name"],
                                        t("作成日"): m.get("created"), t("商品ランク"): m.get("item_rank") or ""}
                                       for m in misses]), use_container_width=True, hide_index=True)
            if st.button(t("📝 登録の準備（{n} 件・page15 の台帳 → NST の値・HS と画像を自動で埋める）").format(n=len(misses)),
                         key="p47s_prep_btn"):
                try:
                    st.session_state["p47s_prep"] = _skuid_prepare(misses)
                except Exception as e:  # noqa: BLE001
                    st.error(t("❌ 準備に失敗: {err}").format(err=f"{type(e).__name__}: {e}"))
                    return
                st.rerun()
        else:
            st.caption(t("出典: page15 の台帳 {ledger} 件（登録時に人が確認した値）· NST {nst} 件").format(
                ledger=prep["n_ledger"], nst=prep["n_nst"])
                + (" · " + t("画像") + " " + " ".join(f"{t(k)}={v}" for k, v in prep["stats"]["images"].items())
                   if prep["stats"].get("images") else ""))
            if prep["issues"]:
                with st.expander(t("⚠️ HS の要確認 {n} 件").format(n=len(prep["issues"]))):
                    st.dataframe(_issues_df(prep["issues"]), use_container_width=True, hide_index=True)
            df = _skuid_frame(prep)
            edited = lang_stable_editor(   # 表頭・出典が言語で変わっても編集を保つ
                df, key="p47s_editor", reset=tuple(df["型番"]), hide_index=True, num_rows="fixed",
                use_container_width=True,
                disabled=["型番", t("出典"), "JANコード", t("HS 判定")],
                # 列名は原文のまま（下で r["アイテム名"] などと読む）。表示名だけ t()
                column_config={**{c: st.column_config.Column(t(c)) for c in ("型番", "JANコード", "商品原価", "HS",
                                                                            "画像URL", *_SK_PKG)},
                               "アイテム名": st.column_config.TextColumn(t("アイテム名"), max_chars=REG.NAME_MAX),
                               "通関英文名": st.column_config.TextColumn(t("通関英文名"),
                                                                    max_chars=hs_classify.MAX_NAME_EN)})
            recs = edited.to_dict("records")
            rows = [{"型番": r["型番"], "アイテム名": str(r["アイテム名"] or "").strip(), "商品原価": r["商品原価"],
                     **{c: r[c] for c in _SK_PKG}, REG.COL_HS: str(r["HS"] or "").strip(),
                     REG.COL_NAME_EN: str(r["通関英文名"] or "").strip(), REG.COL_IMAGE: str(r["画像URL"] or "").strip()}
                    for r in recs]
            no_name = [r["型番"] for r in rows if not r["アイテム名"]]
            no_cus = sum(not r[REG.COL_HS] or not r[REG.COL_NAME_EN] for r in rows)
            no_wt = sum(not str(r["パッケージ重量(g)"] or "").strip() for r in rows)
            if no_wt:
                st.warning(t("⚠️ パッケージ重量(g) が空の {n} 件は 1 g で登録されます（分かれば表に入れてください）").format(n=no_wt))
            if no_name:
                st.error(t("❌ アイテム名が空の {n} 件は登録しません: {codes}").format(n=len(no_name),
                                                                              codes="・".join(no_name[:20])))
            if no_cus:
                st.warning(t("⚠️ HS か通関英文名が空の {n} 件は報関情報なしで登録されます（斑马で後から入れる）").format(n=no_cus))
            ok_rows = [r for r in rows if r["アイテム名"]]
            if ok_rows and st.button(t("🦓 斑马に登録して SKUID を取る（{n} 件）").format(n=len(ok_rows)), type="primary",
                                     key="p47s_reg_btn"):
                _skuid_register(ok_rows)
                reg = st.session_state.get("p47s_reg") or {}
        if reg.get("error"):
            st.error(reg["error"])
        elif reg.get("rows"):
            cnt = {k: sum(r["status"] == k for _, r in reg["rows"]) for k in ("ok", "partial", "skipped", "fail")}
            msg = t("ok={ok} 要確認={partial} 既存のため未登録={skipped} 失敗={fail}（計 {n} 件）").format(
                **cnt, n=len(reg["rows"]))
            (st.error if cnt["fail"] else st.warning if cnt["partial"] or cnt["skipped"] else st.success)(msg)
            st.dataframe(pd.DataFrame([{t("型番"): c, t("結果"): r["status"], "SKUID": r.get("sku_id", ""),
                                        t("内容"): r.get("message", "")} for c, r in reg["rows"]]),
                         use_container_width=True, hide_index=True)
            if cnt["fail"]:
                st.info(t("失敗分はもう一度「🦓 斑马に登録」を押せば、未登録の品だけ登録されます（登録済みは SKUID だけ取ります）"))

    pairs, dup = SF.drop_shared_skuids([(h["code"], h["sku_id"]) for h in hits]
                                       + [(c, r["sku_id"]) for c, r in reg.get("rows", []) if r.get("sku_id")])
    st.divider()
    if dup:   # 登録で既存 SKU が返った等で 1 SKUID に 2 型番 → CSV に出さない（重複と同じ扱い）
        st.error(t("❌ 同じ斑马 SKUID が複数の型番に当たったため CSV から外しました: {items}").format(
            items=" · ".join(f"{s}: {'・'.join(cs)}" for s, cs in dup.items())))
    if pairs:
        st.download_button(t("⬇️ NST SKUID 更新 CSV（{n} 件）").format(n=len(pairs)), data=SF.update_csv(pairs),
                           file_name=TPL.dated_filename(registrant="SKUID更新"), mime="text/csv", type="primary",
                           key="p47s_csv_dl")
        st.caption(t("NST の CSV インポート（更新・型番で照合）で取り込んでください。斑马に無い品は登録後にこの CSV に加わります"))
    else:
        st.info(t("CSV に出す品はまだありません"))


_render_skuid_tab()


