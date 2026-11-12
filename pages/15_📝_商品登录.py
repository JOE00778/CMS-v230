"""模块 #15 商品登录 · 新品首次登録（NST 取込 CSV + 斑马は API で直接登録）

定位：商品首次登录（NST/BM 还都没有这个 SKU），不是已登录商品的修改。
修改场景请走 page 03（定義原価編集）/ page 07（商品等级判定）等。

四个 tab：
  🆕 商品登録 v2（Boss 2026-10-03 拍板 · JD 出力廃止）
  🔎 SKUID 空チェック：NST で SKUID 空の品 → 斑马の SKUID（無ければ斑马に登録）→ NST 更新 CSV（shared/skuid_fill · 2026-11-12）
  📦 セット品登録：JAN_数量 → NST 父/子组合货品 CSV
  📜 旧 HTML 版：商品登録ツール iframe

v2 流程（オーケストレーションは shared/item_register.py）：
  ① テンプレ DL（仕入先=nst.vendor_master 完全名称 / 大分類・中分類=NST 実在組合せ / 担当者 4 人）
  ② 記入済み xlsx を UL（パッケージ 3 辺・重量は任意）→ ③「▶ 処理する」
     read_upload → NST 既登録 JAN を error → enrich（jancode 商品名・メーカー・固定値・HS・網調べは常に実行）
     → 画像（jancode → 画像検索 → 待上图）
  ④ ok/ng/warn 件数・issues 表・data_editor で人が確認・修正
  ⑤「📦 生成」NST CSV（直接ダウンロード）+ 斑马へ API 登録（shared/banma_register）、同時に台帳 nst.item_register_log
     （Boss 2026-11-09「去掉包装必填·网上搜·人工确认后生成 NST CSV，同时通过 API 写入斑马」。斑马 xlsx は廃止）
"""
from __future__ import annotations

import hashlib
import io
import math
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from data_warehouse.templates import item_entry_form as FORM
from data_warehouse.templates import nst_item_master as TPL
from data_warehouse.templates import jd_bm_item_master as JBM
from shared import banma_client, hs_ai, hs_classify, image_search, jan_web, nst_suiteql
from shared import banma_fix as BF
from shared import banma_register as BMR
from shared import skuid_fill as SF
from shared import item_register as REG
from shared import nst_choices as NC
from shared.auth import require_password
from shared.db import get_connection
from shared.i18n import t, lang_selector
from shared.theme import inject_theme

st.set_page_config(page_title=t("商品登录"), page_icon="📝", layout="wide")
require_password()
inject_theme()
lang_selector()

st.title(t("📝 商品登录"))

tab_item, tab_skuid, tab_bundle, tab_legacy = st.tabs([
    t("🆕 商品登録 (原生)"),
    t("🔎 SKUID 空チェック"),
    t("📦 セット品登録 (原生)"),
    t("📜 旧 HTML 版"),
])

JAN_COL_NAME = "JANコード"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ───────────────────────── tab 🆕 商品登録 v2 ─────────────────────────

# SuiteQL は 1 回最大 60 秒×4 リトライ。テンプレ DL を毎回待たせないようキャッシュ（仕様 §3）
@st.cache_data(ttl=600, show_spinner=False)
def _cached_choices() -> NC.Choices:
    with get_connection() as conn:
        return NC.load_choices(conn)


@st.cache_data(ttl=600, show_spinner=False)
def _cached_template() -> bytes:
    return FORM.build_template(_cached_choices())


@st.cache_data(ttl=3600, show_spinner=False)
def _cached_prefix_makers() -> dict[str, str]:
    with get_connection() as conn:
        return NC.load_prefix_makers(conn)


_ITEM_KEYS = ["page15_result", "page15_csv_bytes", "page15_csv_name", "page15_csv_n", "page15_log_msg",
              "page15_gen_issues", "page15_editor", "page15_csv_src", "page15_pending_n",
              "page15_skuid_blank", "page15_weight_fix", "page15_hs_retry_msg"]


def _content_hash(records) -> str:
    """表・行の内容ハッシュ（CSV が今の表から作られたか / 台帳に同じ内容を記録済みか の判定）。"""
    import json
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def _issues_df(issues) -> pd.DataFrame:
    return pd.DataFrame([{t("行"): i.row or "", "型番 / JAN": i.jan,
                          t("区分"): "❌ error" if i.level == "error" else "⚠️ warn",
                          t("内容"): i.message} for i in issues])


def _issue_table(issues, key: str) -> None:
    """error を先頭に並べ、error / warn / すべて で絞れる表（Boss 2026-11-10「warn の中から error を探すのが大変」）。"""
    n_err = sum(i.level == "error" for i in issues)
    labels = {"error": f"❌ error のみ（{n_err}）", "warn": f"⚠️ warn のみ（{len(issues) - n_err}）",
              "all": f"すべて（{len(issues)}）"}
    pick = st.radio(t("表示"), list(labels), index=0 if n_err else 2, horizontal=True, key=key,
                    format_func=lambda k: t(labels[k]), label_visibility="collapsed")
    shown = sorted((i for i in issues if pick == "all" or i.level == pick), key=lambda i: i.level != "error")
    st.dataframe(_issues_df(shown), use_container_width=True, hide_index=True)


def _search_image(jan: str) -> str:
    """CMS 画像検索（page22 と同じ）: PG キャッシュ → 楽天 API → kakaku。取れたらキャッシュへ。"""
    with get_connection() as conn:
        hit = image_search._load_cache(conn, [jan]).get(jan)
        if hit and hit.get("status") == "ok" and hit.get("image_url"):
            return hit["image_url"]
        status, _, size, err, url, source = image_search._fetch_image_for_jan(jan)
        image_search._upsert_cache(conn, jan, url, source if status == "ok" else None,
                                   status, size if status == "ok" else 0, None if status == "ok" else err)
        conn.commit()
    return url if status == "ok" else ""


def _process(data: bytes, choices: NC.Choices, want_weight: bool) -> dict:
    """read_upload → NST 既登録チェック → enrich。結果は dict で session_state へ。"""
    rows, issues = FORM.read_upload(data, choices)
    notes: list[str] = []
    ng = len({i.row for i in issues if i.level == "error" and i.row})
    if any(i.level == "error" and not i.row for i in issues):
        notes.append(t("ファイル全体のエラーがあります（下の表を確認）"))
    nst_rows, extra, stats = [], {}, {}
    nst_has: set[str] = set()
    bm_has: dict[str, str] = {}
    skipped_both = 0
    if rows:
        # 進捗バーの前の照会。斑马 API が遅いと数分かかり、表示が無いと「押しても反応しない」に見える（2026-11-11）
        with st.spinner(t("NST・斑马の登録済みを確認中…（斑马の応答が遅いと数分かかります。押し直すと最初からやり直しになります）")):
            with get_connection() as conn:
                exist, warns = NC.existing_jans([r[JAN_COL_NAME] for r in rows if r[JAN_COL_NAME]], conn=conn)
                codes_exist, code_warns = NC.existing_item_codes([REG.item_key(r) for r in rows], conn=conn)
            notes += code_warns
            notes += warns
            nst_has = {REG.item_key(r) for r in rows if r[JAN_COL_NAME] in exist or REG.item_key(r) in codes_exist}
            # 斑马の既存も先に見る（Boss 2026-11-10: 両方にあれば skip・片方だけなら無い方へ）
            try:
                client = banma_client.BanmaClient.from_env()
                with get_connection() as conn:
                    banma_client.ensure_token(conn, client)
                bm_has = BMR.find_skus(client, [REG.item_key(r) for r in rows])
            except Exception as e:  # noqa: BLE001 — 照会できなくても続行。登録時に 1 件ずつ再照会する
                bm_has = {}
                notes.append(t(f"斑马の既存照会に失敗（生成時に 1 件ずつ再確認します）: {type(e).__name__}: {e}"))
        both = [r for r in rows if REG.item_key(r) in nst_has and REG.item_key(r) in bm_has]
        for r in both:
            issues.append(FORM.Issue(0, REG.item_key(r), "warn", "NST・斑马とも登録済みのためスキップ（何もしません）"))
        skipped_both = len(both)
        rows = [r for r in rows if r not in both]
    if rows:
        try:
            prefix_makers = _cached_prefix_makers()
        except Exception as e:  # noqa: BLE001 — メーカーは jancode 会社名で続行
            prefix_makers = {}
            notes.append(f"NST メーカー（JAN プレフィックス）の読込に失敗・jancode 会社名だけで判定: {e}")
        meta_fetch = jan_web.make_meta_fetcher()   # jancode（403 で即停止・上限 40）→ 楽天（jancode 形式に整形）
        bar = st.progress(0.0, text=t("処理中…"))
        nst_rows, extra, more, stats = REG.enrich(
            rows, choices=choices, prefix_makers=prefix_makers,
            fetch_meta=meta_fetch, fetch_weight=jan_web.fetch_weight,
            classify_customs=hs_classify.classify_customs, want_weight=want_weight,
            on_progress=lambda d, n, msg: bar.progress(min(d / n, 1.0) if n else 1.0,
                                                       text=msg))
        bar.empty()
        issues += more
        if meta_fetch.state["blocked"]:
            notes.append(t("jancode.xyz にアクセス制限されたため途中から楽天で商品名を取りました（制限は最長 24 時間。"
                           "この間に処理し直しても jancode には 1 件しか行きません）"))
        for r in nst_rows:   # 片方だけ登録済みの印（生成時: NST 済 → CSV に出さない / 斑马済 → 斑马は SKUID だけ取る）
            k = REG.item_key(r)
            extra.setdefault(k, {})["nst_exists"] = k in nst_has
            extra[k]["bm_sku_id"] = bm_has.get(k, "")
        if nst_rows:
            with st.spinner(t("画像を探しています（jancode → 画像検索 → 待上图）…")):
                stats["images"] = REG.fill_images(nst_rows, extra, _search_image)
        ng += stats["ng"]
        if nst_rows and hs_ai.is_configured():
            with st.spinner(t("HS を AI でも判定中…")):
                issues += REG.cross_check_hs(nst_rows, extra, stats, hs_ai.judge)
        elif nst_rows:
            notes.append(t("GEMINI_API_KEY が無いため HS の AI 突合はしていません（規則の判定のみ）"))
    ok_jans = {REG.item_key(r) for r in nst_rows}
    warn = len({i.jan for i in issues if i.level == "warn"} & ok_jans)
    return {"nst": nst_rows, "extra": extra, "issues": issues, "stats": stats,
            "notes": notes, "choices": choices, "ok": len(nst_rows), "ng": ng, "warn": warn,
            "skipped_both": skipped_both}


def _register_banma(out_bm: list[dict], log_ok: bool) -> list:
    """斑马 API 登録（既存 SKU は skip するので押し直しても二重登録しない。skip でも SKUID は返る）。"""
    try:
        client = banma_client.BanmaClient.from_env()
        with get_connection() as conn:
            banma_client.ensure_token(conn, client)
    except Exception as e:  # noqa: BLE001
        st.session_state["page15_bm_result"] = {"error": f"斑马に接続できません（登録していません）: {type(e).__name__}: {e}"}
        return []
    bar = st.progress(0.0, text=t("斑马に登録中…"))
    with get_connection() as conn:
        record = (lambda code, r: REG.record_banma(conn, code, r)) if log_ok else None
        results = BMR.register_all(client, out_bm, record=record,
                                   on_progress=lambda d, n, c: bar.progress(d / n, text=f"{d}/{n} {c}"))
    bar.empty()
    st.session_state["page15_bm_result"] = {"rows": results, "logged": log_ok}
    return results


def _generate(edited: pd.DataFrame, res: dict, to_banma: bool = True) -> None:
    """確認表 → 台帳 → 斑马へ API 登録（1 件ずつ・結果を台帳へ）→ 斑马 SKUID を入れて NST CSV。

    NST の SKUID（custitem_item_skuid）= 斑马 SKUID（在庫同期用・2026-11-09 実品で一致確認）。斑马に登録しないと
    取れないので、斑马の後に CSV を作る（Boss 2026-11-09）。取れなかった品は SKUID 空で出して件数を出す。
    to_banma=False（画面のチェックを外した時・Boss 2026-11-10）は斑马に触らず NST CSV だけ・SKUID と商品ランクは空。
    error があれば何も出さない。"""
    for key in ("page15_csv_bytes", "page15_csv_name", "page15_csv_n", "page15_csv_src", "page15_bm_result",
                "page15_skuid_blank"):
        st.session_state.pop(key, None)
    out_nst, out_bm, sources, gen_issues = REG.from_frame(
        edited.to_dict("records"), res["nst"], res["extra"], choices=res.get("choices"))
    review = REG.registration_issues(out_nst, choices=res.get("choices"))
    gen_issues += review
    st.session_state["page15_gen_issues"] = gen_issues
    st.session_state["page15_pending_n"] = len({i.jan for i in review})
    if any(i.level == "error" for i in gen_issues):
        st.error(t("❌ 入力エラーがあります。確認表を修正して再生成してください（部分出力はしません）"))
        return
    if not out_nst:
        st.error(t("❌ 生成できる行がありません"))
        return
    for row in out_nst:   # 商品ランク: 斑马にも登録 = NEW / NST のみ = 空（Boss 2026-11-10・テンプレには無い）
        row["商品ランク"] = "NEW" if to_banma else ""

    # 台帳は append-only。同じ内容の押し直しで同じ行を重ねない（このセッション内）
    log_key = _content_hash(out_nst)
    log_ok = st.session_state.get("page15_logged") == log_key
    if log_ok:
        st.session_state["page15_log_msg"] = ("ok", t("台帳は記録済み（同じ内容のため再記録しません）"))
    else:
        bm_lists = [JBM.nst_to_bm_row(r, image_url=r.get(REG.COL_IMAGE, "")) for r in out_bm]
        try:
            with get_connection() as conn:
                n_log, err = REG.write_register_log(conn, out_nst, bm_lists, sources)
        except Exception as e:  # noqa: BLE001 — 接続自体の失敗
            n_log, err = 0, f"{type(e).__name__}: {e}"
        st.session_state["page15_log_msg"] = (
            ("error", t(f"台帳への記録に失敗: {err}")) if err
            else ("ok", t(f"台帳 nst.item_register_log に {n_log} 件記録")))
        log_ok = not err
        if log_ok:
            st.session_state["page15_logged"] = log_key

    # 斑马 API 登録（既存 SKU は skip するので押し直しても二重登録しない。skip でも SKUID は返る）
    results = _register_banma(out_bm, log_ok) if to_banma else []   # 斑马済は register_one が skip して SKUID だけ返す
    sku_by_code = {code: str(r.get("sku_id") or "") for code, r in results}
    for row in out_nst:
        row["SKUID"] = sku_by_code.get(REG.item_key(row), "")
    # 斑马に出さないときは SKUID 空が正常 → 件数警告は出さない
    st.session_state["page15_skuid_blank"] = (sum(not row["SKUID"] for row in out_nst
                                                  if not res["extra"].get(REG.item_key(row), {}).get("nst_exists"))
                                              if to_banma else 0)
    # NST 登録済み（斑马だけ未登録）の品は CSV に出さない（Boss 2026-11-10）
    csv_rows = [r for r in out_nst if not res["extra"].get(REG.item_key(r), {}).get("nst_exists")]
    st.session_state["page15_csv_n"] = len(csv_rows)
    if csv_rows:
        # CSV を直接ダウンロード（Boss 2026-11-10）。登録者名 = 商品担当者の名前（番号なし・複数なら ・ で連結）
        owners = dict.fromkeys(re.sub(r"^\d+\s*", "", str(r.get("商品担当者") or "")).strip() for r in csv_rows)
        st.session_state["page15_csv_bytes"] = REG.build_nst_registration_csv(csv_rows)
        st.session_state["page15_csv_name"] = TPL.dated_filename(registrant="・".join(o for o in owners if o))
    st.session_state["page15_csv_src"] = _content_hash(edited.to_dict("records"))


def _missing_fix(edited: pd.DataFrame) -> pd.DataFrame:
    """アイテム名（どこからも取れない）・重量（ネットに無い）が空の品だけの入力表（Boss 2026-11-10「CMS 上で直接填写」）。
    値は確認表に重ねて生成に使う。"""
    name, wt = "アイテム名", REG.COL_PKG_WEIGHT
    if name not in edited.columns or wt not in edited.columns:
        return edited
    blank = lambda c: edited[c].fillna("").astype(str).str.strip() == ""   # noqa: E731
    miss = blank(name) | blank(wt)
    if not miss.any():
        return edited
    st.subheader(t(f"✍️ 手入力が必要な項目（{int(miss.sum())} 件：アイテム名 {int(blank(name).sum())} · 重量 {int(blank(wt).sum())}）"))
    st.caption(t("ここに入れれば再アップロード不要です。アイテム名 = jancode・楽天・Yahoo のどこにも無かった品（最大 60 字）。"
                 "重量 = 単位 g・不明なら 1 も可（斑马の重量に使います）"))
    fixed = st.data_editor(
        edited.loc[miss, ["型番", name, wt]], key="page15_weight_fix", hide_index=True,
        num_rows="fixed", use_container_width=True, disabled=["型番"],
        column_config={name: st.column_config.TextColumn(f"{name} ✍️", max_chars=REG.NAME_MAX),
                       wt: st.column_config.TextColumn(f"{wt} ✍️", help=t("g（整数または小数）。不明なら 1"))})
    out = edited.copy()
    out.loc[fixed.index, [name, wt]] = fixed[[name, wt]]
    return out


def _hs_retry(res: dict, records: list[dict]) -> None:
    """HS が空の品だけ AI に再判定させるボタン（Gemini 429 などで空のまま残った分・Boss 2026-11-10）。
    名前は確認画面の今の値（手入力したアイテム名も使う）。結果は res["extra"] に入り、確認表の HS・通関英文名に出る。"""
    if st.session_state.get("page15_hs_retry_msg"):
        st.info(st.session_state["page15_hs_retry_msg"])
    if not hs_ai.is_configured():
        return
    names = {str(r.get("型番") or ""): str(r.get("アイテム名") or "").strip() for r in records}
    blank = [{**r, "アイテム名": names.get(REG.item_key(r)) or r.get("アイテム名", "")} for r in res["nst"]
             if not res["extra"].get(REG.item_key(r), {}).get(REG.COL_HS)
             and (names.get(REG.item_key(r)) or r.get("アイテム名"))]
    if not blank:
        return
    if st.button(t(f"🤖 HS を AI で再判定（HS が空の {len(blank)} 件だけ）"), key="page15_hs_retry"):
        with st.spinner(t("HS を AI で判定中…")):
            more = REG.cross_check_hs(blank, res["extra"], res["stats"], hs_ai.judge)
        res["issues"] += more
        left = sum(not res["extra"].get(REG.item_key(r), {}).get(REG.COL_HS) for r in blank)
        st.session_state["page15_hs_retry_msg"] = t(f"再判定: {len(blank)} 件中 {len(blank) - left} 件に HS が入りました"
                                                     + (f"（{left} 件はまだ空・時間をおいて再度）" if left else ""))
        st.rerun()


def _render_result(res: dict) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("OK", res["ok"])
    c2.metric("NG", res["ng"], delta_color="inverse")
    c3.metric(t("警告あり"), res["warn"])
    s = res["stats"]
    if s:
        st.caption(
            f"jancode ok={s['jancode_ok']} 楽天={s.get('name_rakuten', 0)} not_found={s['jancode_not_found']} "
            f"error={s['jancode_error']} · メーカー NST={s['maker_nst']} "
            f"jancode={s['maker_jancode']} 手入力={s.get('maker_manual', 0)} 空={s['maker_none']} · アイテム名切詰め={s['name_cut']} 手入力={s['name_manual']} · "
            f"HS ok={s['hs_ok']} 判定不可={s['hs_ng']} AI一致={s.get('ai_agree', '-')} "
            f"AI採用={s.get('ai_changed', '-')} AI失敗={s.get('ai_error', '-')} · 重量 取得={s['weight_hit']} "
            f"なし={s['weight_miss']} 失敗={s['weight_error']} 調べず={s['weight_skipped']}"
            + (" · 画像 " + " ".join(f"{k}={v}" for k, v in s["images"].items()) if s.get("images") else ""))
    for n in res["notes"]:
        st.warning(n)
    ex = res["extra"]
    only_nst = [k for k, v in ex.items() if v.get("nst_exists")]
    only_bm = [k for k, v in ex.items() if v.get("bm_sku_id") and not v.get("nst_exists")]
    if res.get("skipped_both") or only_nst or only_bm:
        st.info(t(f"登録済みの扱い（同じ型番 / JAN）: NST・斑马とも済 → スキップ {res.get('skipped_both', 0)} 件 / "
                  f"NST だけ済 → 斑马にだけ登録 {len(only_nst)} 件 / 斑马だけ済 → NST CSV にだけ出す {len(only_bm)} 件"
                  "（斑马 SKUID は既存のものを入れる）"))
    if res["issues"]:
        n_err = sum(i.level == "error" for i in res["issues"])
        if n_err:
            st.error(t(f"❌ error {n_err} 件の行は出力しません（下の「処理時の問題」を開いて確認）"))
        with st.expander(t(f"⚠️ 処理時の問題 {len(res['issues'])} 件（error {n_err} 件の行は出力しません）"),
                         expanded=False):
            _issue_table(res["issues"], "page15_flt_process")
    if not res["nst"]:
        st.error(t("❌ 出力できる行がありません"))
        return

    st.subheader(t("📋 確認・修正（🤖 = 自動で埋めた値。白い列だけ直せます）"))
    df = pd.DataFrame(REG.to_frame(res["nst"], res["extra"]))
    auto_label = {"アイテム名": "アイテム名 🤖", "メーカー名": "メーカー名 🤖",
                  "通関英文名": "通関英文名 🤖", "HS": "HS 🤖"}
    col_config = {c: st.column_config.TextColumn(lbl) for c, lbl in auto_label.items()}
    col_config["通関英文名"] = st.column_config.TextColumn(
        "通関英文名 🤖", max_chars=hs_classify.MAX_NAME_EN, help=t("斑马 英文名称（最大 76 字）"))
    col_config["アイテム名"] = st.column_config.TextColumn(
        "アイテム名 🤖", max_chars=REG.NAME_MAX, help=t("NST アイテム名 = 斑马 中文名称（最大 60 字）"))
    col_config[REG.COL_URL] = st.column_config.LinkColumn(REG.COL_URL)
    col_config["画像URL"] = st.column_config.TextColumn(
        "画像URL 🤖", help=t("斑马の商品画像（https）。jancode → 画像検索 → 待上图 の順で自動。差し替えるなら https の URL を貼る"))
    for col in FORM.COL_CARTON_DIMS:
        col_config[col] = st.column_config.TextColumn(col, help=t("整箱の実測値。空なら NST 取込用に 1 を入れます（仮値・非実測）"))
    for col, values in FORM.FIELD_CHOICES.items():
        if col in df.columns:
            col_config[col] = st.column_config.SelectboxColumn(col, options=["", *values])
    choices = res.get("choices")
    if choices:
        for col, values in (("大分類", choices.large),
                            ("中分類", tuple(dict.fromkeys(m for ms in choices.middle_by_large.values() for m in ms))),
                            ("仕入先1：仕入先", choices.suppliers), ("商品担当者", choices.owners)):
            col_config[col] = st.column_config.SelectboxColumn(col, options=["", *values])
    edited = st.data_editor(
        df, use_container_width=True, hide_index=True, num_rows="fixed",
        disabled=[c for c in df.columns if c not in REG.EDITABLE],
        column_config=col_config, key="page15_editor")

    st.info(t("ブランド・メーカーは不明なら空欄。包装 3 辺の不足分は 1 cm の既定仮値（非実測）です。パッケージ重量(g) がネットで見つからない品は手入力必須（不明なら 1 も可）。取得・手入力済みの値は保持します。"
              "画像の出典が「待上图」の品は、斑马に待上图の画像で登録されます。"))
    edited = _missing_fix(edited)
    records = edited.to_dict("records")
    _hs_retry(res, records)
    current_nst, _, _, current_issues = REG.from_frame(records, res["nst"], res["extra"], choices=choices)
    current_issues += REG.registration_issues(current_nst, choices=choices)
    if current_issues:
        n_cur_err = sum(i.level == "error" for i in current_issues)
        if n_cur_err:
            st.error(t(f"❌ error {n_cur_err} 件があり、このままでは生成できません（下を開いて確認）"))
        with st.expander(t(f"✍️ 全商品の必須・条件項目チェック（{len(current_issues)} 件・error {n_cur_err} 件）"),
                         expanded=False):
            st.warning(t("要確認項目があります。空のカートン 3 辺・入数は 1 の仮値で出します。分類・税率は適用と値を確認してください。未解決のまま生成したファイルは確認用です。"))
            _issue_table(current_issues, "page15_flt_check")
    else:
        st.success(t("NST テンプレ項目チェックで未入力・要確認項目はありません"))
    with st.expander(t("📑 原本の全 71 項目・入力条件")):
        st.dataframe(pd.DataFrame(REG.nst_field_rules()), use_container_width=True, hide_index=True)
    if any(not str(r.get(JAN_COL_NAME) or "").strip() for r in records):
        st.subheader(t("✍️ JAN なし商品の待手入力"))
        needs = REG.manual_input_needs(records)
        if needs:
            st.info(t("不足している欄だけを表示しています。確認済みの値を手入力してください。"
                      "ブランド・メーカーは不明なら空欄のまま。包装 3 辺の空欄は 1 cm の仮値で出力します（非実測）。パッケージ重量(g) は手入力必須（不明なら 1 も可）。入力後、この一覧は更新されます。"))
            st.dataframe(pd.DataFrame(needs), use_container_width=True, hide_index=True)
        else:
            st.success(t("JAN なし商品の待手入力項目はありません（入力済みの内容は確認してください）"))

    st.divider()
    st.caption(t("確認が終わったら押してください。チェックありなら斑马へ API で登録し、取れた斑马 SKUID を入れて NST 取込 CSV を作ります（斑马に既にある SKU は登録せず SKUID だけ取ります）。チェックなしなら NST CSV だけ"))
    to_banma = st.checkbox(t("斑马にも登録する（斑马 SKUID を NST CSV に入れる・商品ランク = NEW）"), value=True,
                           key="page15_to_banma",
                           help=t("外すと NST 取込 CSV だけ作ります。斑马には登録せず、SKUID・商品ランクは空のまま"))
    if st.button(t("📦 生成（NST.csv）＋ 斑马に登録") if to_banma else t("📦 生成（NST.csv のみ）"),
                 type="primary", key="page15_gen"):
        _generate(edited, res, to_banma=to_banma)
    gen_issues = st.session_state.get("page15_gen_issues") or []
    if gen_issues:
        _issue_table(gen_issues, "page15_flt_gen")
    csv_bytes = st.session_state.get("page15_csv_bytes")
    if st.session_state.get("page15_csv_src") and not csv_bytes and st.session_state.get("page15_csv_n") == 0:
        st.info(t("NST CSV に出す品はありません（すべて NST に登録済み）"))
    if csv_bytes and st.session_state.get("page15_csv_src") != _content_hash(edited.to_dict("records")):
        # 画面は編集後の値なのに CSV は編集前 → 気づけないので伏せる（セット品タブと同じ対策）
        st.warning(t("⚠️ 表を編集しました。もう一度「📦 生成」を押してください（前回の CSV は隠しています）"))
        csv_bytes = None
    if csv_bytes:
        if st.session_state.get("page15_pending_n"):
            st.warning(t(f"⚠️ {st.session_state['page15_pending_n']} 件に登録前の要確認項目があります。上の「全商品の必須・条件項目チェック」を確認してください。"))
        n = st.session_state.get("page15_csv_n", 0)
        if st.session_state.get("page15_skuid_blank"):
            st.error(t(f"❌ SKUID（斑马）が空の品 {st.session_state['page15_skuid_blank']} 件。斑马に登録できていないため在庫同期されません。"
                       "原因を直して「📦 生成」を押し直してください（登録済みの品は SKUID だけ取り直します）"))
        st.download_button(
            t(f"⬇️ NST CSV をダウンロード（{n} 件）"), data=csv_bytes,
            file_name=st.session_state.get("page15_csv_name") or TPL.dated_filename(),
            mime="text/csv", type="primary", key="page15_csv_dl")
        level, msg = st.session_state.get("page15_log_msg") or ("ok", "")
        (st.error if level == "error" else st.success)(msg)
    bm = st.session_state.get("page15_bm_result")
    if bm:
        st.subheader(t("🦓 斑马 登録結果"))
        if bm.get("error"):
            st.error(bm["error"])
        else:
            rows = bm["rows"]
            cnt = {k: sum(r["status"] == k for _, r in rows) for k in ("ok", "partial", "skipped", "fail")}
            msg = (f"ok={cnt['ok']} 要確認={cnt['partial']} 既存のため未登録={cnt['skipped']} 失敗={cnt['fail']}"
                   f"（計 {len(rows)} 件）")
            (st.error if cnt["fail"] else st.warning if cnt["partial"] or cnt["skipped"] else st.success)(msg)
            if not bm.get("logged"):
                st.warning(t("台帳に記録できなかったため、斑马の登録結果は台帳に残っていません（下の表を保存してください）"))
            st.dataframe(pd.DataFrame([{"型番": c, t("結果"): r["status"], "SKUID": r.get("sku_id", ""),
                                        "SPUID": r.get("spu_id", ""), t("内容"): r.get("message", "")}
                                       for c, r in rows]), use_container_width=True, hide_index=True)
            if cnt["fail"]:
                st.info(t("失敗分は原因を直してもう一度「📦 生成」を押せば、未登録の品だけ登録されます"))
            # NST 済・今回斑马に登録した品は SKUID が NST に入らない（新規登録 CSV の対象外）
            # → 型番 + SKUID の NST 更新 CSV を出す（2026-11-10 は手入力の提示だけ → Boss 2026-11-12 CSV に）
            nst_only = [(c, r.get("sku_id", "")) for c, r in rows
                        if res["extra"].get(c, {}).get("nst_exists") and r.get("sku_id")]
            if nst_only:
                st.warning(t(f"⚠️ NST 登録済みの {len(nst_only)} 件は斑马 SKUID が NST に入っていません。"
                             "下の SKUID 更新 CSV を NST に取り込んでください（在庫同期用）"))
                st.dataframe(pd.DataFrame(nst_only, columns=["型番", "斑马 SKUID"]),
                             use_container_width=True, hide_index=True)
                st.download_button(t(f"⬇️ NST SKUID 更新 CSV（{len(nst_only)} 件）"), data=SF.update_csv(nst_only),
                                   file_name=TPL.dated_filename(registrant="SKUID更新"), mime="text/csv",
                                   key="page15_skuid_update_dl")


def _render_item_tab() -> None:
    """⚠️ st.stop() 禁止（スクリプト全体が止まり他タブが消える）。抜けるときは return。"""
    st.caption(t("新品登録 · テンプレ DL → 記入 → UL → 確認 → NST 取込 CSV（手でアップロード）＋ 斑马へ API で自動登録"))
    with st.expander(t("📌 使い方"), expanded=False):
        st.markdown(t(
            "- **入力項目**（NST 原本の必須項目だけ）：JAN / 型番 / 商品原価 / 仕入先（完全名称）/ 商品担当者 / "
            "大分類・中分類（NST 原本の選択肢）/ カートン 3 辺・入数（パッケージ 3 辺は任意・空ならネットで調べる。"
            "jancode に商品名が無い JAN は「アイテム名（任意）」も記入）\n"
            "- **メーカー名は入力しません**：自動で埋め、確認表で人が確認・修正します\n"
            "- **商品ランクは入力しません**：斑马にも登録なら NEW、NST のみなら空\n"
            "- **SKUID は入力しません**：生成時に斑马へ登録して取れた斑马 SKUID を NST CSV に入れます（在庫同期用）\n"
            "- **JAN の無い商品**：JAN 欄は空、末尾の型番 / SKU とアイテム名は手入力必須（ETERNAL-BONSAI の既定 6 型番は JAN 欄に直接入力可）。ブランド・メーカーは不明なら空欄。包装 3 辺が空なら 1 cm の仮値（非実測）、重量が取れなければ確認表で手入力必須（1 も可）。HS・通関英文名は判定結果が空なら確認表で手入力\n"
            "- **自動で埋める**：アイテム名（jancode 商品名・60 字）/ メーカー名（NST 同プレフィックス → jancode）/ "
            "型番は入力値（空なら JAN）/ 取扱中・輸出事業・輸出専用・日本円 / 税率（食品=★）/ 斑马の HS・通関英文名（≤76 字）\n"
            "- **NST 出力の JAN**：実 JAN が無い商品は原本指定に従い型番を出力（内部の実 JAN・台帳は空のまま）\n"
            "- **網調べ**は空欄だけ埋める（手入力は上書きしない）。出典 URL を表で確認\n"
            "- **NST に登録済みの JAN / 型番** はエラーで外す\n"
            "- **出力**：NST CSV（原本の全 71 列・列名と順序を保持、空列も削除しません）＋ 斑马へ API 登録（中文名称 = アイテム名・画像は jancode → 画像検索 → 待上图）"
        ))
    try:
        choices = _cached_choices()
        template = _cached_template()
    except Exception as e:  # noqa: BLE001
        st.error(t(f"❌ 選択肢（仕入先・分類）の読込に失敗: {type(e).__name__}: {e}"))
        return
    for w in choices.warnings:
        st.warning(w)
    st.download_button(t("📥 テンプレートをダウンロード"), data=template,
                       file_name=FORM.template_filename(), mime=_XLSX_MIME,
                       key="page15_tpl_dl")

    uploaded = st.file_uploader(t("📤 記入済みテンプレ（xlsx）をアップロード"), type=["xlsx"],
                                key="page15_upload")
    want_weight = True   # Boss 2026-11-09: 包装 3 辺・重量は任意 → 常にネットで調べる
    st.caption(t("パッケージ 3 辺・重量が空欄の行はネットで調べます（NETde卸）"))
    col_r, col_c = st.columns([1, 1])
    run = col_r.button(t("▶ 処理する"), type="primary", disabled=uploaded is None,
                       key="page15_run", use_container_width=True)
    if col_c.button(t("🗑️ 清除结果"), key="page15_item_clear", use_container_width=True):
        for k in _ITEM_KEYS:
            st.session_state.pop(k, None)
        st.session_state.pop("page15_upload", None)
        st.rerun()

    if uploaded is None:
        st.info(t("📤 記入済みテンプレをアップロードして「▶ 処理する」"))
        return
    data = uploaded.getvalue()
    h = hashlib.sha256(data).hexdigest()
    res = st.session_state.get("page15_result")
    # run が True なのはボタンを押した回の rerun だけ → 押されたら毎回処理（jancode 403 後の再試行のため）
    if run:
        for k in _ITEM_KEYS:
            st.session_state.pop(k, None)
        try:
            res = _process(data, choices, want_weight)
        except Exception as e:  # noqa: BLE001 — 壊れた xlsx・PG 接続失敗でも他タブを描画する
            st.error(t(f"❌ 処理に失敗: {type(e).__name__}: {e}"))
            return
        res["key"] = (h, want_weight)
        st.session_state["page15_result"] = res
    if not res:
        return
    if res["key"][0] != h:
        st.warning(t("ファイルが変わりました。「▶ 処理する」を押してください（前回の結果は隠しています）"))
        return
    _render_result(res)


with tab_item:
    _render_item_tab()


# ───────────────────────── tab 🔎 SKUID 空チェック ─────────────────────────
# NST で SKUID（斑马 SKUID・在庫同期用）が空の品 → 斑马の同じ型番 / JAN の SKUID、無ければ斑马に登録して取る
# → NST 更新 CSV（型番 + SKUID）。NST へは書かない（CSV は人が取り込む）。Boss 2026-11-12

_SK_KEYS = ["p15s_result", "p15s_prep", "p15s_reg", "p15s_editor"]
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
            stats["images"] = REG.fill_images(need_img, extra, _search_image)
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
        st.session_state["p15s_reg"] = {"error": f"斑马に接続できません（登録していません）: {type(e).__name__}: {e}"}
        return
    bar = st.progress(0.0, text=t("斑马に登録中…"))
    with get_connection() as conn:   # 台帳に行がある品（page15 で登録失敗した品など）はその行に結果を書く
        results = BMR.register_all(client, rows, record=lambda code, r: REG.record_banma(conn, code, r),
                                   on_progress=lambda d, n, c: bar.progress(d / n, text=f"{d}/{n} {c}"))
    bar.empty()
    st.session_state["p15s_reg"] = {"rows": results}


# ── 🩺 斑马登録内容チェック（Boss 2026-11-12「三边・重量・清关英文・HScode・图片 为空的也检查，CMS 上修改后回传到斑马」）
# 書き戻しは shared/banma_fix（報関 = SKU 単位 API / 三辺・重量・画像 = SPU 丸ごと PUT・描述/包材ありの SPU は書かない）

_CC_KEYS = ["p15c_result", "p15c_reg", "p15c_editor"]
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
    if rest:
        bar = st.progress(0.0, text=t("斑马の報関情報を照会中…"))
        for i, sid in enumerate(rest, 1):
            try:
                customs[sid] = BF.get_customs(client, sid)
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない。その品は報関を判定しない
                cus_err[sid] = f"{type(e).__name__}: {e}"
            bar.progress(i / len(rest), text=f"{i}/{len(rest)}")
        bar.empty()
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
            try:
                url = _search_image(str(it["jan"]))
            except Exception:  # noqa: BLE001
                url = ""
            if url.startswith("https://"):
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
    edited = st.data_editor(df, key="p15c_editor", hide_index=True, num_rows="fixed", use_container_width=True,
                            disabled=["型番", t("アイテム名"), t("空の項目"), t("提案"), t("備考")],
                            column_config={"英文名": st.column_config.TextColumn(max_chars=hs_classify.MAX_NAME_EN),
                                           "画像URL": st.column_config.LinkColumn("画像URL")})
    changes, errs = _cc_changes(rows, edited.to_dict("records"))
    for code, e in errs[:20]:
        st.error(f"❌ {code}: {e}")
    n_p = sum(bool(c["prod"]) for c in changes)
    n_c = sum(bool(c["cus"]) for c in changes)
    if changes and not errs and st.button(t(f"🦓 斑马に反映（三辺・重量・画像 {n_p} 件 · 報関 {n_c} 件）"), type="primary",
                                          key="p15c_write"):
        st.session_state["p15c_reg"] = _cc_write(changes)
    reg = st.session_state.get("p15c_reg")
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
                               mime=_XLSX_MIME, key="p15c_xlsx")
        if any(r.get("excel") and "Image" in r["excel"]["prod"] for r in reg):
            st.info(t("画像を API で書かなかった品は斑马の画面で入れてください（上の表の skipped）"))


def _render_skuid_tab() -> None:
    """⚠️ st.stop() 禁止（他タブが消える）。抜けるときは return。"""
    st.caption(t("🔎 NST で SKUID（斑马 SKUID・在庫同期用）が空の品を探し、斑马の同じ型番 / JAN の SKUID を取る。"
                 "斑马に無ければ斑马に登録してから取る → NST 更新 CSV（型番 + SKUID）を手で NST に取り込む。"
                 "🩺 SKUID がある品は斑马の三辺・重量・画像・HS・英文名の空を探し、CMS で直して斑马へ書き戻す"))
    d = SF.DEFAULTS
    c1, c2, c3 = st.columns(3)
    period = c1.selectbox(t("作成日"), list(SF.PERIODS), index=list(SF.PERIODS).index(d["period"]), key="p15s_period")
    depts = c2.multiselect(t("部門"), SF.DEPTS, default=list(d["depts"]), key="p15s_depts")
    handlings = c3.multiselect(t("取扱区分"), SF.HANDLINGS, default=list(d["handlings"]), key="p15s_handlings")
    c4, c5, c6 = st.columns(3)
    states = c4.multiselect(t("状態"), SF.STATES, default=list(d["states"]), key="p15s_states")
    ranks = c5.multiselect(t("商品ランク"), SF.RANKS, default=list(d["ranks"]), key="p15s_ranks")
    by_jan = c6.radio(t("斑马での照合"), [True, False], index=0, key="p15s_by_jan", horizontal=True,
                      format_func=lambda v: t("型番 + JAN") if v else t("型番のみ"),
                      help=t("型番で見つからなければ JAN（斑马の単品 SKU コード = JAN）でも探す"))
    c7, c8 = st.columns(2)
    source = c7.radio(t("斑马の照会元"), list(_SK_SOURCES), index=0, key="p15s_source",
                      format_func=lambda k: t(_SK_SOURCES[k]))
    allow_dup = c8.checkbox(t("同じ斑马 SKUID を複数の品に入れる"), value=d["allow_dup"], key="p15s_allow_dup",
                            help=t("既定は入れない（1 つの斑马 SKU に NST の 2 品をつなぐと在庫同期が崩れる）。"
                                   "入れると「重複で保留」の品も CSV に出します"))
    opts = {"period": period, "depts": tuple(depts), "handlings": tuple(handlings), "states": tuple(states),
            "ranks": tuple(ranks), "by_jan": by_jan, "source": source}
    c9, c10 = st.columns(2)
    checks = c9.multiselect(t("🩺 斑马の検査項目"), BF.CHECKS, default=list(BF.CHECKS), key="p15c_checks")
    placeholder = c10.checkbox(t("仮値も空とみなす（重量 0/1/10/100/200/300/500g・三辺 1×1×1/10×10×10・待上图の画像）"),
                               value=True, key="p15c_placeholder", help=t("page46 と同じ口径（Boss 2026-10-07〜09）"))
    b1, b2, b3 = st.columns(3)
    if b1.button(t("🔍 SKUID 空をチェックする"), type="primary", key="p15s_run", use_container_width=True):
        for k in _SK_KEYS:
            st.session_state.pop(k, None)
        try:
            st.session_state["p15s_result"] = _skuid_check(opts)
        except Exception as e:  # noqa: BLE001 — 照会失敗は「斑马に無し」にしない
            st.error(t(f"❌ チェックに失敗（何も出力していません）: {type(e).__name__}: {e}"))
    if b2.button(t("🩺 斑马の登録内容をチェックする"), type="primary", key="p15c_run", use_container_width=True):
        for k in _CC_KEYS:
            st.session_state.pop(k, None)
        try:
            st.session_state["p15c_result"] = _content_check(opts, tuple(checks), placeholder)
        except Exception as e:  # noqa: BLE001
            st.error(t(f"❌ チェックに失敗（何も書いていません）: {type(e).__name__}: {e}"))
    if b3.button(t("🗑️ 清除结果"), key="p15s_clear", use_container_width=True):
        for k in _SK_KEYS + _CC_KEYS:
            st.session_state.pop(k, None)
        st.rerun()
    res, cres = st.session_state.get("p15s_result"), st.session_state.get("p15c_result")
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

    reg = st.session_state.get("p15s_reg") or {}
    if misses:
        st.subheader(t(f"🦓 斑马に無し（{len(misses)} 件）→ 斑马に登録してから SKUID を取る"))
        prep = st.session_state.get("p15s_prep")
        if not prep:
            st.dataframe(pd.DataFrame([{"型番": m["code"], "JANコード": m["jan"], t("アイテム名"): m["name"],
                                        t("作成日"): m.get("created"), t("商品ランク"): m.get("item_rank") or ""}
                                       for m in misses]), use_container_width=True, hide_index=True)
            if st.button(t(f"📝 登録の準備（{len(misses)} 件・page15 の台帳 → NST の値・HS と画像を自動で埋める）"),
                         key="p15s_prep_btn"):
                try:
                    st.session_state["p15s_prep"] = _skuid_prepare(misses)
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
                df, key="p15s_editor", hide_index=True, num_rows="fixed", use_container_width=True,
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
                                     key="p15s_reg_btn"):
                _skuid_register(ok_rows)
                reg = st.session_state.get("p15s_reg") or {}
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
                           key="p15s_csv_dl")
        st.caption(t("NST の CSV インポート（更新・型番で照合）で取り込んでください。斑马に無い品は登録後にこの CSV に加わります"))
    else:
        st.info(t("CSV に出す品はまだありません"))


with tab_skuid:
    _render_skuid_tab()


# ───────────────────────── tab 📦 セット品登録 (原生) ─────────────────────────

_BUNDLE_LINE_RE = re.compile(r"^\s*(\d{8,14})\s*[_,\-\s]\s*(\d{1,2})\s*$")


def _parse_bundle_lines(raw: str) -> tuple[list[dict], list[str]]:
    """解析 JAN_数量 多行（如 `4901234567890_3`），返回 (items, errors)。"""
    items: list[dict] = []
    errors: list[str] = []
    for i, line in enumerate((raw or "").splitlines(), 1):
        if not line.strip():
            continue
        m = _BUNDLE_LINE_RE.match(line)
        if not m:
            errors.append(f"第 {i} 行格式错误：「{line}」(期望 JAN_数量)")
            continue
        jan, qty = m.group(1), int(m.group(2))
        if qty < 1:
            errors.append(f"第 {i} 行数量必须 ≥1：「{line}」")
            continue
        items.append({"line": i, "jan": jan, "qty": qty, "bundle_sku": f"{jan}_{qty}"})
    return items, errors


def _fetch_internal_ids(conn, jans: list[str]) -> dict[str, dict]:
    if not jans:
        return {}
    placeholder = ",".join(["%s"] * len(jans))
    rows = conn.execute(
        f"SELECT jan, internal_id, display_name FROM nst.item_master_raw "
        f"WHERE jan IN ({placeholder})",
        jans,
    ).fetchall()
    return {r["jan"]: dict(r) for r in rows}


with tab_bundle:
    st.caption(t(
        "セット品登録 · 输入 JAN_数量 多行 → 从 PG 自动拉 Internal ID → "
        "生成 NST 父/子组合货品 CSV（NetSuite 上传）"
    ))

    with st.expander(t("📌 セット品登録 说明"), expanded=False):
        st.markdown(t(
            "- **输入格式**：每行一个 `JAN_数量`（例如 `4901234567890_3` 表示该 JAN 配 3 个）\n"
            "- **数据源**：Internal ID + 日文品名 自动从 `nst.item_master_raw` 取（不再上传 NST item.xls）\n"
            "- **输出**：NST 父组合货品 CSV + 子组合货品 CSV（ZIP 打包）\n"
            "  - 父表：外部ID / 名称（`<JAN>_<qty>`）\n"
            "  - 子表：外部ID / 父记录 / 内部ID / UPC Code / 价格\n"
            "- **斑马(BM) 组合品 xlsx**：本 tab 暂不出（需中英文品名），仍走「📜 旧 HTML 版」tab（JD 出力は廃止）"
        ))

    bundle_text = st.text_area(
        t("セット品リスト（每行 `JAN_数量`）"),
        height=200,
        placeholder="4901234567890_3\n4905678901234_2\n...",
        key="page15_bundle_text",
    )

    _BUNDLE_RESULT_KEYS = [
        "page15_bundle_items", "page15_bundle_parse_errors", "page15_bundle_pg_map",
        "page15_bundle_zip", "page15_bundle_zip_n",
        # 結果を作った元テキスト。結果を消すときは必ず一緒に消す
        # （残すと「入力を変えた」判定が次回も誤爆する）
        "page15_bundle_src",
    ]

    col_b1, col_b2 = st.columns([1, 1])
    with col_b1:
        btn_bundle_query = st.button(t("🔍 查询 PG 并预览"), type="primary",
                                     disabled=not bundle_text, key="page15_bundle_query",
                                     use_container_width=True)
    with col_b2:
        if st.button(t("🗑️ 清除结果"), key="page15_bundle_clear",
                     use_container_width=True,
                     help=t("清除上次查询结果和 セット品 输入，准备下一批")):
            for k in _BUNDLE_RESULT_KEYS:
                st.session_state.pop(k, None)
            st.session_state.pop("page15_bundle_text", None)
            st.rerun()

    if btn_bundle_query:
        # 修「第二次没法操作」：开新查询前清旧残留
        for k in _BUNDLE_RESULT_KEYS:
            st.session_state.pop(k, None)
        items, parse_errors = _parse_bundle_lines(bundle_text)
        st.session_state["page15_bundle_items"] = items
        st.session_state["page15_bundle_parse_errors"] = parse_errors
        # 「この結果はどのテキストから作ったか」を一緒に残す（下の突合に使う）
        st.session_state["page15_bundle_src"] = bundle_text

        if items:
            with get_connection() as conn:
                pg_map = _fetch_internal_ids(conn, [it["jan"] for it in items])
            st.session_state["page15_bundle_pg_map"] = pg_map

    items: list[dict] = st.session_state.get("page15_bundle_items") or []
    parse_errors: list[str] = st.session_state.get("page15_bundle_parse_errors") or []
    pg_map: dict = st.session_state.get("page15_bundle_pg_map") or {}

    # 入力を書き換えたのに「🔍 查询」を押し直さず「⬇️ 生成 ZIP」を押すと、
    # 下の処理は session_state の items（＝前回のバッチ）から CSV を作ってしまう。
    # 画面には新しいテキストが見えているので、古い組合せの CSV が出たことに気付けない。
    # 結果とテキストが食い違っていたら結果を伏せ、再クエリを促す。
    if items and st.session_state.get("page15_bundle_src") != bundle_text:
        st.warning(t("⚠️ 输入已修改，请重新点「🔍 查询 PG 并预览」后再生成"
                     "（当前显示的是上一次查询的结果，已隐藏以免生成错误的 CSV）"))
        items, parse_errors, pg_map = [], [], {}

    if parse_errors:
        with st.expander(t(f"⚠️ 解析错误 {len(parse_errors)} 条（这些行不进 CSV）"),
                          expanded=True):
            for e in parse_errors:
                st.text(f"• {e}")

    if items:
        rows = []
        for it in items:
            pg = pg_map.get(it["jan"]) or {}
            rows.append({
                "JAN": it["jan"],
                "数量": it["qty"],
                "bundle_sku": it["bundle_sku"],
                "Internal ID": pg.get("internal_id") or "",
                "日文品名": pg.get("display_name") or "",
                "状态": "✅ 命中" if pg.get("internal_id") else "❌ PG 未命中",
            })
        df_bundle = pd.DataFrame(rows)

        n_hit = (df_bundle["Internal ID"] != "").sum()
        n_miss = (df_bundle["Internal ID"] == "").sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(t("セット品数"), len(items))
        c2.metric(t("PG 命中"), int(n_hit))
        c3.metric(t("PG 未命中"), int(n_miss), delta_color="inverse")
        c4.metric(t("子记录行数"), int(df_bundle["数量"].sum()))

        st.subheader(t("📋 セット品预览（命中行才进 CSV）"))
        st.dataframe(df_bundle, use_container_width=True, height=300)

        st.divider()
        st.subheader(t("📦 生成 NST 父/子 CSV"))

        if st.button(t("⬇️ 生成 ZIP（父表 + 子表 CSV）"), type="primary",
                     key="page15_bundle_gen"):
            valid = [r for r in rows if r["Internal ID"]]
            if not valid:
                st.error(t("❌ 没有命中行可生成（请确认 JAN 已在 nst.item_master_raw）"))
            else:
                # 父表
                parent_buf = io.StringIO()
                pw = pd.DataFrame(
                    [{"外部 ID": r["bundle_sku"], "名称": r["bundle_sku"]} for r in valid]
                )
                pw.to_csv(parent_buf, index=False, encoding=None)
                parent_csv = parent_buf.getvalue().encode("utf-8-sig")

                # 子表（每行展开 qty 次）
                child_rows = []
                for r in valid:
                    for i in range(1, int(r["数量"]) + 1):
                        child_rows.append({
                            "外部 ID": f"{r['bundle_sku']}_{i}",
                            "父记录": r["bundle_sku"],
                            "内部 ID": r["Internal ID"],
                            "UPC Code": r["JAN"],
                            "价格": "",
                        })
                child_buf = io.StringIO()
                pd.DataFrame(child_rows).to_csv(child_buf, index=False)
                child_csv = child_buf.getvalue().encode("utf-8-sig")

                date_str = datetime.now().strftime("%Y%m%d")
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(f"NST_父组合货品SKU_{date_str}.csv", parent_csv)
                    zf.writestr(f"NST_子组合货品_{date_str}.csv", child_csv)
                buf.seek(0)
                st.session_state["page15_bundle_zip"] = buf.getvalue()
                st.session_state["page15_bundle_zip_n"] = (len(valid), len(child_rows))

        zip_bytes = st.session_state.get("page15_bundle_zip")
        if zip_bytes:
            n_parent, n_child = st.session_state.get("page15_bundle_zip_n", (0, 0))
            st.download_button(
                t(f"⬇️ 下载 ZIP（父 {n_parent} 行 + 子 {n_child} 行）"),
                data=zip_bytes,
                file_name=f"セット品登録_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip",
                type="primary",
            )
            st.success(t(f"✅ 已生成 · 解压含 2 份 CSV，直传 NetSuite"))


# ───────────────────────── tab 📜 旧 HTML 版 ─────────────────────────

with tab_legacy:
    st.caption(t("旧商品登録ツール（HTML 版）· 参考用 · JD 出力は廃止（2026-10-03）"))
    st.info(t(
        "📌 旧版商品登録ツール iframe 嵌入（参考用）。JD 出力は廃止済み；"
        "新品登録（NST CSV + 斑马 API 登録）は「🆕 商品登録 (原生)」tab を使ってください。"
    ))

    html_path = Path(__file__).resolve().parent.parent / "assets" / "商品登録ツール_0418.html"
    if html_path.exists():
        components.html(html_path.read_text(encoding="utf-8"), height=1500, scrolling=True)
    else:
        st.error(t(f"❌ 找不到 HTML 文件：{html_path}"))
