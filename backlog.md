# 商品信息管理 Backlog v2

> **⚠️ 2026-06-03 现状校正（v2 已严重过时）**：下方 v2 描述停留在 2026-05-05，与现状差距很大——
> - modules/ 现有 **5 个**（cost_sync / image_translate / inventory_health / operation_advice / rank_classifier），非 v2 说的「只有 cost_sync」
> - Streamlit 页现有 **29 个**（pages/02~32），非 v2 说的「6 页」
> - 数据源已从本地 SQLite 迁移到 **PostgreSQL**（database 仓库为单一事实源），下方「NetSuite→CSV→SQLite」链路 + T-101~T-103 ingest 任务多已被 database 仓库的 ingester 取代
> - **死代码 / 代码质量 / 测试健康的完整清单与整理路线图见 [docs/20-cms-cleanup-report.md](docs/20-cms-cleanup-report.md)**（新单一事实源）
> - 当前测试套件 35 failed（SQLite→PG 迁移腐烂），重构前须先修
>
> 下方 v2 原文保留作历史参考。


> **2026-05-05 v2 更新**：标记已完成项 + 在 PORTFOLIO v2 里降级到 Tier P1（让位 shopify 上线冲刺）。
>
> 数据源 = NetSuite Saved Search → CSV → 本地 SQLite。
> 设计文档：`~/.claude/plans/tidy-yawning-pony.md`（5 phase / 10 模块）。
>
> **真实进展（5/05 实测）**：
> - Phase 1 完成度 70%：cost_sync/ 模块已写（rules.py 实现）+ 6 Streamlit 页全建
> - warehouse.db 12MB 真数据（sales_line 表 15,045 行）
> - **118 个真实测试函数**（unit + integration · 1,225 行测试代码）
> - modules/ 实际只有 cost_sync/，其余 9 模块未实现（符合原 backlog 描述）
> - item 表计数 = 0（无真实商品数据导入）

| Owner 标记 | 说明 |
|---|---|
| `boss` | NetSuite 操作 / Saved Search 配置 / CSV 上传 |
| `haiku` | 模板化代码、ingest/export、pytest |
| `opus` | 算法设计、规则定义、跨模块 schema |
| `mix` | 先 opus 设计后 haiku 实现 |

---

## D 决策类（先扫清）

| ID | 题目 | Owner | Prio |
|----|------|-------|------|
| D-101 | **6 大决策一次性敲定**：① 6+ 个 NetSuite Saved Search 命名 / 字段 / 频率 ② 订货算法选型（再订货点 vs EOQ vs Min/Max）③ PO 创建审批流程（直接生成 vs Boss 复核）④ 商品登录字段最小集 ⑤ ~~Streamlit 部署位置~~ ✅ 2026-05-08 已定 Docker on 元川さん（Inspiron 5405）⑥ 数据备份策略（PG dump 频率 / 位置） | boss + opus | P0 |

---

## 基础设施 · 数据导入完善（5 条）

> 现状：`ingest/items.py` 已写。剩 5 个域。

| ID | 题目 | Owner | Prio | Est | 备注 |
|----|------|-------|------|-----|------|
| T-101 | NetSuite Saved Search 字段约定文档（`docs/netsuite_saved_searches.md`）| opus | P0 | 1 | D-101 ① 决定后展开 |
| T-102 | sales ingestor（订单明细 → `sales` 表）| haiku | P1 | 2 | 依赖 T-101 |
| T-103 | purchases ingestor（PO + Receipt + Bill → `purchase` 表）| haiku | P1 | 2 | 依赖 T-101 |
| T-104 | inventory ingestor（库存快照 → `inventory` 表）| haiku | P1 | 2 | 依赖 T-101 |
| T-105 | suppliers + lots ingestor（供应商 + 批次/赏味期限）| haiku | P2 | 2 | 依赖 T-101 |

---

## 模块实现（10 个模块，按 plan 的 phase 排）

### Phase 1 已完成 / 收尾

| ID | 题目 | Owner | Prio | Est | 备注 |
|----|------|-------|------|-----|------|
| T-106 | 模块 #1 成本同步收尾（生产数据真跑 + 输出审计 + 回写 NetSuite CSV 验证）| opus | **P1** | 2 | cost_sync/ rules.py + 测试已就绪，**差端到端真跑** |
| T-107 | 模块 #7 商品情报检索增强（多维筛选 + 全文搜索 + 导出）| haiku | **P1** | 2 | page 已存在，逻辑沉淀到 `modules/product_search/`；**首批落 .tasks/** |

### Phase 2

| ID | 题目 | Owner | Prio | Est |
|----|------|-------|------|-----|
| T-108 | 模块 #6 店铺别毛利（`modules/store_margin/` + 整月/单月切换 + Plotly 图）| haiku | P1 | 3 |
| T-109 | 模块 #8 销售数据查询（`modules/sales_query/` + 时间序列 + 维度交叉）| haiku | P1 | 3 |

### Phase 3

| ID | 题目 | Owner | Prio | Est | 备注 |
|----|------|-------|------|-----|------|
| T-110 | 模块 #4 库存健康监控（周转率 / 交叉比率 / 安全库存 + alerts 表）| opus + haiku | P1 | 3 | page 已建占位 |
| T-111 | 模块 #2 商品等级自动判定（销量分位 + 库存周转规则 + `rank_history` 表 + 回写 NetSuite）| mix | P2 | 4 | 依赖 T-102/T-104 |

### Phase 4

| ID | 题目 | Owner | Prio | Est |
|----|------|-------|------|-----|
| T-112 | 模块 #3 SKU 进货价格波动（z-score / 同比 / 阈值告警 + `price_volatility_alerts` 表）| mix | P2 | 3 |
| T-113 | 模块 #9 赏味期限管理（30/60/90 天分级告警 + 库存价值合计）| haiku | P2 | 2 |

### Phase 5

| ID | 题目 | Owner | Prio | Est | 备注 |
|----|------|-------|------|-----|------|
| T-114 | 模块 #5 自动订货系统（D-101 ② 算法定后实现 + `order_proposals` 表 + PO export）| opus + haiku | P2 | 5 | 最复杂 |
| T-115 | 模块 #10 商品登录工作流（新品输入表单 → `item_create.csv` 给 NetSuite Import）| mix | P2 | 3 |

---

## 跨模块基建（4 条）

| ID | 题目 | Owner | Prio | Est | 备注 |
|----|------|-------|------|-----|------|
| T-116 | 共享 UI 组件库完善（`shared/filters.py / kpi_cards / tables / alerts` 全实现 + page 替换）| haiku | P1 | 2 | 减少 page 重复代码 |
| T-117 | 测试覆盖到 60%（pytest unit + integration · ~~现在几乎无测试~~ → 已有 118 个测试，差缺口模块）| haiku | **P1** | 3 | **已有基础**，补 Phase 2 模块 + ingestor 测试；**首批落 .tasks/** |
| T-118 | 端到端真实数据冒烟（7,403 SKU + 6 个域 saved search 全跑一遍）| opus | P1 | 2 | 上线前必做 |
| T-119 | 部署落地（D-101 ⑤ 决定后实施：本地 launch script / Cloud / Docker）| haiku | P2 | 2 | |

---

## 统计

- **总条数**：20（1 决策 + 19 执行）
- **Haiku 主导**：11 条
- **Opus 主导**：3 条
- **Mix**：5 条
- **Boss 后台**：1 条（D-101）
- **Est 总计**：约 49 轮

## 关键路径

```
D-101 ─→ T-101 ─→ T-102/T-103/T-104/T-105 (4 ingest 并行)
                    ↓
                  T-108~T-115 (10 模块) ─→ T-116/T-117/T-118 ─→ T-119 部署
T-106/T-107 已基础上可独立先跑（不阻断主链）
```

## 开工顺序建议（v2）

> 与 shopify 上线冲刺**并行**：商品信息管理在 P1 池，shopify 阻塞 quota 时这边继续推进。

1. **本周内**：T-107 商品情报检索增强（haiku 主导，独立任务）+ T-117 测试覆盖（独立）+ T-106 端到端真跑（依赖 NetSuite 真 CSV）
2. **第 2 周**：D-101 决策（Boss 抽 1-2h 扫）+ T-101 文档
3. **第 3-4 周**：T-102/T-103/T-104/T-105 并行（4 个 ingestor）— 依赖 D-101
4. **第 5-6 周**：Phase 2/3 模块（T-108/T-109/T-110/T-111）
5. **shopify 上线后**：Phase 4/5 模块（T-112~T-115）+ T-116/T-118/T-119

---

## v1 → v2 主要变化

1. ✅ **更新真实进展**：cost_sync/ 已写 + 118 测试 + 12MB warehouse.db（v1 描述过于保守）
2. ✅ **首批 3 条落 .tasks/**：T-106 / T-107 / T-117（独立可跑、不依赖 D-101）
3. ✅ **跟 shopify 上线冲刺并行**（P1 池，shopify 阻塞时这边推进）

---

> **本地路径**：`~/CC/商品信息管理/`
> **数据库**：`~/CC/商品信息管理/data_warehouse/warehouse.db`（12MB · 15 张表 · sales_line 15045 行真数据）
> **上一版**：v1（2026-05-04）见 git history
