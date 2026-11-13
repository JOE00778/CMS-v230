"""模块 #48 · 🆕 新品订货申请（Boss 2026-11-13「运营想要的货在 CMS 中自己提申请，采购去确认，然后做发注书，之后每周确定一次 PO 是否到货」
「主要用于新品的订货」「不需要分角色，只要能看到，采购负责人就会去确认，如果一直不到就会一直提示」）.

  ➕ 提申请   JAN・数量・商品名・备注（申请人 = 飞书登录名）。同 JAN 有未完成的申请时不能再提
  📋 待确认   谁都可以确认 / 取消（记录是谁）。确认时定 仕入先・数量（单价可到发注书再填）
  📦 发注书   待发注的申请 → NST 订货单 CSV（shared/po_csv = page10 同一逻辑・仕入先各一个外部 ID）
  🚚 到货跟踪 每次打开都按 NST 发注明细（每天同步）现算：超期置顶，直到到货或取消
设计: docs/superpowers/specs/2026-11-13-new-item-order-request-design.md · 逻辑: shared/new_item_request.py
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd
import streamlit as st

from shared import image_search, jan_web
from shared import new_item_request as NR
from shared import po_csv
from shared.auth import require_password
from shared.db import get_connection
from shared.editor_state import lang_stable_editor
from shared.i18n import lang_selector, t
from shared.nst_choices import load_suppliers
from shared.theme import inject_theme

st.set_page_config(page_title=t("新品订货申请"), page_icon="🆕", layout="wide")
require_password()
inject_theme()
lang_selector()
st.title(t("🆕 新品订货申请"))
st.caption(t("运营提申请 → 采购确认 → 生成 NST 发注书 CSV → 按 NST 发注明细（每天同步）自动跟踪到货。"
             "没有入荷预定日的 PO 按发注日 + {d} 天算期限，超期的一直置顶提示，直到到货或取消").format(d=NR.OVERDUE_DAYS))

USER = (st.session_state.get("__lark_user") or {}).get("name") or "password-login"
TODAY = date.today()


@st.cache_data(ttl=600, show_spinner=False)
def _masters() -> tuple[pd.DataFrame, list[str]]:
    with get_connection() as conn:
        return po_csv.load_item_master(conn), load_suppliers(conn)


def _load() -> list[dict]:
    with get_connection() as conn:
        NR.ensure_table(conn)
        reqs = NR.load_requests(conn)
        po = NR.load_po_lines(conn, [r["jan"] for r in reqs if r["status"] == "confirmed"])
    return NR.track(reqs, po, TODAY)


try:
    ITEMS, SUPPLIERS = _masters()
    tracked = _load()
except Exception as e:  # noqa: BLE001
    st.error(t("❌ 读取失败: {err}").format(err=f"{type(e).__name__}: {e}"))
    st.stop()
NAMES = dict(zip(ITEMS["jan"], ITEMS["display_name"])) if not ITEMS.empty else {}
# 起批量 = NST 発注ロット（Boss 2026-11-13「数量是需要根据起批量来订货」→ 整倍数）。空・0 は 1 扱いで NO_LOT に印
_LOT_RAW = dict(zip(ITEMS["jan"], ITEMS["order_lot"])) if not ITEMS.empty else {}
NO_LOT = {j for j, v in _LOT_RAW.items() if pd.isna(v) or v < 1}
LOTS = {j: 1 if j in NO_LOT else int(v) for j, v in _LOT_RAW.items()}

flash = st.session_state.pop("p48_flash", None)
if flash:
    st.success(flash)

by_state = {s: [x for x in tracked if x["state"] == s] for s in NR.STATES}
summ = NR.arrival_summary(tracked, TODAY)
c = st.columns(5)
c[0].metric(t("待确认"), len(by_state["待确认"]))
c[1].metric(t("待发注"), len(by_state["待发注"]))
c[2].metric(t("在途"), len(by_state["在途"]) + len(by_state["部分到货"]))
c[3].metric(t("🔴 超期"), len(by_state["超期"]))
c[4].metric(t("到货率（近 90 天确认）"), "-" if summ["rate"] is None else f"{summ['rate']:.0%}",
            help=t("确认 {n} 件：已到货 {a} · 在途未超期 {i} · 超期 {o} · 待发注 {u} · PO 关闭未到齐 {c}").format(
                n=summ["total"], a=summ["arrived"], i=summ["in_transit"], o=summ["overdue"], u=summ["unordered"],
                c=summ["closed_short"]))
if by_state["超期"]:
    st.error(t("🔴 超期未到货 {n} 件（最长超 {d} 天）→ 看「🚚 到货跟踪」").format(
        n=len(by_state["超期"]), d=max(x["overdue_days"] for x in by_state["超期"])))


def _name(x: dict) -> str:
    return NAMES.get(x["jan"]) or x.get("item_name") or ""


def _when(v) -> str:
    return v.strftime("%Y-%m-%d") if v else ""


def _lot_label(jan: str) -> str:
    if jan not in LOTS:
        return t("未知（NST 未建档）")
    return t("未设（按 1）") if jan in NO_LOT else str(LOTS[jan])


def _lot_errors(recs: list[dict], qty_col: str) -> list[str]:
    """NST 登録済みで数量が起批量の整倍数でない行 →「JAN（起批量 24・建议 48）」。"""
    out = []
    for r in recs:
        q = pd.to_numeric(r[qty_col], errors="coerce")
        if r["JAN"] in LOTS and pd.notna(q):
            ok, sug = NR.lot_check(int(q), LOTS[r["JAN"]])
            if not ok:
                out.append(t("{j}（起批量 {lot}・建议 {s}）").format(j=r["JAN"], lot=LOTS[r["JAN"]], s=sug))
    return out


def _img_one(jan: str) -> str:
    try:
        return image_search.search_jan(jan)
    except Exception:  # noqa: BLE001 — 画像が取れなくても申請は止めない
        return ""


def _images(jans: list[str]) -> dict[str, str]:
    """JAN → 画像 URL（PG キャッシュ → 楽天 → kakaku・shared/image_search）。セッション内で覚える（取れなかった品も再検索しない）。"""
    memo = st.session_state.setdefault("p48_img", {})
    need = [j for j in dict.fromkeys(jans) if j and j not in memo]
    if need:
        with st.spinner(t("读取商品图片中（{n} 件）…").format(n=len(need))):
            with ThreadPoolExecutor(4) as ex:
                memo.update(zip(need, ex.map(_img_one, need)))
    return {j: memo.get(j) or None for j in jans}


def _done(msg: str) -> None:
    st.session_state["p48_flash"] = msg
    st.rerun()


tab_new, tab_conf, tab_po, tab_track = st.tabs([
    t("➕ 提申请"), t("📋 待确认（{n}）").format(n=len(by_state["待确认"])),
    t("📦 发注书（{n}）").format(n=len(by_state["待发注"])),
    t("🚚 到货跟踪（{n}）").format(n=sum(len(by_state[s]) for s in ("超期", "部分到货", "在途", "待发注")))])

# ── ➕ 提申请（Boss 2026-11-13「先提 JAN，点确认，然后自动检索出单价，商品名和图，然后再根据每个产品填写数量」）
def _lookup(jans: list[str]) -> list[dict]:
    """JAN → 画像・商品名・単価（最終仕入原価 > 原価）・起批量。NST 未登録は 楽天 の商品名（単価なし）。"""
    rec = ITEMS.set_index("jan").to_dict("index") if not ITEMS.empty else {}
    unreg = [j for j in jans if j not in rec]
    web = {}
    if unreg:
        with st.spinner(t("在网上检索 NST 未建档商品的名称（{n} 件）…").format(n=len(unreg))):
            with ThreadPoolExecutor(4) as ex:
                web = dict(zip(unreg, ex.map(_web_name, unreg)))
    imgs = _images(jans)
    out = []
    for j in jans:
        price, src = NR.unit_price_of(rec.get(j) or {})
        out.append({"jan": j, "img": imgs.get(j), "name": NAMES.get(j) or web.get(j, ""), "price": price,
                    "src": src or ("" if j in rec else "NST 未建档"), "lot": _lot_label(j)})
    return out


def _web_name(jan: str) -> str:
    try:
        return jan_web.rakuten_meta(jan).name or ""
    except Exception:  # noqa: BLE001 — 名前が取れなくても申請は止めない（手で入れる）
        return ""


with tab_new:
    gen = st.session_state.get("p48_new_gen", 0)
    st.caption(t("① 粘贴 JAN（一行一个，可多个）→「🔍 确认」→ ② 自动带出图片・商品名・单价・起批量，逐行填数量 → 提交。申请人: {u}")
               .format(u=USER))
    raw = st.text_area(t("JAN（一行一个）"), key=f"p48_jans_{gen}", height=120)
    if st.button(t("🔍 确认"), key="p48_lookup_btn", disabled=not raw.strip()):
        jans, bad = [], []
        for ln in raw.splitlines():
            if ln.strip():
                j = NR.normalize_jan(ln)
                if j:      # 式文で書くと Streamlit の magic が None を画面に出す（2026-11-13 Boss 指摘）
                    jans.append(j)
                else:
                    bad.append(ln.strip())
        jans = list(dict.fromkeys(jans))
        conflicts = NR.open_jan_conflicts(jans, tracked)
        for x in bad:
            st.error("❌ " + t("JAN「{j}」不是 8〜14 位数字").format(j=x))
        for j, rid in conflicts.items():
            st.warning(t("JAN {j} 已有未完成的申请 #{id}（到货或取消后才能再提）").format(j=j, id=rid))
        st.session_state["p48_lookup"] = _lookup([j for j in jans if j not in conflicts])
        st.session_state["p48_lookup_gen"] = st.session_state.get("p48_lookup_gen", 0) + 1
    found = st.session_state.get("p48_lookup") or []
    if found:
        st.markdown(t("**② 核对图片和商品，填数量**（数量要是起批量的整倍数；没填数量的行不提交）"))
        df = pd.DataFrame([{"图片": x["img"], "JAN": x["jan"], "商品名": x["name"], "单价": x["price"],
                            "单价来源": t(x["src"]) if x["src"] else "", "起批量": x["lot"], "数量": None, "备注": ""} for x in found])
        ed = lang_stable_editor(df, key=f"p48_fill_{st.session_state.get('p48_lookup_gen', 0)}",
                                reset=tuple(df["JAN"]), hide_index=True, num_rows="fixed", use_container_width=True,
                                row_height=72, disabled=["图片", "JAN", "单价来源", "起批量"],
                                column_config={"图片": st.column_config.ImageColumn(t("图片"), width="small"),
                                               "商品名": st.column_config.TextColumn(t("商品名")),
                                               "单价": st.column_config.NumberColumn(t("单价"), min_value=0),
                                               "单价来源": st.column_config.Column(t("单价来源")),
                                               "起批量": st.column_config.Column(t("起批量")),
                                               "数量": st.column_config.NumberColumn(t("数量"), min_value=1, step=1),
                                               "备注": st.column_config.TextColumn(t("备注"))})
        recs = [r for r in ed.to_dict("records") if pd.notna(pd.to_numeric(r["数量"], errors="coerce"))]
        for e in _lot_errors(recs, "数量"):
            st.error("❌ " + t("数量不是起批量的整倍数: {item}").format(item=e))
        st.caption(t("要提交 {n} 件（共 {m} 件，没填数量的 {k} 件不提交）").format(n=len(recs), m=len(ed), k=len(ed) - len(recs)))
        if st.button(t("✅ 图片和数量确认无误，提交申请"), type="primary", key="p48_submit", disabled=not recs):
            rows, errs = NR.parse_new_rows(recs, tracked, lots=LOTS)
            if errs:
                for e in errs:
                    st.error("❌ " + e)
                st.warning(t("有错误，全部没有提交"))
            else:
                price = {str(r["JAN"]): r["单价"] for r in recs}
                for r in rows:
                    r["item_name"] = r["item_name"] or NAMES.get(r["jan"], "")
                    v = pd.to_numeric(price.get(r["jan"]), errors="coerce")
                    r["unit_price"] = None if pd.isna(v) else float(v)
                with get_connection() as conn:
                    ids = NR.insert_requests(conn, rows, USER)
                unreg = [r["jan"] for r in rows if r["jan"] not in NAMES]
                st.session_state["p48_new_gen"] = gen + 1      # JAN 欄を空に
                st.session_state.pop("p48_lookup", None)
                _done(t("✅ 已提交 {n} 件（#{ids}）").format(n=len(ids), ids=", #".join(map(str, ids)))
                      + (" · " + t("NST 未建档 {n} 件：{jans}（发注前要先在 page15 商品登录建档）").format(
                          n=len(unreg), jans="・".join(unreg)) if unreg else ""))

# ── 📋 待确认
with tab_conf:
    xs = by_state["待确认"]
    if not xs:
        st.success(t("没有待确认的申请"))
    else:
        st.caption(t("勾选后「✅ 确认」或「🗑️ 取消」。确认要填仕入先和数量，单价可以到发注书再填。操作人: {u}").format(u=USER))
        imgs = _images([x["jan"] for x in xs])
        df = pd.DataFrame([{"选择": False, "图片": imgs[x["jan"]], "#": x["id"], "JAN": x["jan"], "商品名": _name(x),
                            "NST": "✅" if x["jan"] in NAMES else "❌", "起批量": _lot_label(x["jan"]),
                            "申请数": x["qty"], "备注": x.get("note") or "",
                            "申请人": x.get("requested_by") or "", "申请日": _when(x["requested_at"]),
                            "仕入先": x.get("supplier") or None, "单价": x.get("unit_price"), "确认数量": x["qty"]}
                           for x in xs])
        ed = lang_stable_editor(df, key="p48_conf_editor", reset=tuple(df["#"]), hide_index=True, num_rows="fixed",
                                use_container_width=True, row_height=72,
                                disabled=["图片", "#", "JAN", "商品名", "NST", "起批量", "申请数", "备注", "申请人", "申请日"],
                                column_config={**{k: st.column_config.Column(t(k)) for k in
                                                  ("选择", "商品名", "起批量", "申请数", "备注", "申请人", "申请日")},
                                               "图片": st.column_config.ImageColumn(t("图片"), width="small"),
                                               "NST": st.column_config.Column(t("NST 建档")),
                                               "仕入先": st.column_config.SelectboxColumn(t("仕入先"), options=SUPPLIERS),
                                               "单价": st.column_config.NumberColumn(t("单价"), min_value=0),
                                               "确认数量": st.column_config.NumberColumn(t("确认数量"), min_value=1,
                                                                                     step=1)})
        sel = [r for r in ed.to_dict("records") if r["选择"]]
        b1, b2 = st.columns(2)
        if b1.button(t("✅ 确认（{n} 件）").format(n=len(sel)), type="primary", disabled=not sel, key="p48_confirm"):
            bad = [str(r["JAN"]) for r in sel if not r["仕入先"] or pd.isna(pd.to_numeric(r["确认数量"], errors="coerce"))]
            lot_bad = _lot_errors(sel, "确认数量")
            if bad:
                st.error(t("❌ 这些没填仕入先或数量: {jans}").format(jans="・".join(bad)))
            elif lot_bad:
                st.error(t("❌ 这些数量不是起批量的整倍数，全部没有确认: {items}").format(items=" / ".join(lot_bad)))
            else:
                with get_connection() as conn:
                    n = NR.confirm(conn, [{"id": r["#"], "supplier": r["仕入先"],
                                           "unit_price": None if pd.isna(r["单价"]) else float(r["单价"]),
                                           "confirmed_qty": int(r["确认数量"])} for r in sel], USER)
                _done(t("✅ 已确认 {n} 件").format(n=n))
        if b2.button(t("🗑️ 取消（{n} 件）").format(n=len(sel)), disabled=not sel, key="p48_cancel_req"):
            with get_connection() as conn:
                n = NR.cancel(conn, [r["#"] for r in sel], USER)
            _done(t("🗑️ 已取消 {n} 件").format(n=n))

# ── 📦 发注书
with tab_po:
    xs = by_state["待发注"]
    if not xs:
        st.success(t("没有待发注的申请"))
    else:
        st.caption(t("勾选要下单的 → 生成 NST 订货单 CSV（与 page10 发注書作成同一格式），在 NST 导入。"
                     "导入后 NST 发注明细同步时会自动变为「在途」"))
        df = pd.DataFrame([{"选择": x["jan"] in NAMES, "#": x["id"], "JAN": x["jan"], "商品名": _name(x),
                            "NST": "✅" if x["jan"] in NAMES else "❌", "起批量": _lot_label(x["jan"]),
                            "仕入先": x.get("supplier") or None,
                            "单价": x.get("unit_price"), "数量": x.get("confirmed_qty") or x["qty"],
                            "确认人": x.get("confirmed_by") or "", "确认日": _when(x.get("confirmed_at"))} for x in xs])
        ed = lang_stable_editor(df, key="p48_po_editor", reset=tuple(df["#"]), hide_index=True, num_rows="fixed",
                                use_container_width=True,
                                disabled=["#", "JAN", "商品名", "NST", "起批量", "确认人", "确认日"],
                                column_config={**{k: st.column_config.Column(t(k)) for k in
                                                  ("选择", "商品名", "起批量", "确认人", "确认日")},
                                               "NST": st.column_config.Column(t("NST 建档")),
                                               "仕入先": st.column_config.SelectboxColumn(t("仕入先"), options=SUPPLIERS),
                                               "单价": st.column_config.NumberColumn(t("单价"), min_value=0),
                                               "数量": st.column_config.NumberColumn(t("数量"), min_value=1, step=1)})
        c1, c2, c3 = st.columns(3)
        order_date = c1.date_input(t("日付"), value=TODAY, key="p48_date")
        employee = c1.selectbox(t("従業員"), po_csv.EMPLOYEES, key="p48_emp")
        department = c2.selectbox(t("部門"), po_csv.DEPARTMENTS, key="p48_dept")
        location = c2.selectbox(t("場所"), po_csv.LOCATIONS, key="p48_loc")
        memo = c3.text_input(t("备注"), t("新品订货申请"), key="p48_memo")
        tax_pct = c3.selectbox(t("税率 fallback（DB 缺失时使用）"), [10, 8, 0], format_func=lambda x: f"{x}%",
                               key="p48_tax")
        sel = [r for r in ed.to_dict("records") if r["选择"]]
        if st.button(t("📄 生成发注书 CSV（{n} 件）").format(n=len(sel)), type="primary", disabled=not sel,
                     key="p48_make_po"):
            unreg = [str(r["JAN"]) for r in sel if r["JAN"] not in NAMES]
            bad = [str(r["JAN"]) for r in sel if r["JAN"] in NAMES and (
                not r["仕入先"] or pd.isna(pd.to_numeric(r["单价"], errors="coerce")) or float(r["单价"]) <= 0)]
            if unreg:
                st.warning(t("NST 未建档，没放进 CSV（先到 page15 商品登录建档）: {jans}").format(jans="・".join(unreg)))
            if bad:
                st.error(t("❌ 这些没填仕入先或单价，全部没有生成: {jans}").format(jans="・".join(bad)))
            lot_bad = _lot_errors([r for r in sel if r["JAN"] in NAMES], "数量")
            if lot_bad:
                st.error(t("❌ 这些数量不是起批量的整倍数，全部没有生成: {items}").format(items=" / ".join(lot_bad)))
            ok = [r for r in sel if r["JAN"] in NAMES]
            if ok and not bad and not lot_bad:
                ext = datetime.now().strftime("%Y%m%d%H%M%S")
                outs = []
                for i, (sup, g) in enumerate(pd.DataFrame(ok).groupby("仕入先", sort=True)):
                    out, _ = po_csv.build_output(g.rename(columns={"JAN": "jan", "单价": "単価"})[["jan", "数量", "単価"]],
                                                 ITEMS, ext_id=f"{ext}{i + 1}", sup=sup, order_date=order_date,
                                                 employee=employee, department=department, memo=memo,
                                                 location=location, tax_pct=tax_pct)
                    outs.append(out)
                st.session_state["p48_po_csv"] = (pd.concat(outs, ignore_index=True), ext)
        got = st.session_state.get("p48_po_csv")
        if got:
            out, ext = got
            st.dataframe(out.rename(columns=t), use_container_width=True, hide_index=True)
            st.download_button(t("📥 订货单 CSV 下载（{n} 行 · ¥{a:,}）").format(n=len(out), a=int(out["総額"].sum())),
                               data=out.to_csv(index=False, encoding="utf-8-sig"), file_name=f"発注書_新品_{ext}.csv",
                               mime="text/csv", key="p48_po_dl")

# ── 🚚 到货跟踪
with tab_track:
    order = {"超期": 0, "部分到货": 1, "在途": 2, "待发注": 3}
    xs = sorted((x for x in tracked if x["state"] in order),
                key=lambda x: (order[x["state"]], -x["overdue_days"], x["id"]))
    icon = {"超期": "🔴", "部分到货": "🟡", "在途": "🚚", "待发注": "⏳", "已到货": "✅", "PO关闭未到齐": "⚠️", "已取消": "🗑️"}

    def _rows(items):
        return pd.DataFrame([{t("状态"): f"{icon[x['state']]} {t(x['state'])}", "#": x["id"], "JAN": x["jan"],
                              t("商品名"): _name(x), t("确认数量"): x.get("confirmed_qty") or x["qty"],
                              t("发注数"): int(x["ordered"]), t("入荷数"): int(x["received"]),
                              t("PO 号"): "・".join(x["po_numbers"]), t("期限"): _when(x["deadline"]),
                              t("超期天数"): x["overdue_days"] or None, t("仕入先"): x.get("supplier") or "",
                              t("申请人"): x.get("requested_by") or "", t("确认人"): x.get("confirmed_by") or "",
                              t("确认日"): _when(x.get("confirmed_at"))} for x in items])

    if not xs:
        st.success(t("没有未到货的申请"))
    else:
        st.dataframe(_rows(xs), use_container_width=True, hide_index=True)
        with st.expander(t("🗑️ 取消申请（供货商断货等，不再跟踪）")):
            ids = st.multiselect(t("申请 #"), [x["id"] for x in xs],
                                 format_func=lambda i: next(f"#{x['id']} {x['jan']} {_name(x)}" for x in xs if x["id"] == i),
                                 key="p48_cancel_ids")
            if st.button(t("🗑️ 取消（{n} 件）").format(n=len(ids)), disabled=not ids, key="p48_cancel_track"):
                with get_connection() as conn:
                    n = NR.cancel(conn, ids, USER)
                _done(t("🗑️ 已取消 {n} 件").format(n=n))
    done = [x for x in tracked if x["state"] in ("已到货", "PO关闭未到齐", "已取消")]
    if done:
        with st.expander(t("已完成 {n} 件（已到货 / PO 关闭未到齐 / 已取消）").format(n=len(done))):
            st.dataframe(_rows(sorted(done, key=lambda x: -x["id"])), use_container_width=True, hide_index=True)
