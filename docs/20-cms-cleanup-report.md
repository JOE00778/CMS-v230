# CMS 整理报告（cleanup report）

> 生成：2026-06-03 · 触发：Boss「开始整理 CMS」· 范围：死代码清理 / 根目录归档 / backlog 整理 / 代码质量扫描
>
> 本报告 = 整理路线图的单一事实源。已完成项见 §1，待 Boss 拍板项见 §2，质量扫描清单见 §4–5。

---

## 1. 本轮已完成（已 commit）

| # | 动作 | 说明 |
|---|------|------|
| 1 | `git rm legacy_streamlit/` | Boss 2026-05-02 提供的旧 order-management-app 截断源码，零引用、纯历史残留物。Boss 授权删除，git 历史可回溯 |
| 2 | `git rm .DS_Store` + `.gitignore` 加 `.DS_Store` | macOS 垃圾文件被追踪，已清并永久忽略 |
| 3 | `订货逻辑.md` → `docs/21-订货逻辑.md` | 散在仓库根的订货逻辑文档（最终版 2026-05-14）归入 docs/ |
| 4 | `tests/integration/test_export_base.py` 加 module-level skip | 该测试 import 已不存在的 `data_warehouse.db.migrations`，导致整个 pytest collection 中断。加 skip 后套件可正常 collect（详见 §3） |

---

## 2. 待 Boss 拍板（不可逆 / 需判断，未擅自动）

### 2.1 `data_warehouse/exports/` 孤儿子系统
现状结构：
```
data_warehouse/
  templates/     ← ✅ 活：nst_item_master / jd_bm_item_master，被 page 03/07/15 在用
  exports/       ← ⚠️ 孤儿：base.py(Exporter基类) + cost_update.py，全仓库零 runtime 引用
  warehouse.db   ← gitignore
  （db/ 子模块已消失 —— exports 与 test_export_base 都依赖它，现已半坏）
```
- `exports/` 零 page/module/shared 引用，其依赖 `data_warehouse.db.migrations` 已被删除 → 处于半坏状态。
- **决策**：① 删整组（`exports/` + `test_export_base.py`）；② 修复接线（补回 db 模块或改接 `shared/schema_bootstrap.py`）让 cost_update exporter 重新可用。
- ⚠️ CLAUDE.md「踩雷区」里「`data_warehouse/` 是死代码」「`shared/{xml_xls,filters}.py` 残留」的描述**已过时**：templates 是活代码、xml_xls/filters 早已不存在。本报告 §6 已同步修正该雷区条目。

---

## 3. 🚨 测试健康警报（系统性腐烂）

修复 collection 中断后，套件真实状态浮现：**35 failed / 85 passed / 1 skipped**。

| 测试文件 | 失败数 | 根因 |
|---|---|---|
| `tests/test_health_metrics.py` | 16 | 同下 |
| `tests/unit/test_purchase_engine.py` | 15 | `sqlite3.OperationalError: no such table: nst.sales_monthly` |
| `tests/test_rank_classifier.py` | 4 | 同下 |

**根因统一**：业务代码已迁移到 **PostgreSQL**（表名带 `nst.` schema 前缀），但测试 fixture 仍是 **SQLite**（不识别 schema.table）。这是 SQLite→PG 迁移没跟上的测试腐烂，**不是本轮整理引入的**（之前 collection error 一直掩盖着它）。

**影响**：当前没有可信的回归安全网。任何代码重构（§4–5）在测试修复前都缺乏 verify 基础。

**建议**：作为独立任务「T-CMS-test-pg-fixture：测试套件适配 PG schema」立项，优先级高于代码重构。

---

## 4. pages/ 质量扫描（29 个 Streamlit 页）

### 跨页重复样板（抽到 shared/ 收益最高）
- **`_df()` SQL→DataFrame** → 出现在页 06/14/17/18/25/28/30/32（8 处完全相同）→ 抽到 `shared/db_helpers.py`，消约 120 行
- **`_query()` SQL 执行+错误处理** → 页 02/04/05/27（4 处变体），均返回 `(df, error)` 元组
- **页面初始化样板** → 全 29 页重复 `require_password()` + `inject_theme()` + `lang_selector()` + `get_connection()` → 抽 `shared/page_init.py:init_page()`
- **`st.download_button` 文件命名** → 页 02/03/04/06/07/09/10/14/15/17 无统一命名规则

### 不一致约定
- **i18n 策略分裂**：页 02/04 用本地 `_L("zh","ja")` lambda（违反 CLAUDE.md），其余页用 `t()` 中文 key。建议停用 `_L()`，统一 `t()` + 在 `shared/i18n.py` 补日文条目。
- **异常处理**：13 处 `except Exception: pass`（页 02:83, 04:82, 05:41/44/209, 14:114/378, 19:98, 22:215/305, 26:74/76）静默吞错。
- **session_state key 命名**无规范（业务前缀 / 页面前缀混用）。
- **`@st.cache_data`** ttl 不统一（300 / 120 / 无）。

### 脆弱点（优先修）
- 🔴 **页 19:96** 硬编码 Lark token 作 `st.secrets.get(...)` 默认值 → 应仅从 secrets 读（呼应「凭证不进 chat/代码」红线）
- 🟠 **页 07:31** 硬编码 `warehouse.db` 路径 → 改用 `get_connection()`
- 页 07:5 `import sqlite3` 未使用（死 import）

---

## 5. modules/ + shared/ 质量扫描（22 文件）

### 死代码 / 职责重叠
- `shared/auth.py:show_role_badge()` 空实现（函数体仅 `return`）
- `shared/lark_auth.py` / `lark_notify.py` / `lark_openapi.py` 三模块职责重叠：`lark_notify._send_bot_*` 只是再包了一层 `lark_openapi.im_send_*` → 可消中间层
- `kpi_history.py` / `supabase_client.py` 仅 `cms.py` 调用、无 page 级引用（确认是否仍需）

### 重复实现
- **`_secret()` × 4**（auth / lark_notify / lark_openapi / n8n_client）逻辑相同（secrets 优先 + env fallback）→ 抽 `shared/config.py` 统一。⚠️ 注意三者 fallback 优先级实际不一致（auth/lark_auth 走 env 优先，lark_notify 走 secrets 优先，n8n_client 注释声称与 auth 一致实则不是）——合并时要先定一个口径。

### 脆弱点
- 🔴 **`kpi_history.py:117-146`** `take_snapshot()` 双层 `except: pass` 吞异常 → UPSERT 失败时 KPI 快照静默不更新、home 仪表盘数据陈旧无告警（13 处静默 except）
- 🟠 **`kpi_history.py:88-90`** SQL 用 f-string 拼接 `ym`（当前来源 `datetime.now()` 相对安全，但易被后续改成用户输入）→ 参数化
- `db.py:83/98/106/125` schema 初始化吞异常，掩盖 DB 状态
- `lark_openapi.py:30` 模块级全局 token 缓存，多进程/多用户无隔离

---

## 6. 建议的整理路线图（按优先级）

| 优先级 | 任务 | 工作量 | 前置 |
|---|---|---|---|
| **P0** | 测试套件适配 PG schema（§3）—— 恢复回归安全网 | 中 | 无 |
| **P1** | 修脆弱点：页 19 硬编码 token、页 07 硬编码路径、kpi_history 吞异常加日志 | 小 | 无 |
| **P1** | 拍板 `data_warehouse/exports/` 去留（§2.1） | 小 | Boss |
| **P2** | 抽 `shared/db_helpers.py`（`_df`/`_query`）+ `shared/page_init.py` | 中 | P0（需测试 verify） |
| **P2** | 统一 i18n：停用 `_L()`，页 02/04 改 `t()` | 中 | P0 |
| **P3** | 抽 `shared/config.py` 统一 `_secret()`（先定 fallback 口径） | 小 | 无 |
| **P3** | 规范 lark_* 三模块路由、统一异常处理 | 中 | P0 |

> 注：P2 及以后的代码重构均依赖 P0（测试修复）提供 verify 基线，否则重构无安全网。
