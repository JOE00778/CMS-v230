# 24 · ECMS 发货对接规格（Coupang KR → ECMS，Shopify 后续）

> 2026-08-29 分析。Boss 指示：CMS 上做两个 tab（Coupang / Shopify），点一下自动拉取
> 发货订单 → 整理成 ECMS 需要的形式 → 运营核对 → 点按钮发 ECMS → 返回可下载面单。
> **先做 Coupang，Shopify 后做。**
>
> 本文只回答一件事：**发一单到 ECMS 到底需要哪些信息、每一项从哪来、格式什么样。**
> 客户端与页面骨架已在 `shared/ecms_client.py` + `pages/41_📮_ECMS发货.py`（commit b56f318）。

## 1. 两条路：Excel 上传 vs API

运营现在的做法是**填 Excel 上传 ECMS**（`~/Downloads/coupang通关文件.xlsx` 的
「上传用文件 ecms」sheet，57 列 A–BE）。Boss 要的「点一下就发出去、马上拿面单」只能走 **API**
——Excel 上传是异步的，拿不到即时面单。

所以本文用 Excel 模板**反推 ECMS 要什么字段**，实际发送走 `POST /api/manifest`。
两者字段并非一一对应，差异见 §4。

## 2. 数据源现状

| 源 | 位置 | 有什么 | 能不能直接用 |
|---|---|---|---|
| Coupang 订单 | `GET /v4/vendors/{id}/ordersheets` | receiver / overseaShippingInfoDto / orderItems | ✅ API 能拿全 |
| Coupang 订单（PG） | `coupang.order_sheet` | 订单数与金额 | ❌ **PII 全部丢弃**（建表时就不存 orderer/receiver），做不了建单 |
| 商品主档（Coupang） | xlsx「coupang 产品信息0818」 | JAN / 英文名 / HScode / 产品重量 / URL | ⚠️ 现在是**手工 Excel**，没进库 |
| 商品主档（NST） | xlsx「cms0811」= `nst.item_master_raw` | JAN / 厂商 / **毛重(g)** / 原价 | ✅ PG 里有 |
| 收件人地址拆分 | xlsx「地址分析 ecms」 | 韩语地址 → 省 / 市 / 详细地址 | ⚠️ 现在是**人工 + AI 翻译**，无规则化实现 |

**第一个结论**：`coupang.order_sheet` 用不了。它 2026-07-29 建表时就明确「PII（orderer /
receiver / parcelPrintMessage）正规化和 raw_payload 都不保存」，因为它是为**订单数统计**建的。
建单要的收件人姓名/电话/地址/邮编/PCCC 一个都没有。

**建议**：建单时**实时调 Coupang API**取当次要发的订单，PII 只在内存里走一趟，
落库只留 `order_id ↔ trackingNo ↔ 状态`。这样既能建单，又不破坏现有「不存 PII」的设计。
（需 Boss 确认，见 §6-①）

## 3. ECMS 上传模板 57 列 · 逐列来源

标记：**M**=必填 · O=可选 · C=条件必填（照模板原文）
「运营确认」= 模板首行标了「确认」的 6 列，是运营现在每单人工核对的地方。

### 3.1 运单头（A–P）

| 列 | 字段 | M/O | 来源 | 备注 |
|---|---|---|---|---|
| A | Client Code 客户代码 | M | **固定值**（ECMS 分配） | = API 的 `clientId` |
| B | Order Number 订单号 | M | Coupang `orderId` | = API `shipment.referenceCode`；建单幂等键 |
| C | Ref Number 头程运单号 | M | 我方生成 | = API `box.referenceNumber` |
| D | Expected Ship Date 预计发货日 | O | 当天/次日 | |
| E | Arrival Date 预计到达日 | O | — | 一般留空 |
| F | Weight 毛重 | 推荐 | 商品毛重合计 + 包材 | = API `box.weight.value`，**KG** |
| G/H/I | Length/Width/Height 长宽高 | 推荐 | 箱型 | = API `box.dimension`，**CM** |
| J | Weight Unit | 推荐 | 固定 `KG` | |
| K | Length Unit 体积单位 | 推荐 | 固定 `CM` | |
| L | Warehouse Code 仓库编码 | M | **固定值**（ECMS 分配） | = API `originWarehouseCode` |
| M | Customs Clearance Type 清关模式 | O | 按 $150 判 | 见 §5.3；**API 无对应字段** |
| N | Insurance Type 保险类型 | O | — | ESE Care ¥330/件，默认不投 |
| O | Service Level 服务等级 | O | — | 对应 API `productCode`（默认 EX000） |
| P | Order Date 订单日期 | O | Coupang `orderedAt` | |

### 3.2 收件人（Q–AB）

| 列 | 字段 | M/O | 来源 | 备注 |
|---|---|---|---|---|
| Q | Consignee Info Language 语种 | M | 固定 | 韩国线用 KR/EN，待确认 |
| R | Consignee Name 姓名 | M | `receiver.name` | |
| S | Consignee Telephone 电话 | M | **`overseaShippingInfoDto.ordererPhoneNumber`**（Boss 2026-08-30 确认用客户电话） | **不能用 `receiver.safeNumber`**——那是 0503/0502 安心号，清关用不了。见 §5.1 |
| T | Consignee Email 邮箱 | O | `orderer.email`（常空） | |
| U | Consignee Country 国家 | M | 固定 `KR` | |
| V | Consignee Province 州/省 | M | **地址拆分** | 🔎 运营确认 |
| W | Consignee City 城市 | M | **地址拆分** | 🔎 运营确认 |
| X | Consignee Postal Code 邮编 | O | `receiver.postCode` | 韩国 5 位，**保留前导零**（`01058`）→ 必须当文本 |
| Y | Consignee Address 地址 | M | **地址拆分**（剩余部分） | 🔎 运营确认 |
| Z | Consignee ID Type 证件类型 | C | 固定（PCCC 类型） | ⚠️ API 里无直接字段，见 §6-③ |
| AA | Consignee IDNo 证件号 | C | **`overseaShippingInfoDto.personalCustomsClearanceCode`** | 🔎 运营确认 · **韩国清关命门**，见 §5.2 |
| AB | Order Remark 订单备注 | O | — | `parcelPrintMessage`（配送メモ）不建议带 |

### 3.3 内件（AC–AS）

| 列 | 字段 | M/O | 来源 | 备注 |
|---|---|---|---|---|
| AC | Item Info Language 内件语言 | M | 固定 `EN` | |
| AD | Item Num 内件序号 | O | 1,2,3… | = API `item.sequenceNumber` |
| AE | Item_Brand 品牌 | M | NST 主档「厂商」 | |
| AF | Item_Specifications 规格型号 | M | 商品主档「등록 옵션명」 | 🔎 运营确认 |
| AG | Item_SKU | M | JAN | Coupang `externalVendorSkuCode` 形如 `4573626220481_2`（JAN_件数），**要拆** |
| AH | Item_Name 品名 | M | 商品主档「英文名称」 | 申报品名必须英文 |
| AI | Item_Describe 描述 | O | 同上 | |
| AJ | Item_Grossweight 毛重 | M | NST「毛重(g)」→ kg | **单件**毛重 |
| AK | Item_Weight_Unit | M | 固定 `KG` | |
| AL | Item_Dangerous_Type 危险品类型 | M | 合规判定 | = API `hazmatIndicator`；ECMS 禁运矩阵见 wiki |
| AM | HSCode 海关编码 | O | 商品主档「HScode」 | 6 位（如 `330499`） |
| AN | Item_Url 内件网址 | 推荐 | Coupang 商品页 URL | 🔎 运营确认 · `https://www.coupang.com/vp/products/{productId}?vendorItemId={optionId}` |
| AO | Item_Quantity 内件总数 | M | `shippingCount` × SKU 内含件数 | 见 §5.4 |
| AP | Item_Price 单价 | M | 申报单价 | 见 §5.3 币种与精度 |
| AQ | Total_Price 总价 | O | 单价 × 数量 | |
| AR | Item_Currency 币种 | M | 见 §5.3 | |
| AS | Item_Country 原产国 | M | 固定 `JP` | |

### 3.4 其余（AT–BE）

| 列 | 字段 | M/O | 说明 |
|---|---|---|---|
| AT | Shipper_Code 发货人编码 | M | **固定值**（ECMS 分配）。API 用 `shipper` 对象代替 |
| AU | Platform Id 电商平台编码 | O | Coupang 的平台码，需 ECMS 给 |
| AV | Sub Client Code 被代理商家编码 | O | 不适用 |
| AW | HazmatDesc 危险品描述 | O | |
| AX | TAX_NUMBER 目的国进口税号 | O | = API `item.additionalInfo.taxNumber` |
| AY | TAX_PAID_TYPE 消费税支付状态 | O | = API `item.additionalInfo.taxPaidType` |
| AZ | Shipper's ABN Number | O | 澳洲专用，不适用 |
| BA/BB | Export Type / Reference 出口报关 | O | = API `customs.exportType` / `exportReference` |
| BC | ESE Care | O | ECMS 保险 ¥330/件 |
| BD | A/F 运杂费 | O | = API `customs.declarationValues` type=Freight |
| BE | Other Charges 保险金额 | O | = API `customs.declarationValues` type=Insurance |

## 4. Excel 列 → API 字段：三类差异

1. **Excel 有、API 有** —— 直接映射（上表大部分）。
2. **Excel 有、API 无明确字段**：`Customs Clearance Type`（清关模式）、`Consignee ID Type/IDNo`
   （PCCC）、`Platform Id`、`Shipper_Code`、`Insurance Type`。→ §6 待确认清单。
3. **API 有、Excel 无**：`serviceType`（Warehouse/Dropoff/Pickup）、`reasonForExport`、
   `incoterm`、`dutyBilling.paidBy`、`immediateLabel`。这几个 Excel 靠合同预设，API 必须显式传。

## 5. 格式与精度规则

### 5.1 电话：安心号不能用
Coupang 给两个号：`receiver.safeNumber`（0503-/0502- 开头的안심번호，转接号）和
`overseaShippingInfoDto.ordererPhoneNumber`（真实手机 010-）。
xlsx 源文件里 `Recipient phone number` 列**是空的**，而 `Contact information of buyer for
customs clearance purpose` 列有真实号——这就是运营实际在用的那个。
**ECMS 收件人电话取 `ordererPhoneNumber`，不取 safeNumber。**

### 5.2 PCCC：韩国清关命门
韩国个人直购必须有 개인통관고유부호（P + 12 位，如 `P842160107476`），
来自 `overseaShippingInfoDto.personalCustomsClearanceCode`；另有 `oneTimePccc`（一次性 PCCC）
和 `otpNumber` 两种变体。**三者取哪个、API 走哪个字段传，规格书 v1.7 没写**（IOR 对象标着
"Reserved"）→ §6-③ 必须问 ECMS。

### 5.3 金额、币种与 $150 线
- 规格书 v1.7 的精度定义（**这是权威来源**）：
  | 字段 | 类型 | 含义 |
  |---|---|---|
  | `Weight.value` | `Double(8,2)` | 最多 8 位、**2 位小数**（≤ 999999.99） |
  | `Dimension.length/width/height` | `Double(8,2)` | 同上 |
  | `Price.amount` | `Double(8,2)` | 同上 |
  | `DeclarationValues.amount` | `Double(8,2)` | 同上 |
  | `Item.quantity` / `numberOfPieces` | `Int(5)` | 整数 |
- **币种建议用 USD 不用 KRW**：① `Double(8,2)` 上限 999,999.99，KRW 单价动辄数万，
  多件订单容易顶到上限；② 韩国个人通关免税线是 **USD 150**（xlsx「JD 用发货文件」填写说明行
  D 列原文：`$150以下 =1 / 超过$150=2`），判目录通关还是一般申报要按美元算。
  → KRW→USD 换算走 CMS 现有汇率（`shared/forex.py` / page36 三金汇率）。
- ⚠️ **Excel 模板本身没有写小数位要求**：57 列表头单元格的 numFmt 大多是 `General`，
  个别是错乱的日期格式（F 列毛重竟是 `yyyy/mm/dd h:mm`），不构成规则。
  **精度一律按 API 规格 `Double(8,2)` 落**；Excel 侧是否另有要求 → §6-④ 问 ECMS。

### 5.4 数量：SKU 里带件数
Coupang 的 `externalVendorSkuCode` 是 `JAN_件数` 格式（`4573626220481_2` = 该 JAN 的 2 件装）。
申报数量 = `shippingCount` × 下划线后的件数，不是直接用 `shippingCount`。
毛重同理要乘。**这条弄错会导致申报数量与实物不符，属于 ECMS 事件码 S04A401（内件不一致）的成因。**

### 5.5 邮编前导零
韩国邮编 5 位，存在 `01058` 这种前导零（xlsx 里该列格式设成了文本 `@`）。
全流程必须当字符串处理，一旦被当数字会掉零、地址匹配失败。

### 5.6 地址拆分 —— 已实证并落地（`shared/kr_address.py`）

ECMS 要 省(V) / 市(W) / 详细地址(Y) 三段，Coupang 只给整串韩语 `addr1`+`addr2`。

**不用 AI 翻译**。xlsx「地址分析 ecms」里 AI 译文出过明显错误：「경기도 수원시」→
「京畿道京畿道水原市」、「부산광역시」→「釜山广域市釜山广域市」，还有一条把
「（注：因原文韩语地名…）」这种解释文本写进了地址栏。

**用封闭集合前缀匹配**。韩国行政区是固定集合——一级 17 个、二级 264 个，
数据来自 행정안전부 법정동코드 전체자료（code.go.kr，2026-03-01 판），
抽成 `shared/kr_admin_divisions.json`（4KB，全表 50,100 行不进仓库）。

切分口径**照搬运营现行做法**（xlsx「地址分析」B/C/D 列）：

| 地址形态 | 省 | 市 | 详细 |
|---|---|---|---|
| 도 아래 시+구（경기도 성남시 분당구…） | 경기도 | 경기도 성남시 | 분당구 … |
| 광역시 아래 구（부산광역시 서구…） | 부산광역시 | 부산광역시 | 서구 … |
| 도 아래 시만（충청남도 공주시…） | 충청남도 | 충청남도 공주시 | 관골2길 … |
| 세종특별자치시（单层制，无 시군구） | 세종특별자치시 | 세종특별자치시 | 고운동 … |

**实测**（用 xlsx 里的 311 条真实订单地址 + 运营人工拆好的 16 条）：

| 指标 | 结果 |
|---|---|
| 三段全部切出 | **311 / 311** |
| 与运营人工结果完全一致 | **15 / 16** |
| 唯一差异 | 运营保留简称「대구」，本模块正规化成「대구광역시」；详细地址一致 |

调参过程如实记录：初版 325/327，两处失败都是**结构性**问题不是样本特例——
① 세종특별자치시 的法定洞代码是 `3611000000`（长得像 시군구），按代码位数判层级会把
整个市漏掉，改成按名称空格数判；② 韩国人日常写简称（「대구 달서구」），补别名表。
即便如此，**新写法出现时仍可能漏**，所以切不出来一律返回 `ok=False` 让页面标红，
绝不猜着填。

**备选方案（暂不采用，留档）**：`juso.go.kr` 검색API（행정안전부，免费、自动审批승인키）
输入地址串返回 `siNm`/`sggNm`/`emdNm`/`roadAddr`/`zipNo`/**`engAddr`（英文地址）**。
开源库 `addresskr`（Apache-2.0）的做法值得记：因为不知道详细地址从哪开始，
它**从后往前逐 token 剥离，每次拿前半部分去 API 搜，第一个搜得到的就是标准地址**。
准确度更高且顺带拿英文地址与邮编校验，代价是每单多次外部 API 调用 + 要申请승인키。
→ **英文地址不需要**（Boss 2026-08-30 确认），所以这条路不上。韩文原文直接填，
`Consignee Info Language` 按韩文走。

## 6. 待确认清单（做之前必须问清）

| # | 问谁 | 问什么 | 不问清的后果 |
|---|---|---|---|
| ~~①~~ | ~~Boss~~ | **已定 2026-08-30**：每天定点拉到 CMS 临时表（含 PII），发完可删，**保留 7 天**。见 §7.1 | — |
| ② | **ECMS 牧野さん** | clientId / token（UAT+PRO）、Warehouse Code、Shipper Code、Platform Id | 全是 M 字段，缺一个发不出去 |
| ③ | **ECMS 牧野さん** | **韩国 PCCC 通过 API 哪个字段传**（IOR.idNumber？additionalInfo？）；`oneTimePccc` 怎么处理 | 韩国清关过不了。问询文本见 `docs/24-ecms-inquiry-makino.ja.md`（Boss 2026-08-30 指示由 Boss 自己发） |
| ④ | **ECMS 牧野さん** | 重量/尺寸/金额的**上传侧**精度要求是否与 API 规格一致（都是 2 位小数） | 数值被拒或被截断 |
| ~~⑤~~ | ~~Boss~~ | **已做 2026-08-30**：`shared/kr_address.py`，实测 311/311。见 §5.6 | — |
| ⑥ | **ECMS 牧野さん** | serviceType（Warehouse/Dropoff/Pickup）、reasonForExport 的合同取值 | 已在 b56f318 里默认 Warehouse/commercial，未确认 |

## 7. 实现方案（Coupang tab）

### 7.1 每日拉取与留存（Boss 2026-08-30 拍板）

> 「每天定点拉取 coupang 数据，临时放到 CMS，发送完后可以删掉，数据大概留一个星期就 OK。」

新表 `coupang_shipment_queue`（**含 PII，7 天后自动清**）：

| 列 | 来源 |
|---|---|
| `order_id` / `shipment_box_id` / `ordered_at` / `status` | ordersheets |
| `receiver_name` / `receiver_phone` | `receiver.name` / `overseaShippingInfoDto.ordererPhoneNumber` |
| `post_code` / `addr_raw` | `receiver.postCode` / `addr1`+`addr2`（**字符串，保前导零**） |
| `province` / `city` / `address_detail` / `addr_ok` | `shared/kr_address.to_ecms()` |
| `pccc` | `overseaShippingInfoDto.personalCustomsClearanceCode` |
| `items_json` | orderItems（JAN・件数・数量・単価） |
| `ecms_status` / `tracking_no` | pending / sent / failed |
| `pulled_at` / `expires_at` | 拉取时刻 / +7 天 |

- 排程挂元川既有 dispatcher（与 `coupang_orders_daily` 07:20 同链路），拉 `status=INSTRUCT`（待发货）
- **清理**：每次拉取时顺手 `DELETE WHERE expires_at < now()`，不另起 cron
- 既有 `coupang.order_sheet`（统计用、不存 PII）**不动**，两张表各管各的



```
[拉取] 调 ordersheets（status=INSTRUCT 待发货）→ 按 shipmentBoxId 一箱一单
   ↓  PII 只进 st.session_state，不落库
[组装] receiver + PCCC + 地址拆分 + 商品主档（JAN → 英文名/HS/毛重/URL）+ 金额换算 USD
   ↓
[核对] data_editor 显示，红标缺字段；运营重点核对模板标「确认」的 6 列：
       省 / 市 / 地址 / PCCC / 规格型号 / 商品URL
   ↓
[发送] 逐单 POST /api/manifest（非幂等，发前查 ecms_shipment 拦重复）
   ↓
[面单] immediateLabel=true 直接拿 labelUrl；或事后 POST /api/printLabel
       多单合并成一个 PDF 供下载
   ↓
[回写] order_id ↔ trackingNo ↔ status 落 ecms_shipment（**不落 PII**）
       ⚠️ Coupang 侧还要回填运单号（invoice 上传接口），否则平台不认发货 —— 待办
```

商品主档缺口：「coupang 产品信息0818」（JAN → 英文名 / HScode / 产品重量 / URL）现在只有 Excel。
要么导进 PG 做成表，要么每次让运营上传。**建议进 PG**，否则这个 tab 每次都要人工喂数据。

## 8. 明确不在本次范围
- Shopify tab（Boss 指示：Coupang 做完再做）
- JD（京东物流）出库单上传 —— xlsx 里的「JD 用发货文件」「JD CSV UP用」是另一条链路，不是 ECMS
- Excel 批量上传路径（我们走 API）
- Coupang 侧运单号回填（要单独接 Coupang 的发货处理接口）


---

## 9. 実装（2026-08-30 · Coupang 側）

Boss の指示 4 点を反映済み：①毎日定時に取り込み・7 日で削除 ②電話は客の実番号
③PCCC は注文から取る ④申告は USD 固定（レートは運営 Excel の係数）。

| 追加したもの | 何をする |
|---|---|
| `shared/coupang_client.py` | ordersheets を status=ACCEPT/INSTRUCT で引く（PII 込み・その場限り） |
| `shared/coupang_ecms.py` | 換算と分割の**純関数**。運営 Excel の数式が正 |
| `shared/coupang_store.py` | queue の読み書き + **7 日で PII 削除** |
| `coupang_shipment_queue` / `coupang_product_info` | 2 表（SQLite / PG 両方に追加済み） |
| `pages/41_📮_ECMS发货.py` の「🇰🇷 Coupang」tab | 取込 → 核対 → 送信 → 面単 |
| `tools/pull_coupang_shipments.py` | 元川の定時タスク用。取り込むだけで**送らない** |

### 換算・丸めの規則（`coupang通关文件.xlsx`「JD 用发货文件」の数式が出所）

| 項目 | 式 | 精度 |
|---|---|---|
| 申告金額 | `paid amount(KRW) × 0.00068` | **USD・小数 2 桁**（`ROUND`） |
| 通関類型 | `金額 >= 150 → "2"、未満 → "1"` | USD 150 が免税枠 |
| 重量 | `単品重量(g) ÷ 1000 × 数量` | **kg・小数 1 桁を切り上げ**（`ROUNDUP`） |
| 数量 | `SKU の "_" の後 × Purchased qty` | 整数 |
| 発送人 | `smikie japan` / `3-1-35,Sekidenmachi,Oita,Oita,Japan` / `097-574-9906` | 固定 |

⚠️ `0.00068` は実勢レートではなく運営の固定係数（1 USD ≈ 1,470.6 KRW）。
`COUPANG_KRW_USD_RATE` で差し替え可。**使ったレートは行ごとに保存**している。

### 住所の分割

`shared/kr_address.py`（既存 · 행정안전부 법정동코드ベース、一級 17 + 二級 264、
実測 311/311）の `to_ecms()` をそのまま呼ぶ。**coupang_ecms 側では再実装しない。**

### 運用フロー

```
毎日定時（元川タスク）  tools/pull_coupang_shipments.py --days 3
    → 取り込み・7 日超を削除・**送信はしない**
運営が page41「🇰🇷 Coupang」  赤い行を直す → チェック → 「发送到 ECMS」
    → 1 件ずつ manifest → tracking と面単 URL が返る → queue は sent
```

`reference_code` は `CP-{orderId}-{shipmentBoxId}`。送信前に `ecms_shipment` を見て
二重建単を止める（ECMS の manifest は非冪等）。

### 元川の定時タスク（Boss が設定）

```
タスク名: CMS Coupang 発送取込
実行時刻: 毎日 09:00（時刻は変えて構わない）
コマンド: docker exec cms_streamlit python tools/pull_coupang_shipments.py --days 3
```

**COUPANG_* の凭据は元川に既にある**（`database/.env`。斑马ERP と共用の WING key、
180 日輪換）。streamlit 容器は env_file を読まず environment を明示列挙する作りなので、
`deploy/windows/.env` にも同じ 3 つを写す必要がある（NST_* / BANMA_* と同じ扱い）:

```
COUPANG_ACCESS_KEY=（database/.env と同値）
COUPANG_SECRET_KEY=（同上）
COUPANG_VENDOR_ID=（同上）
```

compose の environment を触ったので、反映は **redeploy.bat**（update-cms.bat の
restart では compose を読み直さない）。

### 疎通確認（凭据が届いたら最初にやること）

Boss 2026-08-30「先测试 ECMS 推送订单信息和回传面单是否可用，然后再测试回填 coupang」。

```
docker exec cms_streamlit python tools/ecms_uat_check.py
```

ダミーの韓国宛て 1 件で manifest → printLabel → getTracking → cancelShipment を
順に叩き、`ok=4/4` で終われば ECMS 側は使える。面単 PDF はファイルに落とすので
中身も目で見られる。落ちたらそこで止まり、送った JSON と生レスポンスを出す。

- **ECMS_ENV=pro では動かない**（実運送状が立つため）。`--allow-pro` を明示したときだけ
- 建てた運単は最後に自動で取消す（`--keep` で残せる）

これが通ってから、page41「🇰🇷 Coupang」で実注文 1 件を UAT に流して突き合わせる。
Coupang への運送状番号の戻し入れはその後（Boss 指示で後回し）。

### まだ塞がっていない穴

- **PCCC の送り先フィールド**（牧野さん回答待ち）。いまは `customs.importReference` に
  仮置きしている。回答が来たら `build_shipment()` に正式に渡す — `pages/41` の
  `TODO(PCCC)` の 1 行を差し替えるだけ
- 商品マスタは画面から Excel 取り込み。自動化は未着手
- 箱サイズは 25×18×8cm 固定。実箱に合わせるなら要調整
- **Coupang への運送状番号の戻し入れが未実装** — これをやらないと平台側が発送を認識しない


---

## 10. Excel 変換（2026-09-02 · 実ファイル照合で確定）

Boss 2026-09-02「在 CMS 上做一个可转化的先，等 ECMS 的 API 对接环境准备好后再用 API 对接」。
運営の実物 2 本（`0902新订单.xlsx` → `0902ecms上传-新订单.xlsx`・37 行）を突き合わせ、
**57 列中 53 列が 37/37 一致**するところまで規則を確定させた。

### ⚠️ 通貨は KRW。前の「USD 換算」は ECMS 側では誤り

実ファイルは 37/37 が `Item_Currency = KRW`、単価も韓国ウォンのまま
（`paid amount ÷ 数量` を**整数に四捨五入**）。
`× 0.00068` の USD 換算を使っているのは **JD 向けの別ライン**（`JD 用发货文件` シート）で、
ECMS のアップロードではない。§5.3 の記述はこの節が優先。

- 単価: `34720 ÷ 3 = 11573.33 → 11573` / `20600 ÷ 3 = 6866.67 → **6867**`（切り捨てではない）
- Python 組み込み `round()` は偶数丸めなので使わない → `round_half_up()`

### 確定した列（実測 37/37）

| 列 | 値 | 出所 |
|---|---|---|
| A / L / AT | `LBF` / `NRT` / `LBFVO` | 固定（Client / Warehouse / Shipper Code） |
| Q / AC / U / AS / Z / AK / AL / AR | `EN` / `EN` / `KR` / `JP` / `ID` / `KG` / `N` / `KRW` | 固定 |
| B 订单号 | Coupang `Order number` | |
| C 头程运单号 | `ECLBF` + yymmdd + 5 桁連番 | 画面で開始番号を指定（運営は途中で番号を飛ばすことがある） |
| R / S / X / Y / AA | 姓名 / **通関用電話** / 邮编 / 住所原文 / PCCC | Coupang AA / **AK** / AC / AD / AJ |
| V / W | 省 / 市 | 住所から。略称は開き、**改称前の道名はそのまま**。市は시군구の先頭語 |
| AE 品牌 | NST 厂商から和名併記を除去 | `Pelican Soap（ペリカン石鹸）`→`Pelican Soap`。32/37 |
| AF 规格型号 | Coupang `Registered option name` | |
| AG SKU / AO 数量 | `JAN_入数` を分解、`入数 × 購入数` | |
| AH / AM / AN / AU | 英語品名 / HSCode / 商品URL / Platform Id | 商品マスタ（**SKU キー**）。URL は `products/{ProductID}?vendorItemId={OptionID}` |
| **AJ 毛重** | **`round(NST 毛重(g) ÷ 1000, 2)`** | **1 個あたり**。入数も購入数も掛けない（実測 148g→0.15 / 757g→0.76） |
| AP 单价 | `paid amount ÷ 数量` を整数に四捨五入 | KRW |
| D–K / M–P / AB / AD / AI / AV–BE | 空 | 箱の重量寸法すら入れない（運営の実ファイルがそう） |

### 残る 4 列の差（すべて説明がつく）

| 列 | 件数 | 理由 |
|---|---|---|
| C Ref Number | 18/37 | 運営が途中で番号を 1 つ飛ばしている。開始番号は画面で指定 |
| X 邮编 | 7/37 | こちらは `07531` を**文字列**で出す。運営の Excel は数値 `7531` + 表示書式で 0 を足している。前ゼロが落ちない分こちらが安全 |
| V / W | 1/37 | 住所が `전남광주통합특별시`（実在しない行政区）。運営が人手で `광주광역시 북구` に直したもの。**自動では埋めない**——画面で赤く出す |

### 商品マスタは SKU キー

同じ JAN でも規格違いは別 SKU・別 OptionID・別英語品名（実データに
`4970301590196` と `4970301590196_3`、`4973307670633_3` と `_4` がある）。
JAN で持つと英語品名と商品 URL が混ざるので、`coupang_product_info` の主キーは **SKU**。
重量と品牌だけは JAN 単位なので NST マスタから引く。

`brand` 列は上書き用。NST の 厂商 とブランド名が別物のケース（`ユニリーバ`→`Dove`、
`コーセーコスメポート`→`CoenRich`、`UHA味覚糖`→`UHA`、`マルコメ`→`Marukome`）は
自動で導けないので、運営がここに入れる。

### 使い方

page41「🇰🇷 Coupang」→ モード「📄 Excel 转换」→ Coupang の受注 Excel を上げる →
欠けている行が赤く出る → 「下载 ECMS 上传文件」。
API 疎通が済んだらモードを「🔌 API 直连」に切り替える（同じ変換規則を使う）。

テストは `tests/test_coupang_to_ecms_xlsx.py`。fixtures は**匿名化済み**
（氏名・電話・PCCC は連番の偽値、住所は行政区だけ残して以下を差し替え）。
分割規則の検証に必要な部分は保っているので、規則を壊すとテストが落ちる。
