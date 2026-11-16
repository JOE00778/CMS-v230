"""page48「📝 订货申请（运营）」と page49「🧾 采购处理（采购）」の共通部品（Boss 2026-11-16「运营提申请，采购看待确认和发注书，
共同看到货追踪…让两个团队更加清楚的分清楚」→ A 案 +「加一个日常补货申请…按照新品订和日常补这个 tab 来区分」）.

  render_submit(ctx, kind)    運営: JAN を貼る / Excel 一括 → ② 画像・商品名・単価・起批量を見て数量 → 申請（kind = new / restock）
  render_confirm(ctx, kind)   採購: 待确认（既定の仕入先・起批量・金額）→ 確認 / 取消
  render_po(ctx, kind)        採購: 待发注 → NST 発注書 CSV（shared/po_csv）・出した時点で記録
  render_tracking(ctx, ops=)  共通: 到货跟踪（下一步谁做・種類 / 自分の申請で絞る）。運営は自分の待确认を撤回、採購は退回 / 取消
ロジックは shared/new_item_request（表 sourcing.new_item_request・PO は NST 同期から現算）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd
import streamlit as st

from shared import image_search, jan_web
from shared import new_item_request as NR
from shared import po_csv
from shared.db import get_connection
from shared.editor_state import lang_stable_editor, set_column
from shared.i18n import t
from shared.nst_choices import load_choices, load_suppliers

KIND_LABEL = {"new": "🆕 新品", "restock": "🔁 日常补货"}
KIND_MEMO = {"new": "新品订货申请", "restock": "日常补货申请"}
ICON = {"发注书已出无PO": "⚠️", "超期": "🔴", "部分到货": "🟡", "在途": "🚚", "已发注待同步": "📨", "待发注": "⏳",
        "待确认": "📋", "已到货": "✅", "PO关闭未到齐": "⚠️", "已取消": "🗑️"}
TRACK_ORDER = {"发注书已出无PO": 0, "超期": 1, "部分到货": 2, "在途": 3, "已发注待同步": 4, "待发注": 5, "待确认": 6}
DONE_STATES = ("已到货", "PO关闭未到齐", "已取消")
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@st.cache_data(ttl=600, show_spinner=False)
def _masters() -> tuple[pd.DataFrame, list[str]]:
    with get_connection() as conn:
        return po_csv.load_item_master(conn), load_suppliers(conn)


class Ctx:
    """1 回の表示で使う材料（商品マスタ・仕入先・申請と到货の現算・ログイン名）。"""

    def __init__(self) -> None:
        self.today = date.today()
        self.user = (st.session_state.get("__lark_user") or {}).get("name") or "password-login"
        self.items, self.suppliers = _masters()
        with get_connection() as conn:
            NR.ensure_table(conn)
            reqs = NR.load_requests(conn)
            po = NR.load_po_lines(conn, [r["jan"] for r in reqs if r["status"] == "confirmed"])
        self.tracked = NR.track(reqs, po, self.today)
        it = self.items
        self.names = dict(zip(it["jan"], it["display_name"])) if not it.empty else {}
        # 起批量 = NST 発注ロット（Boss 2026-11-13「数量是需要根据起批量来订货」→ 整倍数）。空・0 は 1 扱いで no_lot に印
        raw = dict(zip(it["jan"], it["order_lot"])) if not it.empty else {}
        self.no_lot = {j for j, v in raw.items() if pd.isna(v) or v < 1}
        self.lots = {j: 1 if j in self.no_lot else int(v) for j, v in raw.items()}

    def state(self, s: str, kind: str | None = None) -> list[dict]:
        xs = self.tracked if kind is None else NR.of_kind(self.tracked, kind)
        return [x for x in xs if x["state"] == s]

    def name(self, x: dict) -> str:
        return self.names.get(x["jan"]) or x.get("item_name") or ""

    def lot_label(self, jan: str) -> str:
        if jan not in self.lots:
            return t("未知（NST 未建档）")
        return t("未设（按 1）") if jan in self.no_lot else str(self.lots[jan])

    def lot_errors(self, recs: list[dict], qty_col: str) -> list[str]:
        """NST 登録済みで数量が起批量の整倍数でない行 →「JAN（起批量 24・建议 48）」。"""
        out = []
        for r in recs:
            q = pd.to_numeric(r[qty_col], errors="coerce")
            if r["JAN"] in self.lots and pd.notna(q):
                ok, sug = NR.lot_check(int(q), self.lots[r["JAN"]])
                if not ok:
                    out.append(t("{j}（起批量 {lot}・建议 {s}）").format(j=r["JAN"], lot=self.lots[r["JAN"]], s=sug))
        return out


def load_ctx() -> Ctx:
    try:
        return Ctx()
    except Exception as e:  # noqa: BLE001
        st.error(t("❌ 读取失败: {err}").format(err=f"{type(e).__name__}: {e}"))
        st.stop()


def flash() -> None:
    msg = st.session_state.pop("p48_flash", None)
    if msg:
        st.success(msg)


def _done(msg: str) -> None:
    st.session_state["p48_flash"] = msg
    st.rerun()


def _when(v) -> str:
    return v.strftime("%Y-%m-%d") if v else ""


def alerts(ctx: Ctx) -> None:
    """置顶の警告（全種類）: 発注書を出したのに NST に PO が無い / 期限超過。"""
    if ctx.state("发注书已出无PO"):
        st.error(t("⚠️ 已出发注书超过 {d} 天，NST 里还没有 PO 的 {n} 件 → 看「🚚 到货跟踪」（没导入 NST 的请「↩️ 退回待发注」）").format(
            d=NR.ORDER_SYNC_DAYS, n=len(ctx.state("发注书已出无PO"))))
    over = ctx.state("超期")
    if over:
        st.error(t("🔴 超期未到货 {n} 件（最长超 {d} 天）→ 看「🚚 到货跟踪」").format(
            n=len(over), d=max(x["overdue_days"] for x in over)))


def metrics(ctx: Ctx, kind: str) -> None:
    xs = NR.of_kind(ctx.tracked, kind)
    summ = NR.arrival_summary(xs, ctx.today)
    c = st.columns(5)
    c[0].metric(t("待确认"), len(ctx.state("待确认", kind)))
    c[1].metric(t("待发注"), len(ctx.state("待发注", kind)))
    c[2].metric(t("在途"), sum(len(ctx.state(s, kind)) for s in ("在途", "部分到货", "已发注待同步")))
    c[3].metric(t("🔴 超期"), len(ctx.state("超期", kind)))
    c[4].metric(t("到货率（近 90 天确认）"), "-" if summ["rate"] is None else f"{summ['rate']:.0%}",
                help=t("确认 {n} 件：已到货 {a} · 在途未超期 {i} · 超期 {o} · 待发注 {u} · PO 关闭未到齐 {c}").format(
                    n=summ["total"], a=summ["arrived"], i=summ["in_transit"], o=summ["overdue"], u=summ["unordered"],
                    c=summ["closed_short"]))


def amount_block(recs: list[dict], qty_col: str, title: str) -> float:
    """SKU ごとの金額（数量 × 単価）表と合計（Boss 2026-11-13「要体现本次订货SKU总金额和全部都总金额」）。→ 合計。"""
    rows, total, no_price = [], 0.0, 0
    for r in recs:
        q, pr = (pd.to_numeric(r.get(k), errors="coerce") for k in (qty_col, "单价"))
        a = None if pd.isna(q) or pd.isna(pr) else float(q) * float(pr)
        no_price += a is None
        total += a or 0.0
        rows.append({"JAN": r["JAN"], "name": r.get("商品名") or "", "qty": None if pd.isna(q) else int(q),
                     "price": None if pd.isna(pr) else float(pr), "amount": a})
    if rows:
        st.markdown(title.format(n=len(rows), a=total)
                    + (t("（单价未填 {k} 个 SKU 未计入）").format(k=no_price) if no_price else ""))
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={"name": st.column_config.Column(t("商品名")),
                                    "qty": st.column_config.NumberColumn(t("数量")),
                                    "price": st.column_config.NumberColumn(t("单价"), format="yen"),
                                    "amount": st.column_config.NumberColumn(t("金额"), format="yen")})
    return total


def _img_one(jan: str) -> str:
    try:
        return image_search.search_jan(jan)
    except Exception:  # noqa: BLE001 — 画像が取れなくても申請は止めない
        return ""


def images(jans: list[str]) -> dict[str, str]:
    """JAN → 画像 URL（PG キャッシュ → 楽天 → kakaku・shared/image_search）。セッション内で覚える（取れなかった品も再検索しない）。"""
    memo = st.session_state.setdefault("p48_img", {})
    need = [j for j in dict.fromkeys(jans) if j and j not in memo]
    if need:
        with st.spinner(t("读取商品图片中（{n} 件）…").format(n=len(need))):
            with ThreadPoolExecutor(4) as ex:
                memo.update(zip(need, ex.map(_img_one, need)))
    return {j: memo.get(j) or None for j in jans}


def _web_name(jan: str) -> str:
    try:
        return jan_web.rakuten_meta(jan).name or ""
    except Exception:  # noqa: BLE001 — 名前が取れなくても申請は止めない（手で入れる）
        return ""


def _lookup(ctx: Ctx, jans: list[str]) -> list[dict]:
    """JAN → 画像・商品名・単価（最終仕入原価 > 原価）・起批量。NST 未登録は 楽天 の商品名（単価なし）。"""
    rec = ctx.items.set_index("jan").to_dict("index") if not ctx.items.empty else {}
    unreg = [j for j in jans if j not in rec]
    web = {}
    if unreg:
        with st.spinner(t("在网上检索 NST 未建档商品的名称（{n} 件）…").format(n=len(unreg))):
            with ThreadPoolExecutor(4) as ex:
                web = dict(zip(unreg, ex.map(_web_name, unreg)))
    imgs = images(jans)
    out = []
    for j in jans:
        price, src = NR.unit_price_of(rec.get(j) or {})
        out.append({"jan": j, "img": imgs.get(j), "name": ctx.names.get(j) or web.get(j, ""), "price": price,
                    "src": src or ("" if j in rec else "NST 未建档"), "lot": ctx.lot_label(j)})
    return out


def _start_fill(ctx: Ctx, kind: str, items: list[dict]) -> None:
    """① の結果（JAN ごとの Excel 値つき）→ 画像・商品名・単価・起批量を引いて ② の表へ。
    未完成の申請がある JAN と、日常补货で NST 未登録の JAN は外す。"""
    k = f"p48{kind}"
    if kind == "restock":
        _, unreg = NR.split_restock_jans([x["jan"] for x in items], ctx.names)
        for j in unreg:
            st.error("❌ " + t("JAN {j} 在 NST 没有登录，日常补货只能申请已登录的商品（新品请到「🆕 新品订货」）").format(j=j))
        items = [x for x in items if x["jan"] not in unreg]
    conflicts = NR.open_jan_conflicts([x["jan"] for x in items], ctx.tracked)
    for j, rid in conflicts.items():
        st.warning(t("JAN {j} 已有未完成的申请 #{id}（到货或取消后才能再提）").format(j=j, id=rid))
    items = [x for x in items if x["jan"] not in conflicts]
    found = []
    for x, y in zip(items, _lookup(ctx, [x["jan"] for x in items])):
        if x.get("price") is not None:          # Excel の単価 > NST（最終仕入原価 > 原価）
            y["price"], y["src"] = x["price"], "Excel"
        found.append({**y, "ref": x.get("ref", ""), "qty": x.get("qty"), "note": x.get("note", "")})
    st.session_state[f"{k}_lookup"] = found
    st.session_state[f"{k}_lookup_gen"] = st.session_state.get(f"{k}_lookup_gen", 0) + 1


def _read_upload(up):
    try:
        if up.name.lower().endswith(".csv"):
            try:
                return pd.read_csv(up, dtype=str, encoding="utf-8-sig")
            except UnicodeDecodeError:
                up.seek(0)
                return pd.read_csv(up, dtype=str, encoding="cp932")
        return pd.read_excel(up, dtype=str)
    except Exception as e:  # noqa: BLE001
        st.error(t("❌ 读取失败: {err}").format(err=f"{type(e).__name__}: {e}"))
        return None


# ───────────── 運営: 申請 ─────────────

def month_window(ctx: Ctx, kind: str) -> None:
    """📦 本月订货（Boss 2026-11-16「哪些已经订了，当月已订货的金额」→「放在最下面，已经订货和已申请的金额分开来计算，
    入库的是已经进库存的，这部分也要计算」「还有 SKU 数」）。口径は NR.month_summary / applied_in_month / ordered_in_month。"""
    s = NR.month_summary(ctx.tracked, kind, ctx.today)
    with st.container(border=True):
        st.markdown(t("**📦 本月（{m}）{kind}订货**").format(m=f"{ctx.today:%Y-%m}", kind=t(KIND_LABEL[kind])))
        c = st.columns(3)
        for col, key, label in ((c[0], "applied", "已申请（还没下单）"), (c[1], "ordered", "已订货"), (c[2], "received", "其中已入库")):
            col.metric(t(label), f"¥{s[key]['amount']:,.0f}", t("{n} 个 SKU").format(n=s[key]["sku"]), delta_color="off")
        rows = ([{**x, "_cls": "已申请", "_date": _when(x["requested_at"])} for x in NR.applied_in_month(ctx.tracked, kind, ctx.today)]
                + [{**x, "_cls": "已订货", "_date": _when(x["order_date"])} for x in NR.ordered_in_month(ctx.tracked, kind, ctx.today)])
        if not rows:
            st.caption(t("本月还没有{kind}申请").format(kind=t(KIND_LABEL[kind])))
            return
        with st.expander(t("明细（{n} 件）").format(n=len(rows))):
            st.dataframe(pd.DataFrame([{
                "cls": t(x["_cls"]), "date": x["_date"], "JAN": x["jan"], "name": ctx.name(x),
                "qty": int(x["ordered"]) or x.get("confirmed_qty") or x["qty"], "amount": x["amount"],
                "recv": int(x["received"]), "recv_amt": x.get("received_amount") or None,
                "sup": "・".join(x["po_vendors"]) or x.get("supplier") or "", "po": "・".join(x["po_numbers"]),
                "state": f"{ICON[x['state']]} {t(x['state'])}", "by": x.get("requested_by") or "", "ref": x.get("ref_no") or ""}
                for x in rows]), hide_index=True, use_container_width=True,
                column_config={"cls": st.column_config.Column(t("类别")), "date": st.column_config.Column(t("日期")),
                               "name": st.column_config.Column(t("商品名")), "qty": st.column_config.NumberColumn(t("数量")),
                               "amount": st.column_config.NumberColumn(t("金额"), format="yen"),
                               "recv": st.column_config.NumberColumn(t("入荷数")),
                               "recv_amt": st.column_config.NumberColumn(t("入库金额"), format="yen"),
                               "sup": st.column_config.Column(t("仕入先")), "po": st.column_config.Column(t("PO 号")),
                               "state": st.column_config.Column(t("状态")), "by": st.column_config.Column(t("申请人")),
                               "ref": st.column_config.Column(t("定位号"))})
        st.caption(t("已申请 = 本月提交、还在待确认 / 待发注的（数量 × 单价）；已订货 = 本月生成发注书或 NST 有 PO 的（NST 同步后按 PO 实际金额）；"
                     "其中已入库 = NST 入荷数 × PO 单价（每天 03:03 同步）。日期：已申请 = 申请日，已订货 = 发注日"))


def render_submit(ctx: Ctx, kind: str) -> None:
    _submit_flow(ctx, kind)
    month_window(ctx, kind)    # 最下部（Boss 2026-11-16「本月订货的内容放在最下面」）


def _submit_flow(ctx: Ctx, kind: str) -> None:
    k = f"p48{kind}"
    gen = st.session_state.get(f"{k}_gen", 0)
    mine = [x for x in NR.of_kind(ctx.tracked, kind) if x.get("requested_by") == ctx.user]
    st.caption(t("我的{kind}申请：待确认 {a} · 待发注 {b} · 在途 {c} · 超期 {d}（详细看「🚚 到货跟踪」）").format(
        kind=t(KIND_LABEL[kind]), a=sum(x["state"] == "待确认" for x in mine),
        b=sum(x["state"] == "待发注" for x in mine),
        c=sum(x["state"] in ("在途", "部分到货", "已发注待同步") for x in mine), d=sum(x["state"] == "超期" for x in mine)))
    if kind == "restock":
        st.info(t("🔁 日常补货：只能申请 NST 已登录的商品。新品请到「🆕 新品订货」"))
    mode = st.radio(t("提交方式"), ["粘贴 JAN", "📤 Excel 批量上传"], horizontal=True, format_func=t, key=f"{k}_mode")
    if mode == "粘贴 JAN":
        st.caption(t("① 粘贴 JAN（一行一个，可多个）→「🔍 确认」→ ② 自动带出图片・商品名・单价・起批量，逐行填数量 → 提交。申请人: {u}")
                   .format(u=ctx.user))
        raw = st.text_area(t("JAN（一行一个）"), key=f"{k}_jans_{gen}", height=120)
        ref = st.text_input(t("定位号（在报价表里的位置，这次的 JAN 共用，可空）"), key=f"{k}_ref_{gen}")
        if st.button(t("🔍 确认"), key=f"{k}_lookup_btn", disabled=not raw.strip()):
            jans, bad = [], []
            for ln in raw.splitlines():
                if ln.strip():
                    j = NR.normalize_jan(ln)
                    if j:      # 式文で書くと Streamlit の magic が None を画面に出す（2026-11-13 Boss 指摘）
                        jans.append(j)
                    else:
                        bad.append(ln.strip())
            for x in bad:
                st.error("❌ " + t("JAN「{j}」不是 8〜14 位数字").format(j=x))
            _start_fill(ctx, kind, [{"jan": j, "ref": ref.strip()} for j in dict.fromkeys(jans)])
    else:   # Boss 2026-11-16「搞一个 excel 批量上传模式」「做一个模板，供下载用」
        st.caption(t("① 下载模板 → 填 定位号・JAN・数量・单价・备注（JAN 以外可空）→ 上传 →「📥 读取 Excel」→ ② 核对图片和数量 → 提交。"
                     "定位号空着就沿用上一行；一格多个 JAN 会拆成多行。申请人: {u}").format(u=ctx.user))
        st.download_button(t("📄 下载 Excel 模板"), data=NR.template_xlsx(), file_name="订货申请_模板.xlsx", mime=_XLSX,
                           key=f"{k}_tpl")
        up = st.file_uploader(t("上传填好的 Excel（.xlsx / .csv）"), type=["xlsx", "csv"], key=f"{k}_up_{gen}")
        if st.button(t("📥 读取 Excel"), key=f"{k}_read_xlsx", disabled=up is None):
            xdf = _read_upload(up)
            if xdf is not None:
                items, msgs = NR.parse_upload(xdf)
                for m_ in msgs:
                    st.warning(m_)
                if items:
                    st.success(t("读到 {n} 个 JAN").format(n=len(items)))
                    _start_fill(ctx, kind, items)
    found = st.session_state.get(f"{k}_lookup") or []
    if not found:
        return
    st.markdown(t("**② 核对图片和商品，填数量**（数量要是起批量的整倍数；没填数量的行不提交）"))
    st.caption(t("💡 单价・数量可以从 Excel 复制一整列，点表格里第一个格子粘贴，一次填满"))
    df = pd.DataFrame([{"图片": x["img"], "JAN": x["jan"], "定位号": x.get("ref", ""), "商品名": x["name"],
                        "单价": x["price"], "单价来源": t(x["src"]) if x["src"] else "", "起批量": x["lot"],
                        "数量": x.get("qty"), "备注": x.get("note") or ""} for x in found])
    ed = lang_stable_editor(df, key=f"{k}_fill_{st.session_state.get(f'{k}_lookup_gen', 0)}",
                            reset=tuple(df["JAN"]), hide_index=True, num_rows="fixed", use_container_width=True,
                            row_height=72, disabled=["图片", "JAN", "单价来源", "起批量"],
                            column_config={"图片": st.column_config.ImageColumn(t("图片"), width="small"),
                                           "定位号": st.column_config.TextColumn(t("定位号")),
                                           "商品名": st.column_config.TextColumn(t("商品名")),
                                           "单价": st.column_config.NumberColumn(t("单价"), min_value=0),
                                           "单价来源": st.column_config.Column(t("单价来源")),
                                           "起批量": st.column_config.Column(t("起批量")),
                                           "数量": st.column_config.NumberColumn(t("数量"), min_value=1, step=1),
                                           "备注": st.column_config.TextColumn(t("备注"))})
    recs = [r for r in ed.to_dict("records") if pd.notna(pd.to_numeric(r["数量"], errors="coerce"))]
    for e in ctx.lot_errors(recs, "数量"):
        st.error("❌ " + t("数量不是起批量的整倍数: {item}").format(item=e))
    st.caption(t("要提交 {n} 件（共 {m} 件，没填数量的 {k} 件不提交）").format(n=len(recs), m=len(ed), k=len(ed) - len(recs)))
    amount_block(recs, "数量", t("**💴 本次订货：{n} 个 SKU，合计 ¥{a:,.0f}**"))
    if st.button(t("✅ 图片和数量确认无误，提交申请"), type="primary", key=f"{k}_submit", disabled=not recs):
        rows, errs = NR.parse_new_rows(recs, ctx.tracked, lots=ctx.lots)
        if kind == "restock":
            errs += [t("JAN {j} 在 NST 没有登录，日常补货只能申请已登录的商品（新品请到「🆕 新品订货」）").format(j=j)
                     for j in NR.split_restock_jans([r["jan"] for r in rows], ctx.names)[1]]
        if errs:
            for e in errs:
                st.error("❌ " + e)
            st.warning(t("有错误，全部没有提交"))
            return
        price = {str(r["JAN"]): r["单价"] for r in recs}
        for r in rows:
            r["item_name"] = r["item_name"] or ctx.names.get(r["jan"], "")
            v = pd.to_numeric(price.get(r["jan"]), errors="coerce")
            r["unit_price"] = None if pd.isna(v) else float(v)
            r["kind"] = kind
        with get_connection() as conn:
            ids = NR.insert_requests(conn, rows, ctx.user)
        unreg = [r["jan"] for r in rows if r["jan"] not in ctx.names]
        st.session_state[f"{k}_gen"] = gen + 1      # JAN 欄を空に
        st.session_state.pop(f"{k}_lookup", None)
        _done(t("✅ 已提交 {n} 件（#{ids}）").format(n=len(ids), ids=", #".join(map(str, ids)))
              + (" · " + t("NST 未建档 {n} 件：{jans}（发注前要先在 page15 商品登录建档）").format(
                  n=len(unreg), jans="・".join(unreg)) if unreg else ""))


# ───────────── 採購: 待确认・発注書 ─────────────

def render_confirm(ctx: Ctx, kind: str) -> None:
    k = f"p49{kind}"
    xs = ctx.state("待确认", kind)
    if not xs:
        st.success(t("没有待确认的申请"))
        return
    if kind == "new":   # Boss 2026-11-16「待确认里加一个NST登录的筛选项，让我知道哪些是没有登录的」
        n = {"all": len(xs), "yes": sum(x["jan"] in ctx.names for x in xs)}
        n["no"] = n["all"] - n["yes"]
        nst = st.radio(t("NST登录"), ["all", "yes", "no"], horizontal=True, key=f"{k}_nst",
                       format_func=lambda v: t({"all": "全部（{n}）", "yes": "✅ 已登录（{n}）",
                                                "no": "❌ 未登录（{n}）"}[v]).format(n=n[v]))
        if nst != "all":
            xs = [x for x in xs if (x["jan"] in ctx.names) == (nst == "yes")]
        if not xs:
            st.info(t("没有符合条件的申请"))
            return
    else:
        nst = "all"
    st.caption(t("勾选后「✅ 确认」或「🗑️ 取消」。确认要填仕入先和数量，单价可以到发注书再填。操作人: {u}").format(u=ctx.user))
    imgs = images([x["jan"] for x in xs])
    try:   # 既定の仕入先 = 供货商管理の主供指定 > 最近 PO（Boss 2026-11-13）
        with get_connection() as conn:
            dflt = NR.pick_default_supplier(*NR.load_supplier_sources(conn, [x["jan"] for x in xs]), ctx.suppliers)
    except Exception as e:  # noqa: BLE001 — 取れなくても確認は手で選べる
        st.warning(t("默认供货商读取失败（请手选）: {err}").format(err=f"{type(e).__name__}: {e}"))
        dflt = {}
    df = pd.DataFrame([{"选择": False, "图片": imgs[x["jan"]], "#": x["id"], "定位号": x.get("ref_no") or "",
                        "JAN": x["jan"], "商品名": ctx.name(x),
                        "NST": "✅" if x["jan"] in ctx.names else "❌", "起批量": ctx.lot_label(x["jan"]),
                        "申请数": x["qty"], "备注": x.get("note") or "",
                        "申请人": x.get("requested_by") or "", "申请日": _when(x["requested_at"]),
                        "仕入先": x.get("supplier") or dflt.get(x["jan"], (None, ""))[0],
                        "供货商来源": "" if x.get("supplier") else t(dflt.get(x["jan"], (None, ""))[1] or "无"),
                        "单价": x.get("unit_price"), "确认数量": x["qty"]} for x in xs])
    a1, a2, _ = st.columns([1, 1, 4])   # Boss 2026-11-16「加一个可以批量选择的选项」（今の絞り込みの行だけ・ほかの列の入力は保つ）
    if a1.button(t("☑️ 全选（{n} 件）").format(n=len(xs)), key=f"{k}_sel_all"):
        set_column(f"{k}_conf_editor", "选择", True)
    if a2.button(t("⬜ 全不选"), key=f"{k}_sel_none"):
        set_column(f"{k}_conf_editor", "选择", False)
    ed = lang_stable_editor(df, key=f"{k}_conf_editor", reset=tuple(df["#"]), hide_index=True, num_rows="fixed",
                            use_container_width=True, row_height=72,
                            disabled=["图片", "#", "定位号", "JAN", "商品名", "NST", "起批量", "申请数", "备注", "申请人",
                                      "申请日", "供货商来源"],
                            column_config={**{c: st.column_config.Column(t(c)) for c in
                                              ("选择", "定位号", "商品名", "起批量", "申请数", "备注", "申请人", "申请日",
                                               "供货商来源")},
                                           "图片": st.column_config.ImageColumn(t("图片"), width="small"),
                                           "NST": st.column_config.Column(t("NST登录")),
                                           "仕入先": st.column_config.SelectboxColumn(t("仕入先"), options=ctx.suppliers),
                                           "单价": st.column_config.NumberColumn(t("单价"), min_value=0),
                                           "确认数量": st.column_config.NumberColumn(t("确认数量"), min_value=1, step=1)})
    all_recs = ed.to_dict("records")
    amount_block(all_recs, "确认数量", t("**💴 全部待确认：{n} 个 SKU，合计 ¥{a:,.0f}**") if nst == "all"
                 else t("**💴 筛选后的待确认：{n} 个 SKU，合计 ¥{a:,.0f}**"))
    sel = [r for r in all_recs if r["选择"]]
    sel_total = sum(float(q) * float(pr) for q, pr in ((pd.to_numeric(r["确认数量"], errors="coerce"),
                                                       pd.to_numeric(r["单价"], errors="coerce")) for r in sel)
                    if pd.notna(q) and pd.notna(pr))
    b1, b2, b3 = st.columns(3)
    if kind == "new":
        _register_xlsx(ctx, k, sel, b3)
    if b1.button(t("✅ 确认（{n} 件 · ¥{a:,.0f}）").format(n=len(sel), a=sel_total), type="primary", disabled=not sel,
                 key=f"{k}_confirm"):
        bad = [str(r["JAN"]) for r in sel if not r["仕入先"] or pd.isna(pd.to_numeric(r["确认数量"], errors="coerce"))]
        lot_bad = ctx.lot_errors(sel, "确认数量")
        if bad:
            st.error(t("❌ 这些没填仕入先或数量: {jans}").format(jans="・".join(bad)))
        elif lot_bad:
            st.error(t("❌ 这些数量不是起批量的整倍数，全部没有确认: {items}").format(items=" / ".join(lot_bad)))
        else:
            with get_connection() as conn:
                n = NR.confirm(conn, [{"id": r["#"], "supplier": r["仕入先"],
                                       "unit_price": None if pd.isna(r["单价"]) else float(r["单价"]),
                                       "confirmed_qty": int(r["确认数量"])} for r in sel], ctx.user)
            _done(t("✅ 已确认 {n} 件").format(n=n))
    if b2.button(t("🗑️ 取消（{n} 件）").format(n=len(sel)), disabled=not sel, key=f"{k}_cancel_req"):
        with get_connection() as conn:
            n = NR.cancel(conn, [r["#"] for r in sel], ctx.user)
        _done(t("🗑️ 已取消 {n} 件").format(n=n))


def _register_xlsx(ctx: Ctx, k: str, sel: list[dict], col) -> None:
    """勾选した NST 未登録 → page15「📝 商品登录」のテンプレ（JAN・商品名・原価・仕入先・担当者を預め埋める・Boss 2026-11-16）。"""
    unreg = [r for r in sel if r["JAN"] not in ctx.names]
    if col.button(t("📝 生成商品登录 Excel（{n} 件）").format(n=len(unreg)), disabled=not unreg, key=f"{k}_reg_make"):
        try:
            with get_connection() as conn:
                choices = load_choices(conn)
        except Exception as e:  # noqa: BLE001
            st.error(t("❌ 读取失败: {err}").format(err=f"{type(e).__name__}: {e}"))
            return
        owner = NR.owner_of(ctx.user, choices.owners)
        data = NR.register_template([{"jan": r["JAN"], "name": r["商品名"], "cost": r["单价"], "supplier": r["仕入先"]}
                                     for r in unreg], choices, owner)
        st.session_state[f"{k}_reg_xlsx"] = (data, len(unreg), len(sel) - len(unreg), owner)
    got = st.session_state.get(f"{k}_reg_xlsx")
    if not got:
        return
    data, n, skipped, owner = got
    if skipped:
        st.info(t("勾选里已登录 NST 的 {n} 件跳过了（不用登录）").format(n=skipped))
    st.download_button(t("📥 下载商品登录 Excel（{n} 件）").format(n=n), data=data, mime=_XLSX, key=f"{k}_reg_dl",
                       file_name=f"商品登録_入力テンプレ_{ctx.today:%Y%m%d}.xlsx")
    st.caption(t("已填好 JAN・商品名・商品原价・供应商{owner}。还要填 大分类・中分类・外箱入数（空着按 1）・订货批量{need}"
                 " → 到「📝 商品登录」上传处理 → 生成 NST CSV 导入 NST（同时登录斑马）→ 第二天 03:02 同步后这里显示 ✅")
               .format(owner=t("・商品负责人（{o}）").format(o=owner) if owner else "",
                       need="" if owner else t("・商品负责人")))


def render_po(ctx: Ctx, kind: str) -> None:
    k = f"p49{kind}"
    xs = ctx.state("待发注", kind)
    got = st.session_state.get(f"{k}_po_csv")
    if got:   # 生成直後は申請が「已发注待同步」に移って表が空になっても、ダウンロードは残す
        out, ext = got
        st.dataframe(out.rename(columns=t), use_container_width=True, hide_index=True)
        st.download_button(t("📥 订货单 CSV 下载（{n} 行 · ¥{a:,}）").format(n=len(out), a=int(out["総額"].sum())),
                           data=out.to_csv(index=False, encoding="utf-8-sig"),
                           file_name=f"発注書_{'新品' if kind == 'new' else '補充'}_{ext}.csv", mime="text/csv",
                           key=f"{k}_po_dl")
    if not xs:
        st.success(t("没有待发注的申请"))
        return
    st.caption(t("勾选要下单的 → 生成 NST 订货单 CSV（与 page10 发注書作成同一格式），在 NST 导入。"
                 "导入后 NST 发注明细同步时会自动变为「在途」"))
    df = pd.DataFrame([{"选择": x["jan"] in ctx.names, "#": x["id"], "JAN": x["jan"], "商品名": ctx.name(x),
                        "NST": "✅" if x["jan"] in ctx.names else "❌", "起批量": ctx.lot_label(x["jan"]),
                        "仕入先": x.get("supplier") or None, "单价": x.get("unit_price"),
                        "数量": x.get("confirmed_qty") or x["qty"], "确认人": x.get("confirmed_by") or "",
                        "确认日": _when(x.get("confirmed_at"))} for x in xs])
    ed = lang_stable_editor(df, key=f"{k}_po_editor", reset=tuple(df["#"]), hide_index=True, num_rows="fixed",
                            use_container_width=True, disabled=["#", "JAN", "商品名", "NST", "起批量", "确认人", "确认日"],
                            column_config={**{c: st.column_config.Column(t(c)) for c in
                                              ("选择", "商品名", "起批量", "确认人", "确认日")},
                                           "NST": st.column_config.Column(t("NST登录")),
                                           "仕入先": st.column_config.SelectboxColumn(t("仕入先"), options=ctx.suppliers),
                                           "单价": st.column_config.NumberColumn(t("单价"), min_value=0),
                                           "数量": st.column_config.NumberColumn(t("数量"), min_value=1, step=1)})
    c1, c2, c3 = st.columns(3)
    order_date = c1.date_input(t("日付"), value=ctx.today, key=f"{k}_date")
    employee = c1.selectbox(t("従業員"), po_csv.EMPLOYEES, key=f"{k}_emp")
    department = c2.selectbox(t("部門"), po_csv.DEPARTMENTS, key=f"{k}_dept")
    location = c2.selectbox(t("場所"), po_csv.LOCATIONS, key=f"{k}_loc")
    memo = c3.text_input(t("备注"), t(KIND_MEMO[kind]), key=f"{k}_memo")
    tax_pct = c3.selectbox(t("税率 fallback（DB 缺失时使用）"), [10, 8, 0], format_func=lambda x: f"{x}%", key=f"{k}_tax")
    sel = [r for r in ed.to_dict("records") if r["选择"]]
    if not st.button(t("📄 生成发注书 CSV（{n} 件）").format(n=len(sel)), type="primary", disabled=not sel,
                     key=f"{k}_make_po"):
        return
    unreg = [str(r["JAN"]) for r in sel if r["JAN"] not in ctx.names]
    bad = [str(r["JAN"]) for r in sel if r["JAN"] in ctx.names and (
        not r["仕入先"] or pd.isna(pd.to_numeric(r["单价"], errors="coerce")) or float(r["单价"]) <= 0)]
    if unreg:
        st.warning(t("NST 未建档，没放进 CSV（先到 page15 商品登录建档）: {jans}").format(jans="・".join(unreg)))
    if bad:
        st.error(t("❌ 这些没填仕入先或单价，全部没有生成: {jans}").format(jans="・".join(bad)))
    lot_bad = ctx.lot_errors([r for r in sel if r["JAN"] in ctx.names], "数量")
    if lot_bad:
        st.error(t("❌ 这些数量不是起批量的整倍数，全部没有生成: {items}").format(items=" / ".join(lot_bad)))
    ok = [r for r in sel if r["JAN"] in ctx.names]
    if not ok or bad or lot_bad:
        return
    ext = datetime.now().strftime("%Y%m%d%H%M%S")
    outs, marks = [], []
    for i, (sup, g) in enumerate(pd.DataFrame(ok).groupby("仕入先", sort=True)):
        out, _ = po_csv.build_output(g.rename(columns={"JAN": "jan", "单价": "単価"})[["jan", "数量", "単価"]],
                                     ctx.items, ext_id=f"{ext}{i + 1}", sup=sup, order_date=order_date,
                                     employee=employee, department=department, memo=memo, location=location,
                                     tax_pct=tax_pct)
        outs.append(out)
        marks += [{**r, "_ext": f"{ext}{i + 1}"} for r in g.to_dict("records")]
    st.session_state[f"{k}_po_csv"] = (pd.concat(outs, ignore_index=True), ext)
    # 発注書を出した記録（その時の仕入先・単価・数量）→ 発注書タブから消え「已发注待同步」へ（Boss 2026-11-13）
    with get_connection() as conn:
        n = NR.mark_ordered(conn, [{"id": r["#"], "supplier": r["仕入先"], "unit_price": float(r["单价"]),
                                    "confirmed_qty": int(r["数量"]), "po_ext_id": r["_ext"]} for r in marks], ctx.user)
    _done(t("📄 已生成发注书 CSV（{n} 件）→ 请到「📦 发注书」下载并导入 NST。这些申请已移到「🚚 到货跟踪」，NST 同步出 PO 后显示 PO 号")
          .format(n=n))


# ───────────── 共通: 到货跟踪 ─────────────

def _diff_text(x: dict) -> str:
    return " · ".join(t("{k}: {a} → {b}").format(k=t(k), a=f"{a:g}" if isinstance(a, float) else a,
                                                  b=f"{b:g}" if isinstance(b, float) else b) for k, a, b in x["diffs"])


def _track_rows(ctx: Ctx, items: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{t("状态"): f"{ICON[x['state']]} {t(x['state'])}",
                          t("下一步谁做"): t(NR.next_actor(x["state"])) if NR.next_actor(x["state"]) else "",
                          t("类型"): t(KIND_LABEL[x.get("kind") or "new"]), "#": x["id"], "JAN": x["jan"],
                          t("商品名"): ctx.name(x), t("仕入先"): "・".join(x["po_vendors"]) or x.get("supplier") or "",
                          t("确认数量"): x.get("confirmed_qty") or x["qty"], t("发注数"): int(x["ordered"]),
                          t("入荷数"): int(x["received"]), t("金额"): NR.amount_of(x),
                          t("入库金额"): x.get("received_amount") or None, t("PO 号"): "・".join(x["po_numbers"]),
                          t("与发注书的差异"): ("⚠️ " + _diff_text(x)) if x["diffs"] else "",
                          t("期限"): _when(x["deadline"]), t("超期天数"): x["overdue_days"] or None,
                          t("发注书日"): _when(x.get("ordered_at")), t("定位号"): x.get("ref_no") or "",
                          t("申请人"): x.get("requested_by") or "", t("确认人"): x.get("confirmed_by") or ""}
                         for x in items])


def render_tracking(ctx: Ctx, *, ops: bool) -> None:
    """両ページ同じ表示（Boss 2026-11-16「到货追踪，两个模块的形式不一样，做到统一」）: 筛选・红色提醒・合计・表・已完成。
    違うのは最下部「🛠 操作」だけ — ops=True（運営）: 自分の待确认を撤回。ops=False（採購）: 退回待发注・取消。"""
    k = "p48trk" if ops else "p49trk"
    c1, c2, c3 = st.columns([2, 3, 2])
    kind = c1.radio(t("类型"), ["all", "new", "restock"], horizontal=True, key=f"{k}_kind",
                    format_func=lambda x: t("全部") if x == "all" else t(KIND_LABEL[x]))
    q = c2.text_input(t("🔍 JAN 搜索（可以只输一部分，多个 JAN 用空格隔开）"), key=f"{k}_jan_q")   # Boss 2026-11-13
    mine = c3.toggle(t("只看我的申请"), value=False, key=f"{k}_mine")

    def keep(x: dict) -> bool:
        return ((kind == "all" or (x.get("kind") or "new") == kind) and NR.match_jan(x["jan"], q)
                and (not mine or x.get("requested_by") == ctx.user))

    xs = sorted((x for x in ctx.tracked if x["state"] in TRACK_ORDER and keep(x)),
                key=lambda x: (TRACK_ORDER[x["state"]], -x["overdue_days"], x["id"]))
    if not xs:
        if q.strip() or mine or kind != "all":
            st.info(t("没有符合条件的申请"))
        else:
            st.success(t("没有未到货的申请"))
    else:
        nopo, over = ([x for x in xs if x["state"] == s] for s in ("发注书已出无PO", "超期"))
        if nopo:
            st.error(t("⚠️ 已出发注书超过 {d} 天，NST 里还没有 PO：{n} 件").format(d=NR.ORDER_SYNC_DAYS, n=len(nopo)))
        if over:
            st.error(t("🔴 超期未到货 {n} 件（最长超 {d} 天）").format(n=len(over), d=max(x["overdue_days"] for x in over)))
        sp = NR.receipt_split(xs)   # Boss 2026-11-16「入库的是已经进库存的，这部分也要计算」「还有 SKU 数」
        st.markdown(t("**💴 已入库：{rs} 个 SKU · ¥{ra:,.0f}　｜　未入库：{os} 个 SKU · ¥{oa:,.0f}**（共 {n} 件）").format(
            rs=sp["received"]["sku"], ra=sp["received"]["amount"], os=sp["open"]["sku"], oa=sp["open"]["amount"], n=len(xs)))
        st.caption(t("已入库 = NST 入荷数 × PO 单价；未入库 = 金额 − 已入库（含还没下单的申请，按数量 × 单价）"))
        st.dataframe(_track_rows(ctx, xs), use_container_width=True, hide_index=True,
                     column_config={t(c): st.column_config.NumberColumn(t(c), format="yen") for c in ("金额", "入库金额")})

        def pick(label: str, cands: list[dict], key: str) -> list:
            return st.multiselect(label, [x["id"] for x in cands], key=key,
                                  format_func=lambda i: next(f"#{x['id']} {x['jan']} {ctx.name(x)}" for x in cands
                                                             if x["id"] == i))

        st.markdown(t("**🛠 操作**"))
        if ops:
            mine_pending = [x for x in xs if x["state"] == "待确认" and x.get("requested_by") == ctx.user]
            if mine_pending:
                with st.expander(t("↩️ 撤回我的申请（只能撤回还没确认的）")):
                    ids = pick(t("申请 #"), mine_pending, f"{k}_withdraw_ids")
                    if st.button(t("↩️ 撤回（{n} 件）").format(n=len(ids)), disabled=not ids, key=f"{k}_withdraw"):
                        with get_connection() as conn:
                            n = NR.withdraw(conn, ids, ctx.user)
                        _done(t("↩️ 已撤回 {n} 件").format(n=n))
            else:
                st.caption(t("只能撤回自己提交、还没确认的申请（现在没有）"))
        else:
            back = [x for x in xs if x["state"] in ("已发注待同步", "发注书已出无PO")]
            if back:
                with st.expander(t("↩️ 退回待发注（生成了发注书但没导入 NST 的）")):
                    ids = pick(t("申请 #"), back, f"{k}_unmark_ids")
                    if st.button(t("↩️ 退回（{n} 件）").format(n=len(ids)), disabled=not ids, key=f"{k}_unmark"):
                        with get_connection() as conn:
                            n = NR.unmark_ordered(conn, ids)
                        _done(t("↩️ 已退回待发注 {n} 件").format(n=n))
            with st.expander(t("🗑️ 取消申请（供货商断货等，不再跟踪）")):
                ids = pick(t("申请 #"), xs, f"{k}_cancel_ids")
                if st.button(t("🗑️ 取消（{n} 件）").format(n=len(ids)), disabled=not ids, key=f"{k}_cancel_track"):
                    with get_connection() as conn:
                        n = NR.cancel(conn, ids, ctx.user)
                    _done(t("🗑️ 已取消 {n} 件").format(n=n))
    done = [x for x in ctx.tracked if x["state"] in DONE_STATES and keep(x)]
    if done:
        rd = NR.receipt_split(done)["received"]
        with st.expander(t("已完成 {n} 件（已到货 / PO 关闭未到齐 / 已取消）").format(n=len(done))
                         + t(" · 已入库 {s} 个 SKU · ¥{a:,.0f}").format(s=rd["sku"], a=rd["amount"])):
            st.dataframe(_track_rows(ctx, sorted(done, key=lambda x: -x["id"])), use_container_width=True,
                         hide_index=True, column_config={t(c): st.column_config.NumberColumn(t(c), format="yen")
                                                          for c in ("金额", "入库金额")})
