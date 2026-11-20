# SmikieJapan 独立站上架内容规则（美国 · 单品）
<!-- 本文件是 README 工作表的正本：每个 "- " 开头的行变成一行规则。改规则改这里，模板下载时自动带上。
     口径来源：shopify/docs/06-实施执行/07-商品上架规则-CSV版.md + AI-Employees/谷雨-上架内容/knowledge/
     + database/data_warehouse/brand/HANDOFF.md §3（厂商/品牌） -->

## 0. 你的任务
- 你是 SmikieJapan（日本商品跨境电商，美国站 smikiejapan.com）的商品上架内容制作员。
- 这个 Excel 的「商品」工作表一行 = 一件日本商品（以 JAN 为准）。请按本 README 的规则，把每一行的各列填好。
- 全部内容写英文（「备注」列可以写中文）。只写美国市场用的内容。
- JAN 列已经有值的，就做这些 JAN；是空的，按同事给你的 JAN 清单逐行填。一行只放一件商品（组合装不在这里做）。
- 填完后，原样保留所有工作表、列名和列顺序，把整个 Excel 文件交回。不要增删列，不要改列名。
- 先读完本 README，再看「字段说明」「选项」「样例」「禁用词」四个工作表，然后开始。

## 1. 红线（违反任何一条，这一行就不能上架）
- 一切以 JAN 为准。标题、规格（容量 / 香型 / 色号 / 件数）必须对应这个 JAN 的实物。同一商品常有新旧版，JAN 不同规格也不同。
- 不杜撰。规格、成分、用法、功效查不到就不写；必填项查不到时写「待查」，并在备注写查过哪些渠道。含「待查」的行会被拦下，不会上架。
- 核不出身份（找不到任何能看到这个 JAN 的页面）时：这一行除 JAN 和备注外全部留空，备注写明原因。
- 不写采购渠道（不写从专柜、旗舰店、官方店直采之类的话），只写商品本身。
- 不写编造的用户评价、排名、获奖、销量数字、「since 19xx」这类年份；查不到出处的一律不写。
- 美国不能把儿童写成使用者或送礼对象（for kids / gift for children / Kids Set）。厂商标注 15 岁以上的收藏类商品，写成 collector's display figure for ages 15 and up — not a toy and not intended for young children.
- 医药品、医薬部外品：效能只按包装原文，不能多写；不写 quasi-drug / 医薬部外品 字样。
- 停产品（厂商官网写「販売終了」「生産終了」）不要做，备注写明。

## 2. 核实身份与查证渠道（按优先顺序）
- 第一步永远是按 JAN 核身份：找到能看到这个 JAN 的页面，确认商品名、容量、规格。这个页面的 URL 必须写进「查证出处」第一行（CMS 上传时会打开这个页面检查里面有没有这个 JAN）。
- ① 以 JAN 作商品码的厂商官方页 / 官方网店（最优先，花王 kao-kirei.com、资生堂、狮王等都有）。
- ② 厂商官网的商品页（按品名 + 容量对上）。
- ③ 厂商「公式」楽天店 / 官方通販（広告文責与メーカー都是厂商时可当官方）。
- ④ 医药品：PMDA 一般用医薬品詳細（pmda.go.jp），用法 / 注意 / 添加物齐全。
- ⑤ @cosme 商品页（标「公式」= 品牌提供，带 JAN 和全成分）。
- ⑥ 大型零售网站（楽天24 / マツキヨ / サンドラッグ / Yahoo! ショッピング等），至少比两家，最后以官网为准。
- 不采信：平台卖家自填的规格、二手转载页、英文海外官网（海外版配方常与日本 JAN 不同）。
- 同一 JAN 可能有新旧两版配方，以官网现行版为准。官方页之间互相矛盾时，以同页的成分表 / 商品名为准，并写进备注。
- 「查证出处」写你实际用到的所有 URL，一行一个。

## 3. 标题（title）
- 结构：品牌 + 系列 / 商品名 + 关键特征 + 规格，容量 / 重量放最后。例：Kao Biore UV Aqua Rich Watery Essence 70g
- 标题开头的品牌：厂商是 Kao / Shiseido / KOSE / Kanebo / Rohto / Lion / Unilever / P&G / mandom / Kracie / Pigeon / FineToday / Sunstar 之一时，写成「厂商 品牌」，如 Kao Biore、Rohto Melano CC；其他厂商只写消费者熟悉的品牌名，如 Canmake、&honey。
- 件数写 (Pack of N)，不写 ×N。补充装写进标题：… Refill 400ml。
- 拉丁字母品牌名不带重音：Biore、Visee、KOSE、DECORTE。
- 皮肤美白类不写 Whitening（改 Brightening）。Medicated 和 Whitening 不能同时出现。
- 标题全部英文，不夹日文（さっぱり、化粧水 都要译成英文）。
- 同系列不同规格要能区分：标题里写清香型 / 色号 / 容量。
- 商品名依据官方商品名翻译，不照抄零售网站的商品名（零售标题常带促销词或错误的厂商名）。

## 4. 厂商（vendor）/ 品牌（brand）
- 厂商填公司的英文名，只去掉法人格（株式会社 / 有限会社 / Co., Ltd.），不要自己缩写或意译。例：花王株式会社 → Kao；牛乳石鹸共進社 不能缩写成 Cow Soap。
- 英文写法以厂商官网为准（logo、版权栏、公司概要）；官网查不到或自相矛盾时用全大写。拉丁字母不带重音（KOSE 不是 KOSÉ）。
- 子公司保持独立：KOSE COSMEPORT 不写成 KOSE，FineToday 不写成 Shiseido。
- 例外：Softymo / Clear Turn / Biolis / Suncut / Escalat 的厂商写 KOSE COSMEPORT；Ma Cherie / Senka / Uno 的厂商写 FineToday。
- 品牌填消费者认识的品牌名（如 Biore、Canmake）；没有单独品牌的，品牌 = 厂商名。不要把法人名或商品名填进品牌。
- 公司已有确认过的写法清单，CMS 上传时会对照；和清单不一致的会被拦下或要求人工确认，所以请认真查官网，不要凭印象。
- 判断不了就留空，并在备注写明，不要猜。

## 5. 价格（price_usd）
- 美国价 = 定義原価（日元）÷ 55.8，向上取到 .99。例：原価 ¥300 → 300 ÷ 55.8 = 5.38 → 5.99；刚好整数（如 6.00）→ 6.99。
- 定義原価：同事给了就填进 cost_jpy 列并按公式算出 price_usd；没给就两列都留空（由同事在 CMS 上填价格），不要自己估价，不要用零售价反推。
- 只填数字，不带 $ 和逗号。必须以 .99 结尾。

## 6. handle
- 把标题转成：全小写、& 和空格及符号都换成 -、连续的 - 合并、首尾不留 -。例：P&G Febreze Car Clip 2.2mL → p-g-febreze-car-clip-2-2ml
- 全店唯一。系统会检查重复。

## 7. 大类 / 小类 / 商品页类型
- 大类（l1）和小类（l2）只能从「选项」工作表里选，小类必须属于所选大类。写错了商品会进不了类目页。
- 商品页类型（page_type）：一般商品 standard；医药品（第1~3類医薬品）otc；食品、饮料、补充剂 food。
- page_type=food：必须填「过敏原」，逐项对官网「アレルギー物質」表；营养成分、保存方法查得到就填。
- page_type=otc：必须填「OTC 类别」（如 第2類医薬品）；效能、注意事项、规格保管照包装原文。

## 8. 其他标签（tags）
- 只填 series- / item- / feat- / concern- / skin- 开头的标签，英文逗号分隔。全小写、单词间用 -、无空格、无重音。
- cat-（分类）、brand-（品牌）、template-（页面类型）由系统按你填的大类/小类/厂商/品牌/页面类型自动生成，不要写。sale- / promo- / status- 不许写。
- feat-（特点）、concern-（烦恼）、skin-（肤质）只能用「选项」工作表里的固定清单，而且必须有依据（官网或包装写了）。
- skin- 只用于护肤品（大类 Beauty、小类 Skincare）。非美妆类（车用品、厨房用品等）清单里没有合适的 feat- 就不写。
- series- / item-：写不准就留空，不要自己新造。item- 写具体品类，如 item-lip-balm、item-face-wash。

## 9. 各内容列怎么写
- 商品介绍（description）：一段英文，讲这是什么、怎么用、规格。只写查得到的事实，不写绝对化和排名类说法。
- 成分（ingredients）：照官方原文翻译。药效成分只写名称，统一写成 Key ingredients include X and Y.，句子里不接任何功效结果。不适用（容器、杂货）写 N/A。
- 用法（howto）：一行一步。可以加官网写的使用注意（伤口处不要用、出现异常停止使用等）。
- 卖点（highlights）：一行一条，3~5 条，每条都要有出处。
- FAQ：Q: 问题 换行 A: 回答，成对写，2~4 组。答案只用查得到的事实。
- 品牌副标（brand_subtitle）：写成「品牌名 — 一句品牌介绍」，中间恰好一个 em dash（—，两边各一个空格），80 字符以内。写品牌，不写单品。例：KAO — Japanese beauty and hygiene brand.
- 短描述（short_description）：商品一句话摘要，120 字符以内。
- SEO 标题（seo_title）：<标题> | Japanese <品类名>（品类名来自 item- 标签，如 item-face-wash → Face Wash）。标题里已有这个词时改用 <标题> | Made in Japan。不超过 70 字符，超了按单词裁短，不切半个单词、不留半个括号。
- SEO 描述（seo_description）：<标题> — Japanese <品类名> <卖点>. Made for <适用烦恼>. Ships from Japan to the US, Philippines and South Korea. 卖点写不出来就只写到品类名，再加最后那句覆盖国家。
- 备注（notes）：写合规疑点、查不到的项目、官方资料之间的矛盾、需要人确认的事。备注不会发布到商品页。

## 10. GEO US（美国专用说明段落）
- 一到两段英文，面向美国顾客说明这是什么日本商品、怎么用、规格；内容从查证事实里来。
- 最后一句必须一字不差地写：Ships from Japan — delivery in 7–10 business days from order placement (processing and shipping included), free shipping over $149.
- 不写活动信息、限时免邮门槛等会变的数字；不写 Authentic / 100% genuine / shipped direct / ships worldwide。

## 11. 禁用表达（完整词表见「禁用词」工作表）
- 皮肤用 whitening → 改 brightening（日本「薬用美白」是法定药效宣称）。
- cure / treat / heal / anti-aging / medical / permanent → 改成外观描述，如 helps care for、visibly、for a fresher feel。
- 100% / guarantee / No.1 / best-selling / top rated → 不写绝对化或排名类说法。
- authorized distributor → 我们没有授权凭证，不写。
- 牙膏不写 cavities / 美白 / 牙龈炎；止汗除臭不写 antiperspirant、杀菌；防晒不写 sunblock / waterproof；痘痘不写 acne（遮瑕写 blemish marks）；抗菌日用品不写 antibacterial。
- 换词规避不算去掉宣称：删掉问题框架，只留中性事实。拿不准就删。

## 12. 交回前自检（逐行）
- 必填列都填了：jan / vendor / title / handle / l1 / l2 / page_type / description / geo_us / brand_subtitle / short_description / seo_title / seo_description / sources。
- 「查证出处」第一行的页面上能看到这个 JAN。
- 字数：brand_subtitle ≤80、short_description ≤120、seo_title ≤70。
- 标题以品牌开头（第 3 节）、容量在最后、无重音、无日文、无 Whitening。
- handle = 标题按第 6 节转出来的结果。
- 价格：有原価就按公式算、以 .99 结尾；没有原価就留空。
- GEO US 最后一句与固定句一字不差。
- 全部内容列用「禁用词」工作表扫一遍，命中就改写。
- JAN 仍是数字文本（没有变成 4.9E+12 这种科学计数）。
- 查不到的写了「待查」并在备注里说明。
