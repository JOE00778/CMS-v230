"""模块 #15 商品登录 · 新品首次登録（NST 取込 CSV + 斑马導入 xlsx）

定位：商品首次登录（NST/BM 还都没有这个 SKU），不是已登录商品的修改。
修改场景请走 page 03（定義原価編集）/ page 07（商品等级判定）等。

三个 tab：
  🆕 商品登録 v2（Boss 2026-10-03 拍板 · JD 出力廃止）
  📦 セット品登録：JAN_数量 → NST 父/子组合货品 CSV
  📜 旧 HTML 版：商品登録ツール iframe

v2 流程（オーケストレーションは shared/item_register.py）：
  ① テンプレ DL（仕入先=nst.vendor_master 完全名称 / 大分類・中分類=NST 実在組合せ / 担当者 4 人）
  ② 記入済み xlsx を UL → ③「重量・寸法をネットで調べる」（既定 OFF）→ ④「▶ 処理する」
     read_upload → NST 既登録 JAN を error → enrich（jancode 商品名・メーカー・固定値・HS・網調べ）
  ⑤ ok/ng/warn 件数・issues 表・data_editor で修正
  ⑥「📦 生成」NST CSV（有データ列のみ・原本の登録手順どおり）+ 斑马 xlsx → ZIP、同時に台帳 nst.item_register_log
"""
from __future__ import annotations

import hashlib
import io
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
from shared import hs_classify, jan_web
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

tab_item, tab_bundle, tab_legacy = st.tabs([
    t("🆕 商品登録 (原生)"),
    t("📦 セット品登録 (原生)"),
    t("📜 旧 HTML 版"),
])

JAN_COL_NAME = "JANコード"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _augment_image_url(conn, df: pd.DataFrame) -> pd.DataFrame:
    """从 nst.item_image_cache 按 JAN 取已有 image_url 作辅助参考列（不进 CSV）。"""
    if df.empty or JAN_COL_NAME not in df.columns:
        df["_画像URL（参考·来自 image cache）"] = ""
        return df
    jans = [str(j).strip() for j in df[JAN_COL_NAME] if pd.notna(j) and str(j).strip()]
    if not jans:
        df["_画像URL（参考·来自 image cache）"] = ""
        return df
    placeholder = ",".join(["%s"] * len(jans))
    rows = conn.execute(
        f"SELECT jan_cd, image_url FROM nst.item_image_cache "
        f"WHERE jan_cd IN ({placeholder}) AND status='ok'",
        jans,
    ).fetchall()
    url_map = {r["jan_cd"]: r["image_url"] for r in rows}
    df["_画像URL（参考·来自 image cache）"] = df[JAN_COL_NAME].astype(str).str.strip().map(url_map).fillna("")
    return df


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


_ITEM_KEYS = ["page15_result", "page15_zip_bytes", "page15_zip_n", "page15_log_msg",
              "page15_gen_issues", "page15_editor", "page15_zip_src"]


def _content_hash(records) -> str:
    """表・行の内容ハッシュ（ZIP が今の表から作られたか / 台帳に同じ内容を記録済みか の判定）。"""
    import json
    return hashlib.sha256(json.dumps(records, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


def _issues_df(issues) -> pd.DataFrame:
    return pd.DataFrame([{t("行"): i.row or "", "JAN": i.jan,
                          t("区分"): "❌ error" if i.level == "error" else "⚠️ warn",
                          t("内容"): i.message} for i in issues])


def _process(data: bytes, choices: NC.Choices, want_weight: bool) -> dict:
    """read_upload → NST 既登録チェック → enrich。結果は dict で session_state へ。"""
    rows, issues = FORM.read_upload(data, choices)
    notes: list[str] = []
    ng = len({i.row for i in issues if i.level == "error" and i.row})
    if any(i.level == "error" and not i.row for i in issues):
        notes.append(t("ファイル全体のエラーがあります（下の表を確認）"))
    nst_rows, extra, stats = [], {}, {}
    if rows:
        with get_connection() as conn:
            exist, warns = NC.existing_jans([r[JAN_COL_NAME] for r in rows], conn=conn)
        notes += warns
        for r in rows:
            if r[JAN_COL_NAME] in exist:
                issues.append(FORM.Issue(0, r[JAN_COL_NAME], "error",
                                         "NST に登録済みの JAN です（新規登録の対象外）"))
        ng += len(exist)
        rows = [r for r in rows if r[JAN_COL_NAME] not in exist]
    if rows:
        try:
            prefix_makers = _cached_prefix_makers()
        except Exception as e:  # noqa: BLE001 — メーカーは jancode 会社名で続行
            prefix_makers = {}
            notes.append(f"NST メーカー（JAN プレフィックス）の読込に失敗・jancode 会社名だけで判定: {e}")
        bar = st.progress(0.0, text=t("処理中…"))
        nst_rows, extra, more, stats = REG.enrich(
            rows, choices=choices, prefix_makers=prefix_makers,
            fetch_meta=jan_web.fetch_meta, fetch_weight=jan_web.fetch_weight,
            classify_customs=hs_classify.classify_customs, want_weight=want_weight,
            on_progress=lambda d, n, msg: bar.progress(min(d / n, 1.0) if n else 1.0,
                                                       text=f"{d}/{n} {msg}"))
        bar.empty()
        issues += more
        ng += stats["ng"]
    ok_jans = {r[JAN_COL_NAME] for r in nst_rows}
    warn = len({i.jan for i in issues if i.level == "warn"} & ok_jans)
    return {"nst": nst_rows, "extra": extra, "issues": issues, "stats": stats,
            "notes": notes, "ok": len(nst_rows), "ng": ng, "warn": warn}


def _generate(edited: pd.DataFrame, res: dict) -> None:
    """data_editor の内容 → NST CSV + 斑马 xlsx → ZIP。同時に台帳へ記録（失敗しても ZIP は出す）。"""
    out_nst, out_bm, sources, gen_issues = REG.from_frame(
        edited.to_dict("records"), res["nst"], res["extra"])
    st.session_state["page15_gen_issues"] = gen_issues
    if not out_nst:
        st.error(t("❌ 生成できる行がありません"))
        return
    nst_csv = TPL.build_nst_master_csv(out_nst, REG.csv_field_columns(out_nst),
                                       id_label=TPL.COL_ITEM_CODE)
    image_url_map: dict[str, str] = {}
    try:
        with get_connection() as conn:
            _df = _augment_image_url(conn, pd.DataFrame(out_nst))
        image_url_map = {j: u for j, u in zip(_df[JAN_COL_NAME],
                                              _df["_画像URL（参考·来自 image cache）"]) if u}
    except Exception:  # noqa: BLE001 — 画像は参考。取れなくても生成は続ける
        pass
    bm_xlsx = JBM.build_bm_xlsx(out_bm, image_url_map=image_url_map)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(TPL.dated_filename(), nst_csv)
        zf.writestr(JBM.dated_filename_bm(), bm_xlsx)
    st.session_state["page15_zip_bytes"] = buf.getvalue()
    st.session_state["page15_zip_n"] = len(out_nst)
    st.session_state["page15_zip_src"] = _content_hash(edited.to_dict("records"))

    # 台帳は append-only。同じ内容の押し直しで同じ行を重ねない（このセッション内）
    log_key = _content_hash(out_nst)
    if st.session_state.get("page15_logged") == log_key:
        st.session_state["page15_log_msg"] = ("ok", t("台帳は記録済み（同じ内容のため再記録しません）"))
        return
    bm_lists = [JBM.nst_to_bm_row(r, image_url=image_url_map.get(r[JAN_COL_NAME], ""))
                for r in out_bm]
    try:
        with get_connection() as conn:
            n_log, err = REG.write_register_log(conn, out_nst, bm_lists, sources)
    except Exception as e:  # noqa: BLE001 — 接続自体の失敗
        n_log, err = 0, f"{type(e).__name__}: {e}"
    st.session_state["page15_log_msg"] = (
        ("error", t(f"台帳への記録に失敗: {err}（ZIP は生成済み）")) if err
        else ("ok", t(f"台帳 nst.item_register_log に {n_log} 件記録")))
    if not err:
        st.session_state["page15_logged"] = log_key


def _render_result(res: dict) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("OK", res["ok"])
    c2.metric("NG", res["ng"], delta_color="inverse")
    c3.metric(t("警告あり"), res["warn"])
    s = res["stats"]
    if s:
        st.caption(
            f"jancode ok={s['jancode_ok']} not_found={s['jancode_not_found']} "
            f"error={s['jancode_error']} · メーカー NST={s['maker_nst']} "
            f"jancode={s['maker_jancode']} 空={s['maker_none']} · アイテム名切詰め={s['name_cut']} 手入力={s['name_manual']} · "
            f"HS ok={s['hs_ok']} 判定不可={s['hs_ng']} · 重量 取得={s['weight_hit']} "
            f"なし={s['weight_miss']} 失敗={s['weight_error']} 調べず={s['weight_skipped']}")
    for n in res["notes"]:
        st.warning(n)
    if res["issues"]:
        n_err = sum(i.level == "error" for i in res["issues"])
        with st.expander(t(f"⚠️ 問題 {len(res['issues'])} 件（error {n_err} 件の行は出力しません）"),
                         expanded=True):
            st.dataframe(_issues_df(res["issues"]), use_container_width=True, hide_index=True)
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
    edited = st.data_editor(
        df, use_container_width=True, hide_index=True, num_rows="fixed",
        disabled=[c for c in df.columns if c not in REG.EDITABLE],
        column_config=col_config, key="page15_editor")

    st.divider()
    if st.button(t("📦 生成（NST.csv + 斑马.xlsx）"), type="primary", key="page15_gen"):
        _generate(edited, res)
    gen_issues = st.session_state.get("page15_gen_issues") or []
    if gen_issues:
        st.dataframe(_issues_df(gen_issues), use_container_width=True, hide_index=True)
    zip_bytes = st.session_state.get("page15_zip_bytes")
    if zip_bytes and st.session_state.get("page15_zip_src") != _content_hash(edited.to_dict("records")):
        # 画面は編集後の値なのに ZIP は編集前 → 気づけないので伏せる（セット品タブと同じ対策）
        st.warning(t("⚠️ 表を編集しました。もう一度「📦 生成」を押してください（前回の ZIP は隠しています）"))
        zip_bytes = None
    if zip_bytes:
        n = st.session_state.get("page15_zip_n", 0)
        st.download_button(
            t(f"⬇️ ZIP をダウンロード（{n} 件 · NST.csv + 斑马.xlsx）"), data=zip_bytes,
            file_name=f"商品登録_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip", type="primary", key="page15_zip_dl")
        level, msg = st.session_state.get("page15_log_msg") or ("ok", "")
        (st.error if level == "error" else st.success)(msg)


def _render_item_tab() -> None:
    """⚠️ st.stop() 禁止（スクリプト全体が止まり他タブが消える）。抜けるときは return。"""
    st.caption(t("新品登録 · テンプレ DL → 記入 → UL → NST 取込 CSV + 斑马導入 xlsx（どちらも手でアップロード）"))
    with st.expander(t("📌 使い方"), expanded=False):
        st.markdown(t(
            "- **人が書くのは 12 列だけ**：JAN / 商品原価 / 仕入先（完全名称）/ 商品担当者 / 大分類・中分類 / "
            "カートン入数 / 発注ロット / パッケージ 3 辺・重量（jancode に商品名が無い JAN だけ「アイテム名（任意）」も記入）\n"
            "- **自動で埋める**：アイテム名（jancode 商品名・60 字）/ メーカー名（NST 同プレフィックス → jancode）/ "
            "型番=JAN / 取扱中・NEW・輸出事業・輸出専用・日本円 / 税率（食品=★）/ 斑马の HS・通関英文名（≤76 字）\n"
            "- **網調べ**は空欄だけ埋める（手入力は上書きしない）。出典 URL を表で確認\n"
            "- **NST に登録済みの JAN** はエラーで外す\n"
            "- **出力**：NST CSV（有データ列のみ・先頭=型番）+ 斑马 xlsx（中文名称 = アイテム名）"
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
    want_weight = st.checkbox(t("☑ 重量・寸法をネットで調べる（スーパーデリバリー）"),
                              value=False, key="page15_want_weight")
    if want_weight:
        st.caption(t("目安：約 1.5〜2 秒/件（パッケージ欄に空欄がある行だけ調べます）"))
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
        "新品登録（NST CSV + 斑马 xlsx）は「🆕 商品登録 (原生)」tab を使ってください。"
    ))

    html_path = Path(__file__).resolve().parent.parent / "assets" / "商品登録ツール_0418.html"
    if html_path.exists():
        components.html(html_path.read_text(encoding="utf-8"), height=1500, scrolling=True)
    else:
        st.error(t(f"❌ 找不到 HTML 文件：{html_path}"))
