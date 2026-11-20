"""自建站上架（US · 单品）的口径定义 —— 模板生成（workbook）与上传检查（validate）共用这一份。

口径正本：shopify/docs/06-实施执行/07-商品上架规则-CSV版.md（2026-10-07）
         + AI-Employees/谷雨-上架内容/knowledge/（pricing-rules / claims-cleanup / geo-and-content-rules）
这里只放机器要用的常量；给 AI 读的文字版在 rules_us.md。改口径时两处一起改。
"""
from __future__ import annotations

import math
import re
import unicodedata

# ── 价格（谷雨 pricing-rules.md「US 售价公式 2026-09-20」= 0.36 × FX155）──
US_DIVISOR = 55.8


def price_usd(cost) -> float | None:
    """定義原価(JPY) → USD，向上取到 .99（同 shopify/scripts/us_price_from_definition_cost.py）。
    原価为空或 ≤1 → None（规则：价格留空，不用其他成本代替）。"""
    try:
        c = float(cost)
    except (TypeError, ValueError):
        return None
    if not c or c <= 1:
        return None
    return float(math.floor(c / US_DIVISOR + 1e-9) + 0.99)


# ── 大类 / 小类（附录 A）：名称 → 标签 ──
CATEGORIES: dict[str, tuple[str, dict[str, str]]] = {
    "Beauty": ("cat-beauty", {
        "Skincare": "cat-beauty-skincare", "Makeup": "cat-beauty-makeup", "Hair Care": "cat-beauty-hair-care",
        "Personal Care": "cat-beauty-personal-care", "Sun Care": "cat-beauty-sun-care",
        "Tools": "cat-beauty-tools", "Fragrance": "cat-beauty-fragrance"}),
    "Health": ("cat-health", {
        "Dental": "cat-health-dental", "Eye Care": "cat-health-eye-care", "OTC": "cat-health-otc",
        "Vitamins": "cat-health-vitamins", "First Aid": "cat-health-first-aid"}),
    "Food & Drinks": ("cat-food", {
        "Sweets": "cat-food-sweets", "Snacks": "cat-food-snacks", "Instant": "cat-food-instant",
        "Tea & Coffee": "cat-food-tea", "Matcha": "cat-food-matcha", "Pantry": "cat-food-pantry"}),
    "Lifestyle": ("cat-lifestyle", {
        "Kitchen & Dining": "cat-lifestyle-kitchen", "Cleaning": "cat-lifestyle-cleaning",
        "Car Care": "cat-lifestyle-car-care", "Candles": "cat-lifestyle-candles", "Bath & Body": "cat-lifestyle-bath",
        "Home Decor": "cat-lifestyle-home-decor", "Travel": "cat-lifestyle-travel",
        "Emergency": "cat-lifestyle-emergency"}),
    "Kawaii & Anime": ("cat-kawaii", {
        "Anime Goods": "cat-kawaii-anime-goods", "Figures": "cat-kawaii-figures", "Toys": "cat-kawaii-toys",
        "Plushies": "cat-kawaii-plushies", "Manga": "cat-kawaii-manga", "Trading Cards": "cat-kawaii-trading-cards"}),
    "Stationery": ("cat-stationery", {
        "Pens & Pencils": "cat-stationery-pens", "Office": "cat-stationery-office", "Bags & Cases": "cat-stationery-bags",
        "Notebooks": "cat-stationery-notebooks", "Washi Tape": "cat-stationery-washi-tape"}),
    "Baby & Family": ("cat-baby", {
        "Care": "cat-baby-care", "Nursing": "cat-baby-nursing", "Skincare": "cat-baby-skincare",
        "Food": "cat-baby-food", "Toys": "cat-baby-toys", "Maternity": "cat-baby-maternity"}),
    "Sports & Outdoor": ("cat-sports", {"Equipment": "cat-sports-equipment"}),
}

# ── 附录 B 固定清单 ──
FEAT = set("""feat-fragrance-free feat-colorant-free feat-mineral-oil-free feat-paraben-free feat-alcohol-free
feat-additive-free feat-allergy-tested feat-uv-absorber-free feat-medicated feat-non-comedogenic feat-patch-tested
feat-no-double-cleanse feat-value-for-money feat-organic-natural feat-aging-care feat-pearl-shimmer feat-glow-finish
feat-uv-protection feat-natural-finish feat-dimensional-makeup feat-long-wear-fit feat-matte-finish feat-waterproof
feat-high-pigment feat-long-lasting-color feat-easy-cleansing feat-high-coverage feat-pore-coverage feat-long-wear
feat-smooth-finish feat-refreshing feat-pleasant-scent feat-high-spf feat-no-white-cast feat-treatment-effect
feat-sheer-radiant feat-color-care feat-moisturizing feat-straight-hair feat-wave-hair feat-volume-down
feat-volume-up-hair feat-hair-growth-care feat-scalp-care feat-non-greasy feat-slimming feat-volume-up
feat-lengthening feat-hot-water-off feat-clump-free feat-curl-hold feat-separating feat-smudge-proof
feat-easy-application feat-hold-keep feat-deodorant feat-high-adhesion feat-hydrating feat-hair-care
feat-brightening-body feat-exfoliating-body""".split())
CONCERN = set("""concern-acne concern-sensitive concern-brightening concern-firmness concern-oiliness concern-pores
concern-tightening concern-exfoliating concern-dryness concern-rough-skin concern-lip-dryness concern-dark-circles
concern-sebum concern-dark-spots concern-dullness concern-dandruff-itch concern-wrinkles concern-sagging""".split())
SKIN = set("skin-sensitive skin-combination skin-dry skin-normal skin-oily skin-atopic".split())
# AI 可以自己打的标签前缀；cat-/brand-/template- 由系统生成，sale-/promo-/status- 禁止
FREE_PREFIXES = ("series-", "item-", "feat-", "concern-", "skin-")
SYSTEM_PREFIXES = ("cat-", "brand-", "template-", "sale-", "promo-", "status-")

TEMPLATES = {"standard": "smikie-standard", "otc": "smikie-otc", "food": "smikie-food"}

GEO_US_END = ("Ships from Japan — delivery in 7–10 business days from order placement "
              "(processing and shipping included), free shipping over $149.")

PENDING = "待查"

# ── 禁用表达（07 规则第 8 章 + claims-cleanup + GMC 口径）──
# error = 规则明令不能写；warn = 常有误伤（品名/色号），要人看一眼
BANNED_ERROR = [
    (r"\bwhiten(ing)?\b", "Whitening 不能写（皮肤用改 brightening）"),
    (r"\bcures?\b", "cure 是医疗宣称"),
    (r"\bheal(s|ing|ed)?\b", "heal 是医疗宣称"),
    (r"\banti[- ]?aging\b", "anti-aging 是功效宣称"),
    (r"\bmedical\b", "medical 是医疗宣称"),
    (r"\bpermanent(ly)?\b", "permanent 是绝对化表述"),
    (r"100\s?%", "100% 是绝对化表述（GMC）"),
    (r"\bguarantee[sd]?\b", "guarantee 是绝对化表述"),
    (r"\bbest[- ]?sell(ing|er)\b", "best-selling 是排名类说法"),
    (r"\btop[- ]rated\b", "top rated 是排名类说法"),
    (r"\bauthorized distributor\b", "我们没有授权凭证"),
    (r"\bships? worldwide\b", "覆盖国家写错（GMC）"),
    (r"\bquasi[- ]drug\b", "不写医薬部外品字样"),
    (r"\bsunblock\b", "sunblock 属 FDA misbranded 用语"),
    (r"\bantiperspirant\b", "antiperspirant 是 US OTC 类别名"),
    (r"\b(for (kids|children)|gift for (kids|children))\b", "美国不能把儿童写成使用者/送礼对象（CPSC）"),
    (r"\bdirect from (the )?(official|flagship|brand) store\b", "不写采购渠道"),
]
BANNED_WARN = [
    (r"\btreat(s|ed|ing)?\b", "treat 可能是医疗宣称（品名 Treatment 不算）"),
    (r"\bno\.?\s?1\b", "No.1 可能是排名说法（色号不算）"),
    (r"\bacne\b", "acne 是 FDA 适应症用语（遮瑕写 blemish marks）"),
    (r"\bantibacterial\b", "antibacterial 是杀菌宣称"),
    (r"\bcavit(y|ies)\b", "牙膏不写 cavities"),
    (r"\beczema\b", "eczema 会被药品正则拦"),
    (r"\bwaterproof\b", "防晒品不写 waterproof"),
]

# 07 规则 §1-4：上架前要过合规的品类（按 L1/L2 名或模板判）
COMPLIANCE_L1 = {"Beauty", "Health", "Food & Drinks", "Baby & Family"}

JAN_RE = re.compile(r"^(\d{8}|\d{13})$")
HANDLE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KANA_KANJI = re.compile(r"[ぁ-ゖァ-ヺ一-鿿]")


def deacc(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "")) if not unicodedata.combining(c))


def slug(s: str) -> str:
    """同 shopify/scripts/align_brand_to_pg.py slug()：& → 空格、去重音、非字母数字 → '-'。"""
    s = re.sub(r"[&]+", " ", deacc(s).lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# 前台品牌展示名（同 align_brand_to_pg.py display_name()，改动两边同步）
_DISPLAY_WHITELIST = {"kao": "Kao", "shiseido": "Shiseido", "kose": "KOSE", "kanebo": "Kanebo", "rohto": "Rohto",
                      "lion": "Lion", "unilever": "Unilever", "pg": "P&G", "mandom": "mandom", "kracie": "Kracie",
                      "pigeon": "Pigeon", "finetoday": "FineToday", "sunstar": "Sunstar"}


def _fold(s: str) -> str:
    return slug(s).replace("-", "")


def display_name(vendor: str, brand: str) -> str:
    if not brand:
        return vendor or ""
    if not vendor:
        return brand
    fb, fv = _fold(brand), _fold(vendor)
    if fb == fv or fb.startswith(fv) or fv.startswith(fb):
        return brand
    for k, show in _DISPLAY_WHITELIST.items():
        if fv.startswith(k):
            return f"{show} {brand}" if _fold(show) not in fb else brand
    return brand


def brand_tags(vendor: str, brand: str) -> list[str]:
    """brand- 标签：厂商 + 品牌两个都打（只加不减 · HANDOFF §3.4）。"""
    out = []
    for x in (vendor, brand):
        t = "brand-" + slug(x) if x and slug(x) else ""
        if t and t not in out:
            out.append(t)
    return out


# ── 「商品」工作表的列 ──
# kind: pre = 系统预填（不许改，上传时核对）/ info = 系统预填仅供参考 / ai = AI 填
# (key, 中文名, kind, 必填, 字数上限, 说明)
COLUMNS: list[tuple[str, str, str, bool, int | None, str]] = [
    ("jan", "JAN", "pre", True, None, "商品身份。一切以 JAN 为准，标题/规格必须对应这个 JAN 的实物"),
    ("nst_name", "NST 商品名（日文）", "info", False, None, "仅供识别，常混错厂商名，不能当事实"),
    ("vendor", "厂商 Vendor", "pre", True, None, "灰色=口径表已定，不要改；白色=口径表还没有，按 README 第 3.5 节查官网填写"),
    ("brand", "品牌", "pre", False, None, "灰色=口径表已定，不要改；白色=按 README 第 3.5 节填写（没有品牌就填厂商名）"),
    ("brand_display", "前台品牌展示名", "info", False, None, "商品页品牌行显示的名字；标题开头用这个"),
    ("price_usd", "美国价 USD", "pre", True, None, "= 定義原価 ÷ 55.8 向上取到 .99，系统算好，不要改"),
    ("stock", "可用库存", "info", False, None, "大分弁天仓（斑马）当前可用数，仅供参考"),
    ("title", "标题", "ai", True, 255, "英文。品牌 + 系列/商品名 + 关键特征 + 规格（容量放最后）"),
    ("handle", "handle", "ai", True, 255, "标题转小写、空格换 -、去符号；全店唯一"),
    ("l1", "大类", "ai", True, None, "从「选项」表选（如 Beauty）"),
    ("l2", "小类", "ai", True, None, "从「选项」表选，必须属于所选大类（如 Skincare）"),
    ("page_type", "商品页类型", "ai", True, None, "standard / otc（医药品）/ food（食品）"),
    ("tags", "其他标签", "ai", False, None, "series- / item- / feat- / concern- / skin-，英文逗号分隔；不要写 cat-/brand-/sale-"),
    ("description", "商品介绍", "ai", True, None, "英文段落：是什么、怎么用、规格；只写查得到的事实"),
    ("ingredients", "成分", "ai", False, None, "照官方原文译英文，一行一个或逗号串；不适用写 N/A"),
    ("howto", "用法", "ai", False, None, "一行一步"),
    ("highlights", "卖点", "ai", False, None, "一行一条，3~5 条"),
    ("faq", "FAQ", "ai", False, None, "Q: … A: … 成对，换行分隔"),
    ("geo_us", "GEO US", "ai", True, None, "美国专用说明段落，最后一句必须是固定结尾句（见 README）"),
    ("brand_subtitle", "品牌副标", "ai", True, 80, "「品牌名 — 一句品牌介绍」，恰好一个 em dash（—）"),
    ("short_description", "短描述", "ai", True, 120, "商品一句话摘要"),
    ("seo_title", "SEO 标题", "ai", True, 70, "<标题> | Japanese <品类名>，超 70 字符按单词裁短"),
    ("seo_description", "SEO 描述", "ai", True, 320, "<标题> — Japanese <品类名> <卖点>. Made for <烦恼>. Ships from Japan to the US, Philippines and South Korea."),
    ("food_nutrition", "营养成分（食品）", "ai", False, None, "page_type=food 时填"),
    ("food_allergen", "过敏原（食品）", "ai", False, None, "page_type=food 时必填，逐项对官网アレルギー物質表"),
    ("food_storage", "保存方法（食品）", "ai", False, None, "page_type=food 时填"),
    ("otc_class", "OTC 类别（医药品）", "ai", False, None, "page_type=otc 时必填，如 第2類医薬品"),
    ("otc_efficacy", "效能（医药品）", "ai", False, None, "按包装原文，不能多写"),
    ("otc_precautions", "注意事项（医药品）", "ai", False, None, ""),
    ("otc_specs", "规格·保管（医药品）", "ai", False, None, ""),
    ("sources", "查证出处", "ai", True, None, "用到的官网/资料 URL，一行一个"),
    ("notes", "备注", "ai", False, None, "合规疑点、查不到的项目、需要人确认的事"),
]
COL_KEYS = [c[0] for c in COLUMNS]
PRE_KEYS = [c[0] for c in COLUMNS if c[2] == "pre"]
REQUIRED = [c[0] for c in COLUMNS if c[3]]
MAXLEN = {c[0]: c[4] for c in COLUMNS if c[4]}
TEXT_KEYS = ["title", "description", "ingredients", "howto", "highlights", "faq", "geo_us", "brand_subtitle",
             "short_description", "seo_title", "seo_description", "food_nutrition", "food_allergen", "food_storage",
             "otc_efficacy", "otc_precautions", "otc_specs"]
