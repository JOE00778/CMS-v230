# 飞书机器人对话查询 CMS 数据 · 设计文档

> 日期：2026-06-10 · 状态：**已获 JO 认可，待实现** · 作者：JO + Claudex
> 关联：[SECURITY-DX-REVIEW-lark-chatbot.ja.md](../../SECURITY-DX-REVIEW-lark-chatbot.ja.md)（DX 安全评审）
> 落点：`deploy/n8n/cms_api/`（FastAPI sidecar，复用其 PG 连接 + 签名经验）

## 1. 目标与范围

在飞书**私聊机器人**或**群里 @机器人**，用**固定命令**查询 CMS 的商品 / 库存 / 发注数据，机器人查 PG 后在飞书回复。

- **MVP 范围**：3 类查询命令（商品 / 库存 / 发注）+ 帮助；固定命令格式（非自然语言）
- **不在本期**：自然语言 / AI 理解（NL2SQL）、写操作、复杂图表
- **成功标准**：① 飞书发 `库存 <JAN>` 能收到该商品各仓库存回复 ② 外部企业的人发消息被挡 ③ 所有查询走参数化、零 SQL 注入 ④ 查询留审计日志

## 2. 关键决策（JO 拍板）

| # | 决策 | 选定 |
|---|---|---|
| 1 | 敏感数据（原价/发注金额）返回策略 | **全返回**（跟操作页全开放一致，内部全信任） |
| 2 | 交互方式 | **固定命令**（MVP；自然语言留后续） |
| 3 | 架构 | **cms_api 加 `/lark/event`**（代码可审计，复用 PG/签名；非 N8N） |
| 4 | 渠道 | **私聊 + 群@ 都支持** |

## 3. 架构 / 数据流

```
用户 飞书私聊 or 群@机器人（例：「库存 4901301447647」）
  → 飞书开放平台 推 im.message.receive_v1 事件（带签名）
  → POST https://cms-bot.smikie-cms.cc/lark/event   （经现有 cloudflared，0 新端口）
  → cms_api /lark/event：
       ① 验签（Verification Token + 签名）— 非飞书来源 401
       ② URL challenge 应答（飞书首次校验回调用）
       ③ sender 校验（tenant_key = 本企业，挡外部企业）
       ④ 解析固定命令 → (intent, params)
       ⑤ 参数化查 PG（绑定参数，不拼 SQL）
       ⑥ lark_openapi.im_send_text 回复（私聊回 sender / 群回 chat_id）
       ⑦ 审计日志（who / when / 命令 / 结果条数）
  → 用户在飞书看到结果
```

## 4. 组件（都在 cms_api 内，边界清晰）

| 组件 | 文件 | 职责 | 依赖 |
|---|---|---|---|
| 事件端点 | `app.py` 加 `/lark/event` | 收事件、验签、challenge、分发、回复 | FastAPI、lark_bot、lark_openapi |
| 命令处理 | `lark_bot.py`（新） | 解析固定命令 → 查询 → 组织回复文本 | PG（psycopg）、查询模板 |
| 验签 | `lark_bot.py` 内 | HMAC 验飞书事件签名 | 复用 `_shopee_sign` 同款 hmac 模式 |
| 回复 | 复用 `shared/lark_openapi.im_send_text` | 发消息到飞书 | LARK_APP_ID/SECRET（已配） |
| 审计 | `lark_bot.py` 内 → PG `audit.lark_bot_log` | 记 open_id/时间/命令/条数 | PG |

## 5. 固定命令规格（MVP）

| 命令 | 格式 | 查表 | 返回字段 |
|---|---|---|---|
| 商品 | `商品 <JAN或商品名>` | `nst.item_master_raw` | 品名 · JAN · 规格 · メーカー · 等级 · 建立日期 |
| 库存 | `库存 <JAN或商品名>` | `nst.inventory_snapshot`（最新 snapshot_date） | 各仓库存数量 · 在库月数 |
| 发注 | `发注 <供应商> [年月]` | `nst.po_item_supplier_monthly` | 发注明细 · **金额（全返回）** |
| 帮助 | `帮助` / `help` | — | 命令列表 + 示例 |

- 入参（JAN / 商品名 / 供应商 / 年月）**全部作为 SQL 绑定参数**，不进字符串拼接。
- 商品名/供应商支持模糊匹配（`ILIKE %x%`，仍参数化）。
- 结果过多（如 >20 行）截断 + 提示「结果较多，请精确到 JAN / 去 CMS 网页看」。

## 6. 安全（对齐 DX 评审）

- **验签**：飞书「事件与回调」给的 Verification Token + 请求签名，cms_api 逐条验，失败 401。可选 Encrypt Key 解密事件体。
- **sender 校验**：事件 `tenant_key` = 本企业租户 → 放行；外部企业（群里万一有外部协作者）→ 拒 + 记日志。
- **参数化查询**：所有用户输入绑定参数，零 SQL 注入面。
- **数据全返回**（JO 定）：不做字段脱敏；但**审计日志**记录每次查询（谁、何时、查了什么）。
- **入口**：复用现有 cloudflared 加 public hostname，**不开新公网端口**；cms_api 仍只内网监听 8789，cloudflared 终结 TLS。
- **凭据**：Verification Token / Encrypt Key 走 env（元川机器写，不入 chat/代码）。

## 7. 错误处理

| 情况 | 行为 |
|---|---|
| 命令不认识 | 回「未知命令，发『帮助』看用法」 |
| 查无结果 | 回「没找到 "X"」 |
| 查询/DB 异常 | 回「查询出错，请稍后再试」+ 记日志（不暴露堆栈） |
| 验签失败 | HTTP 401，不回复 |
| sender 非本企业 | 不回复 + 记日志 |
| 飞书 challenge | 原样回 challenge 值（200） |

## 8. 测试

- **单元**：① 命令解析（各命令 + 边界：空参/多空格/未知）② 验签（正确签名过、错误签名拒）③ challenge 应答 ④ 查询 SQL（SQLite ATTACH/PG fixture，复用 `tests/nstdb.py` 模式）
- **范式**：复用 `deploy/n8n/cms_api/tests/test_shopee_signing.py` 的签名测试结构
- **联调**：飞书后台配好后，私聊机器人发各命令 + 群@ 验证

## 9. 部署 / 配置（实现后，JO/元川 做）

1. **Cloudflare dashboard**：Zero Trust → 现有 Tunnel → 加 public hostname `cms-bot.smikie-cms.cc` → service `http://cms-api:8789`
2. **飞书后台**「事件与回调」：订阅 `im.message.receive_v1`，回调 URL = `https://cms-bot.smikie-cms.cc/lark/event`；记下 Verification Token / Encrypt Key
3. **元川 env**（cms-api 容器）：加 `LARK_EVENT_VERIFICATION_TOKEN` / `LARK_EVENT_ENCRYPT_KEY`
4. cms-api 是 N8N stack 服务 → 改了依赖要 build（redeploy）；只改代码则重启 cms-api 容器

## 10. 实现步骤（writing-plans 细化）

1. `lark_bot.py`：验签 + challenge + 命令解析骨架 + 单元测试
2. 3 个查询模板（商品/库存/发注）+ 参数化 SQL + 测试
3. `/lark/event` 端点接线（app.py）+ 回复（im_send_text）+ 审计表
4. cloudflared 路由 + 飞书后台配置（JO/元川）
5. 联调验收
