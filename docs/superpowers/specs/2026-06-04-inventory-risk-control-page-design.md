# 库存风控页（page18 订货依据 → 库存风控）重构设计

> 2026-06-04 · Boss 拍板「整理订货依据，改名库存风控」· brainstorming 收敛
> 关联：[docs/17-订货算法选型.md](../../17-订货算法选型.md) · [shared/order_settings.py](../../../shared/order_settings.py)

## 背景与决策

page18「订货依据」职责模糊：既按月完売率分风险档（断货/压库存/正常），又出一套简易订货量
（×1.5/×1.0/×0.5），后者与 page25「発注AI v2」的权威下单引擎（Boss 2026-05-14 公式·
多仕入先）重叠冲突。改名「库存风控」是把职责钉死的契机。

Boss 决策（4 + 1）：
1. **范围** = 内容/UX 重构（非最小改动）。
2. **职责** = 纯风控监控盘，**去掉订货量**，下单口径完全交给 page25。
3. **数据源** = 迁到 NST 自动源 `nst.inventory_activity_monthly` + `nst.item_master_raw`
   （与 page25 引擎同一权威源，免手动 Excel 上传、两页完売率一致）。
4. **力度** = 风控驾驶舱（顶部风险概览 + 三档风险清单 + 资金占用视角）。
5. **阈值** = 页内**可输入控制 tab**，Boss 随时调；**独立持久化**，不与発注AI 系数阈值耦合。

三页分工（各管一把尺、不重叠）：
- 🛡️ **库存风控**（本页）= 月完売率尺 → 断货 / 压库存 风险识别（只监控）。
- 📦 **発注AI v2**（page25）= 唯一下单引擎（精确补货量 + 仕入先）。
- 🩺 **库存健康度**（page06）= 库存月数 / GMROI 尺。

## 数据层

### 查询（迁到 nst.*）
```sql
SELECT im.item_code, im.jan, im.display_name, im.item_rank AS rank, im.maker,
       im.cost_estimate,
       a.location, a.year_month,
       a.opening_qty, a.received_qty, a.sold_qty AS qty_sold, a.closing_qty AS close_qty
FROM nst.inventory_activity_monthly a
JOIN nst.item_master_raw im ON im.internal_id = a.item_internal_id
```
- `available_qty = opening_qty + received_qty`
- `sell_through_rate = sold_qty / available_qty`（available=0 → 0）
- `资金占用(¥) = closing_qty × cost_estimate`（在库資金 = 压库存的资金占用，替代旧
  `close_amount/out_amount`——nst 源只有数量，cost_estimate 来自 item_master_raw）
- 依赖：`nst.inventory_activity_monthly` 由 NST `receipts_daily`（06:30）自动喂数。

### 新模块 `shared/inventory_risk.py`（纯逻辑、可单测）
- `RISK_LABELS`：断货风险 / 正常 / 压库存 / 数据不足。
- 阈值持久化（独立于 order_settings）：
  - `_DEFAULT = {"high": 0.9, "low": 0.5}`，存 `data/files/inventory_risk_thresholds.json`
    （环境变量 `INVENTORY_RISK_THRESHOLDS` 可覆盖路径，缺失回退默认）。
  - `load_risk_thresholds() -> dict` / `save_risk_thresholds(d) -> None`（仿 order_settings）。
- `classify_risk(sold, available, *, high=0.9, low=0.5) -> str`（纯函数）：
  - `available <= 0` → 数据不足
  - `rate = sold/available`：`≥high` → 断货风险 · `≥low` → 正常 · `<low` → 压库存
- `enrich(df, thresholds) -> df`：给 DataFrame 补 `available_qty / sell_through_rate /
  risk_label / capital_exposure` 列（页面只调这个，逻辑全在模块内）。

## 页面结构（驾驶舱）

文件 `pages/18_🛡️_库存风控.py`（git mv + 重写）。

1. **标题**：`🛡️ 库存风控` · caption「按月完売率识别断货/压库存风险 · 🛒 精确补货量请用『発注AI v2』」。
2. **顶部风险概览卡**（st.columns）：覆盖月数 · SKU总数 · 🔴断货风险数 · 🟡压库存数 · 🟢正常数 ·
   **💰压库存资金占用(¥)**（头条 = Σ 压库存 SKU 的 capital_exposure）。
3. **筛选**：月份 / 仓库(location) / 风险等级 / JAN·item_code 搜索 + 保留「🗂️ 我的看板」预设。
4. **⚙️ 阈值设定 tab**（或 expander）：两个 `number_input`（断货风险≥ default 0.9 · 压库存< default 0.5），
   保存按钮 → `save_risk_thresholds`；改后整页风险分档实时重算。
5. **三档风险清单 tab**：
   - 🔴 断货风险（≥high）· 按完売率降序
   - 🟡 压库存（<low）· **按资金占用降序**（最压钱的在前）
   - 🟢 正常（low~high）· 按完売率降序
   - 列：item_code · jan · 商品名 · 仓库 · 月销量 · 合计可售 · 期末库存 · 完売率 · **资金占用(¥)**。
     **无「建议订货量」**。CSV 下载保留。列显示 toggle 保留。
6. **单 SKU 跨月趋势图**（迁到 nst.* 源，保留 altair 折线）。

## 删除清单
- `建议订货量` 列 · `_suggest_qty()` · ×1.5/×1.0/×0.5 订货文案 · "区分订货策略" caption。
- 旧数据源 `item_monthly_turnover` / `item_v2` 查询。
- 密度 toggle（紧凑/标准/宽松，价值低）。

## 改名联动（全部已定位）
| 位置 | 改动 |
|---|---|
| `pages/18_📦_订货依据.py` | `git mv` → `pages/18_🛡️_库存风控.py` |
| 页内 | docstring / `st.title` / `page_config(page_title,page_icon="🛡️")` / caption |
| `cms.py:414` | 首页卡片 `📦 订货依据` → `🛡️ 库存风控` + 描述改风控口径 |
| `shared/i18n.py:642` | nav 注册 path + label |
| `shared/i18n.py:1135,1137` | 旧翻译词条 + 新增「库存风控」中日词条 |
| `README.md:92` | 表格行 |
| `docs/17:14` | path + 把 B 定性从「看板订货」改「风控监控·不下单」 |

## 测试 / 验证
- **`tests/test_inventory_risk.py`**：`classify_risk` 边界（rate=high/low 边界、available=0、sold=0）
  + `load/save_risk_thresholds` round-trip。唯一能自动测的部分。
- 页面：ast 语法检查 + `shared.db_helpers.df` 既有 smoke + Boss 在 streamlit 实机扫一眼
  （pages 无自动测试覆盖，需人工目视）。
- 回归：全量 pytest 仍绿（≥117 + 新测试）。

## 范围外（不动）
- page25 発注AI v2 / page06 库存健康度 / purchase_engine：不碰。
- 订货量 / 仕入先选择逻辑：全部留在 page25。
- `nst.inventory_activity_monthly` 的 ingest 管道（database 仓 NST API）：本次不改。
