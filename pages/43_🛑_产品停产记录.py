"""模块 · 产品停产记录（Boss 2026-10-03）。

数据源 = 元川 stock_monitor（廃番監視・每周日 02:00）写入的 PG：
  · public.discontinue_alerts         改廃 1 件 = 1 行（販売終了=停产・即将停产 / 消失 / 初回確認；另有旧 page13 手动标记 boss_flagged*）
  · public.discontinue_scan_runs      1 次扫描 = 1 行（改廃 0 件的周也有，用来确认监控在跑）
  · public.discontinue_watch_status   监控对象 1 JAN = 1 行的最新状态（「监控范围」用；in_scope_run = 最近一次全量）
查询对象 = ハリマ共和物産（NETde卸 运营方）在库系统，按 JAN 返回「廃番/廃番予定あり」或「廃番予定なし」。
扫描范围 = NST 輸出事業 · 取扱中 · 等级 A/B/C/NEW（元川 export_stock_monitor.sql）。
本页只读。NST 等级/取扱区分/在库按当前值实时关联。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("产品停产记录"), page_icon="🛑", layout="wide")
from shared.auth import require_password  # noqa: E402

require_password()
from shared.theme import inject_theme  # noqa: E402

inject_theme()
lang_selector()

st.title(t("🛑 产品停产记录"))
st.caption(t("每周日 02:00 用ハリマ共和物産（NETde卸）的在库系统检查监控范围内的商品 · 查到停产・即将停产或从数据源消失就记在这里"))

RANKS = ("Aランク", "Bランク", "Cランク", "NEW")
SIGNAL_LABEL = {
    "販売終了": "新规停产・即将停产",
    "消失": "从数据源消失",
    "初回確認": "首次确认时已停产・即将停产",
}
STATUS_LABEL = {
    "discontinued": "停产・即将停产",
    "active": "在售（无停产预定）",
    "notfound": "数据源查不到",
    "error": "查询失败",
}

_ALERT_SQL = """
SELECT a.detected_at, a.signal_type, a.source, a.jan,
       COALESCE(a.name, m.display_name) AS name,
       COALESCE(a.maker, m.maker)       AS maker,
       m.item_rank, m.handling_cd, m.internal_id,
       a.matched_name, a.product_url
FROM public.discontinue_alerts a
LEFT JOIN nst.item_master_raw m ON m.jan = a.jan
ORDER BY a.detected_at DESC, a.jan
"""

_STOCK_SQL = """
SELECT item_internal_id, SUM(qty_on_hand) AS qty
FROM nst.inventory_snapshot
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM nst.inventory_snapshot)
  AND item_internal_id IN ({ph})
GROUP BY item_internal_id
"""

_RUN_SQL = """
SELECT run_date, scanned, hit, known_stop, errors, newly, gone, baseline, is_partial, created_at
FROM public.discontinue_scan_runs
ORDER BY created_at DESC
LIMIT 20
"""

# 最近一次全量扫描时在范围内的 JAN（试运行不会改 in_scope_run）
_WATCH_SQL = """
SELECT jan, maker, name, status, note, matched_name, detail_url, checked_at, in_scope_run
FROM public.discontinue_watch_status
WHERE in_scope_run = (SELECT MAX(in_scope_run) FROM public.discontinue_watch_status)
"""


def _signal_label(sig: str) -> str:
    if sig in SIGNAL_LABEL:
        return t(SIGNAL_LABEL[sig])
    if str(sig).startswith("boss_flagged"):
        return t("手动标记")
    return sig


def _rollback(conn) -> None:
    """PG は 1 本失敗すると同じトランザクションの後続も全部失敗する → 失敗のたびに戻す。"""
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


@st.cache_data(ttl=600, show_spinner=False)
def _load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    """失败不 st.stop（会连侧边栏以外的内容一起停掉）· 把原因返回给页面显示。"""
    errs: list[str] = []
    alerts, runs, watch = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    with get_readonly_connection() as conn:
        try:
            alerts = pd.DataFrame([dict(r) for r in conn.execute(_ALERT_SQL).fetchall()])
        except Exception as e:  # noqa: BLE001
            errs.append(f"停产记录读取失败: {type(e).__name__}: {e}")
            _rollback(conn)
        if not alerts.empty:
            ids = [str(i) for i in alerts["internal_id"].dropna().unique()]
            alerts["qty"] = None
            if ids:
                try:
                    rows = conn.execute(_STOCK_SQL.format(ph=",".join(["%s"] * len(ids))), ids).fetchall()
                    qty = {str(r["item_internal_id"]): r["qty"] for r in rows}
                    alerts["qty"] = alerts["internal_id"].map(lambda i: qty.get(str(i)) if pd.notna(i) else None)
                except Exception as e:  # noqa: BLE001
                    errs.append(f"在库读取失败（其余照常显示）: {type(e).__name__}: {e}")
                    _rollback(conn)
        try:
            runs = pd.DataFrame([dict(r) for r in conn.execute(_RUN_SQL).fetchall()])
        except Exception as e:  # noqa: BLE001
            errs.append(f"扫描记录读取失败: {type(e).__name__}: {e}")
            _rollback(conn)
        try:
            watch = pd.DataFrame([dict(r) for r in conn.execute(_WATCH_SQL).fetchall()])
        except Exception as e:  # noqa: BLE001
            errs.append(f"监控范围读取失败: {type(e).__name__}: {e}")
    return alerts, runs, watch, errs


alerts, runs, watch, errs = _load()
for e in errs:
    st.error(e)

# ── 监控是否在跑 ─────────────────────────────────────────────
full_runs = runs[~runs["is_partial"].astype(bool)] if not runs.empty else runs
c1, c2, c3, c4 = st.columns(4)
if not full_runs.empty:
    last = full_runs.iloc[0]
    c1.metric(t("最近一次扫描"), str(last["run_date"]))
    c2.metric(t("检查件数"), f"{int(last['scanned']):,}")
    c3.metric(t("数据源命中"), f"{int(last['hit']):,}")
    c4.metric(t("查询失败"), f"{int(last['errors']):,}")
else:
    c1.metric(t("最近一次扫描"), "—")
    st.info(t("还没有全量扫描记录（首次全量为 2026-10-05 周日 02:00）"))

# ── 监控范围 ─────────────────────────────────────────────────
st.subheader(t("📐 监控范围"))
st.markdown(t(
    "- **对象**：NST 中 部门＝輸出事業、取扱区分＝取扱中、等级＝A / B / C / NEW 的商品（每周日 01:30 从 NST 导出）\n"
    "- **数据源**：ハリマ共和物産（NETde卸 的运营方）的在库系统，按 JAN 查询「廃番/廃番予定あり（停产・即将停产）」或「廃番予定なし」\n"
    "- **注意**：数据源只收录ハリマ经销的商品（日用品・化妆品为主）。**数据源查不到的商品，停产了这里也看不到**。"
    "「廃番」和「廃番予定」在数据源里是同一个值，分不出已停产还是即将停产"
))
if watch.empty:
    st.info(t("首次全量扫描（2026-10-05 周日）完成后显示覆盖件数与按厂商的覆盖率"))
else:
    hit_mask = watch["status"].isin(["active", "discontinued"])
    total = len(watch)
    s1, s2, s3, s4 = st.columns(4)
    s1.metric(t("监控对象"), f"{total:,}")
    s2.metric(t("数据源能查到"), f"{int(hit_mask.sum()):,}", f"{hit_mask.mean():.0%}", delta_color="off")
    s3.metric(t("其中 停产・即将停产"), f"{int((watch['status'] == 'discontinued').sum()):,}")
    s4.metric(t("数据源查不到"), f"{int((watch['status'] == 'notfound').sum()):,}")
    st.caption(t("范围基准日") + f": {watch['in_scope_run'].iloc[0]}")

    by = (watch.assign(hit=hit_mask, stop=watch["status"] == "discontinued")
          .groupby(watch["maker"].fillna("—"))
          .agg(n=("jan", "size"), hit=("hit", "sum"), stop=("stop", "sum")))
    by["cov"] = (by["hit"] / by["n"] * 100).round(0)
    by = by.sort_values(["n", "cov"], ascending=[False, False]).reset_index()
    m1, m2 = st.columns([1, 3])
    only_zero = m1.checkbox(t("只看完全查不到的厂商"), value=False)
    bv = by[by["hit"] == 0] if only_zero else by
    m2.caption(t("完全查不到的厂商") + f": {int((by['hit'] == 0).sum()):,} / {len(by):,} · "
               + t("这些厂商的商品停产时本监控无法察觉"))
    st.dataframe(bv.rename(columns={
        "maker": t("厂商"), "n": t("监控件数"), "hit": t("数据源能查到"),
        "stop": t("停产・即将停产"), "cov": t("覆盖率"),
    }), use_container_width=True, hide_index=True, height=320, column_config={
        t("覆盖率"): st.column_config.ProgressColumn(t("覆盖率"), min_value=0, max_value=100, format="%d%%"),
    })

    q = st.text_input(t("按 JAN 查是否在监控范围内"), placeholder="4902806125801").strip()
    if q:
        hit_row = watch[watch["jan"] == q]
        if hit_row.empty:
            st.warning(t("不在监控范围内（不是 輸出事業・取扱中・A/B/C/NEW，或不在最近一次全量扫描里）"))
        else:
            r = hit_row.iloc[0]
            st.write(f"**{r['jan']}** {r['maker'] or ''} {r['name'] or ''}")
            st.write(t("状态") + f": **{t(STATUS_LABEL.get(r['status'], r['status']))}**  ·  "
                     + t("最后检查") + f": {r['checked_at']}  ·  {r['note'] or ''}")
            if r["detail_url"]:
                st.markdown(f"[{t('打开数据源')}]({r['detail_url']})")

# ── 停产记录 ─────────────────────────────────────────────────
st.subheader(t("📋 停产记录"))
if alerts.empty:
    st.info(t("暂无停产记录"))
else:
    alerts["type"] = alerts["signal_type"].map(_signal_label)
    f1, f2, f3 = st.columns([2, 1, 1])
    types = sorted(alerts["type"].unique())
    sel_types = f1.multiselect(t("类型"), types, default=types)
    only_rank = f2.checkbox(t("只看 A/B/C/NEW"), value=True,
                            help=t("按 NST 当前等级。等级已改（如已设为取扱中止）的不显示"))
    only_open = f3.checkbox(t("只看 NST 仍为取扱中"), value=False,
                            help=t("数据源已标停产・即将停产但 NST 还没处理的"))
    v = alerts[alerts["type"].isin(sel_types)]
    if only_rank:
        v = v[v["item_rank"].isin(RANKS)]
    if only_open:
        v = v[v["handling_cd"] == "取扱中"]

    k1, k2 = st.columns(2)
    k1.metric(t("显示件数"), f"{len(v):,}")
    k2.metric(t("其中 NST 仍为取扱中"), f"{int((v['handling_cd'] == '取扱中').sum()):,}")

    show = v[["detected_at", "type", "jan", "name", "maker", "item_rank", "handling_cd",
              "qty", "matched_name", "product_url"]].rename(columns={
        "detected_at": t("检测日"), "type": t("类型"), "jan": "JAN", "name": t("商品名"),
        "maker": t("厂商"), "item_rank": t("等级"), "handling_cd": t("NST 取扱区分"),
        "qty": t("在库（最新快照）"), "matched_name": t("数据源商品名"), "product_url": t("数据源链接"),
    })
    st.dataframe(show, use_container_width=True, hide_index=True, height=520, column_config={
        t("数据源链接"): st.column_config.LinkColumn(t("数据源链接"), display_text=t("打开")),
    })
    st.download_button(t("⬇️ 下载 CSV"), show.to_csv(index=False).encode("utf-8-sig"),
                       file_name="discontinue_records.csv", mime="text/csv")

with st.expander(t("扫描历史（最近 20 次）"), expanded=False):
    if runs.empty:
        st.caption(t("暂无"))
    else:
        st.dataframe(runs.rename(columns={
            "run_date": t("扫描日"), "scanned": t("检查件数"), "hit": t("数据源命中"),
            "known_stop": t("停产・即将停产"), "errors": t("查询失败"), "newly": t("新规停产・即将停产"),
            "gone": t("从数据源消失"), "baseline": t("首次确认时已停产・即将停产"), "is_partial": t("试运行"),
            "created_at": t("记录时间"),
        }), use_container_width=True, hide_index=True)
