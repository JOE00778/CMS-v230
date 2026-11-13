# 新品订货申请（page48）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新页 page48：新品申请 → 确认 → NST 发注书 CSV → 按 NST PO 同步自动跟踪到货、超期置顶、到货率。

**Architecture:** 申请存 `sourcing.new_item_request`；发注/到货不落表，每次由纯函数 `track()` 从 `nst.purchase_order_line ⋈ nst.item_master_raw` 现算。发注书复用 page10 逻辑（抽到 `shared/po_csv.py`）。

**Tech Stack:** Streamlit · PG（shared.db `?` 占位符）· pandas · pytest。

**Spec:** `docs/superpowers/specs/2026-11-13-new-item-order-request-design.md`

## Global Constraints
- 超期默认 = 发注日 + 14 天（PO 行有 入荷预定日 时用预定日）。
- 不分角色；确认/取消记录操作人（飞书登录名，无则「密码登录」）。
- 不写 NST/斑马/Shopify；page10 输出逐字不变。
- 全部 UI 文案 `t()` + `shared/i18n.py` 中日翻译，`tools/i18n_scan.py` gaps=0。
- commit 用 pathspec，不带 uv.lock。

## Review Focus
1. JAN 两边写法不同（前导 0、空格）→ 两边都过 `normalize_jan` 后才对得上。→ Task 2 测试。
2. 同 JAN 已有未完成申请又提一次 → 挡下并给已有申请号。→ Task 2 测试。
3. PO 行没有入荷预定日 → 期限 = 发注日 + 14。→ Task 2 测试。
4. 一个申请对应多张 PO（分批下单/分批到）→ 数量求和，部分到货。→ Task 2 测试。
5. 申请的 JAN 在 NST 没建档 → 发注书把它列为缺档、不进 CSV、不报错。→ Task 1 测试（missing）。

---

### Task 1: `shared/po_csv.py`（page10 发注书逻辑抽出）

**Files:** Create `shared/po_csv.py` · Modify `pages/10_📦_発注書作成.py` · Test `tests/test_po_csv.py`

**Interfaces — Produces:**
- `EMPLOYEES, DEPARTMENTS, LOCATIONS`（page10 原常量原值）
- `tax_rate_from_schedule(ts) -> int | None`
- `load_item_master(conn) -> pd.DataFrame`（jan/item_code/display_name/tax_schedule；JAN 规范化、同 JAN 留 last_modified 最新）
- `build_output(df_order, df_item, *, ext_id, sup, order_date: date, employee, department, memo, location, tax_pct) -> (df_out, missing)`

- [ ] Step 1 test：`build_output` 对 `jan=[4900000000011, 4900000000099]`、数量 [3,2]、単価 [100,50]、item 只有第一个（tax_schedule「仕入8%＆販売免税」）→ df_out 1 行：列顺序 `外部ID,仕入先,日付,従業員,部門,メモ,場所,アイテム,数量,単価/率,金額,税額,総額`，金額 300、税額 24、総額 324、日付 `2026/11/13`；missing 的 jan = ["4900000000099"]。`tax_rate_from_schedule("免税")==0`、`("消費税10%")==10`、`(None) is None`。
- [ ] Step 2 运行确认 FAIL（模块不存在）。
- [ ] Step 3 把 page10 的 `_tax_rate_from_schedule`/`_build_output`/item master 读取/三常量搬进 `shared/po_csv.py`，page10 改 import 并把原全局值作参数传入。
- [ ] Step 4 `uv run pytest -q tests/test_po_csv.py` PASS；`python -m py_compile` page10。
- [ ] Step 5 commit。

### Task 2: `shared/new_item_request.py`（纯逻辑 + 薄 DB 层）

**Files:** Create `shared/new_item_request.py` · Test `tests/test_new_item_request.py`

**Interfaces — Produces:**
- `OVERDUE_DAYS = 14`
- `normalize_jan(s) -> str`（strip、去 `^0{5,}`；非 8–14 位数字返回 ""）
- `track(reqs: list[dict], po_lines: list[dict], today: date) -> list[dict]`
  - req 键：id, jan, status(requested/confirmed/cancelled), requested_at(datetime), confirmed_at
  - po 行键：jan, po_number, trandate(date), quantity, quantity_received, expected_receipt_date(date|None), closed(bool)
  - 每个 req 加：`state` ∈ {"待确认","待发注","在途","部分到货","超期","已到货","PO关闭未到齐","已取消"}、`ordered`, `received`, `po_numbers`(list), `deadline`(date|None), `overdue_days`(int)
- `open_jan_conflicts(new_jans: list[str], tracked: list[dict]) -> dict[str, int]`（jan → 未完成申请 id；未完成 = 待确认/待发注/在途/部分到货/超期）
- `arrival_summary(tracked, today, days=90) -> dict`（confirmed_at 在 90 天内、非取消：total/arrived/in_transit/overdue/unordered/closed_short/rate）
- DB：`ensure_table(conn)`、`load_requests(conn)`、`load_po_lines(conn, jans)`、`insert_requests(conn, rows, user)`、`confirm(conn, items: list[dict], user)`（id/supplier/unit_price/confirmed_qty）、`cancel(conn, ids, user)`

- [ ] Step 1 tests（today=2026-11-13）：
  - 待确认 / 已取消 原样；confirmed 无 PO 行 → 待发注。
  - PO trandate 早于申请日的行不算（申请 2026-11-01，PO 2026-10-20 → 待发注）。
  - 两张 PO（10+5，入荷 10+0，无预定日，发注日 11-05/11-10）→ 部分到货、ordered 15、received 10、deadline 11-24、不超期。
  - 一张 PO 发注 10-20、无预定日、入荷 0，申请 10-15 → 超期、deadline 11-03、overdue_days 10。
  - 预定日 11-20 的行 → deadline 11-20 不超期（在途）。
  - 入荷 ≥ 发注 → 已到货；全部 closed 且未到齐 → PO关闭未到齐。
  - `normalize_jan(" 0000004901234567890 ")=="4901234567890"`、`("abc")==""`。
  - `open_jan_conflicts(["4901"...])` 对 待发注 的同 JAN 返回其 id，对 已到货/已取消 不返回。
  - `arrival_summary`：已到货 1、超期 1、待发注 1 → total 3、rate 1/3。
- [ ] Step 2 FAIL。
- [ ] Step 3 实现。期限 = 未到齐行的 (预定日 or 发注日+14) 取最早；超期 = today > deadline。DDL 同 page34 风格（CREATE TABLE IF NOT EXISTS，列见 spec）。`load_po_lines` SQL：`pol ⋈ item_master_raw im ON im.internal_id = pol.item_internal_id WHERE im.jan = ANY(?)`，jan 规范化后返回。
- [ ] Step 4 PASS。
- [ ] Step 5 commit。

### Task 3: page48 + 侧边栏 + 翻译

**Files:** Create `pages/48_🆕_新品订货申请.py` · Modify `shared/i18n.py`（`_NAV_GROUPS` 订货管理首位 + 中日翻译）

- [ ] Step 1 页面按 spec「页面」四块 + 顶部数字实现；`require_password()`；用户名 `st.session_state.get("__lark_user", {}).get("name") or t("密码登录")`；发注书用 `po_csv.build_output`（仕入先各一个外部 ID `{ts}{n}`，合并下载）。
- [ ] Step 2 `uv run python tools/i18n_scan.py --summary cms.py pages/*.py shared/*.py` → gaps=0；`uv run pytest -q` 全绿；py_compile。
- [ ] Step 3 commit。

### Task 4: 线上验证 + 部署

- [ ] 元川容器内只读：`ensure_table` 之外不写；用真 JAN（近 30 天 PO 的 3 个）造内存 req → `track()` 结果与 `nst.purchase_order_line` 手算一致。
- [ ] push → 元川 pull → `docker restart cms_streamlit` → 容器内 grep 新页存在 + health 200。
- [ ] cc-fleet 事件流写回。
