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

---

## 增补 v2（2026-06-04）：SKU 360 决策上下文 tab

Boss 追加：风控盘上叠加决策上下文。决策（brainstorming）：回转率=**库存周转率**(月销量/当天JD库存，新列，区别于完売率)；在途PO一对多→**合计在途残 + 供应商distinct列表 + 最近PO号**；呈现=**独立「📋 SKU 360」tab**（三档风控清单保持精简不变）。

- 新 tab 宽表列：item_code/jan/商品名/厂家/商品等级/风险/当月销量/前30天销量/上月销量/完売率/库存周转率/当天库存(JD)/当天库存(弁天)/在途残/在途供应商/最近PO/最近采购价/资金占用 + CSV。
- 数据源：`nst.sales_daily`(前30天滚动) · `nst.inventory_snapshot`(当天库存按 warehouse 拆 JD/弁天) · `nst.purchase_order_line`(closed=FALSE 入荷残) · `nst.item_master_raw.last_purchase_cost`(最近采购价) · 上月销量复用 df_all。各 aux 查询 try/except 兜底，缺数据列显 0。
- 纯函数 `inventory_risk.inventory_turnover(sold, stock)` + 单测（库存≤0→0）。
- SKU 360 尊重上方筛选（月/风险/搜索/预设）。依赖 sales_daily / inventory_snapshot / purchase_order_line 被各自 NST pull 喂数。

---

## 增补 v3（2026-06-04 修正）：风控判断基准 完売率 → 库存月数

Boss 纠正：**风控分档不该用完売率**（那是「每月订货量合不合适」的结果指标），应用**周转率/库存月数**判断补货。

- **库存月数 = 仅 JD 当天库存(`nst.inventory_snapshot` 最新·弁天/在途不算) / 直近月 sold**。
- 阈值改**库存月数**单位（页内可调，默认 补货线=1月 / 压库存线=3月）：
  `< 补货线 → 🔴 断货风险(要补货) / > 压库存线 → 🟡 压库存 / 中间 → 🟢 正常`；月销0+有库存→压库存、月销0+无库存→数据不足。
- **完売率降为参考列**（`完売率(参考)`），不参与分档。
- `classify_risk(stock, monthly_sold, *, reorder_months, overstock_months)` 重写为库存月数判断；阈值 key 改 `reorder_months/overstock_months`；新增 `stock_months()` 纯函数；`enrich` 改用 `current_stock`，资金占用=当前库存×cost_estimate。单测同步重写（16 passed）。
- **新增数据依赖** `nst.inventory_snapshot`（当前JD库存）；该表无数据时页面 warn（有销量者会全判断为断货）。
- 三档清单加列：当前库存(JD) / 库存月数；断货档按库存月数升序（最急在前）。SKU 360 加库存月数列。

---

## 增补 v4（2026-06-04）：断货标记 / 断货率 + 去掉「我的看板」预设

- **去掉「我的看板」预设**（全部SKU/断货+压库存/A/B商品/仅NEW）—— 筛选默认不限风险/等级。
- **断货标记**：`is_stockout = 上月(选中月前一月)有销量>0 且 当前库存(月末在库)=0`（有需求但无货）。
  三档清单 + SKU 列设置加「断货」列（🚫断货）。纯函数 `inventory_risk.is_stockout` + 单测。
- **库存状态筛选**：全部 / 有货(当前库存>0) / 断货(is_stockout)。
- **断货率（按商品等级）**：`stockout_rate_by_rank` 纯函数（仅有等级产品为母数）→ 该等级断货数/总数。
  以**选中月全量**为基数（不受风险/搜索筛选影响），expander 展示 等级×总数×断货数×断货率。单测覆盖 A 级 10/2→20%。
- 全量 137 passed。

---

## 增补 v5（2026-06-04）：去月份 · 前30天销售 + JDL实物库存 · current-snapshot 模型

Boss: 这里不需要月份，只需要前30天的销售数据 + 库存数据(JDL)。页面从「月度模型」重构为「当前快照模型」：
- **去掉**：月份选择、月度活动表(inventory_activity_monthly)依赖、完売率、月末在库、上月销量、仓库筛选、跨月趋势图。
- **销售 = 前30天**：`nst.sales_daily` 直近30日 SUM（qty_sold）。
- **库存 = JDL实物**：`jdl.v_inventory_reconciliation.jdl_qty_in_stock`（按 jan）。
- **可售天数 = JDL库存 ÷ 日均销量(前30天/30)** → 阈值(天)分 3 档（断货线<30天 / 压库存线>90天·页内可调）。
- **断货标记** is_stockout 重定义：前30天有销量 且 当前JDL库存=0。断货率(按等级)保留。
- 只取有等级（item_rank 非空）。资金占用 = JDL库存 × cost_estimate。
- 列：item_code/jan/名/厂家/等级/断货/前30天销量/JDL库存/可售天数/在途残/采购价/资金占用。SKU360 加在途供应商/最近PO。
- enrich/classify_risk 复用（喂 qty_sold=前30天, current_stock=JDL）。PG 实测主查询 7965 有等级/2334 近30天有销。全量 157 passed。
