"""JD/BM 商品登録模板 schema + NST 主档行 → JD/BM 行映射

源模板：20260000_NetSuite【アイテム】マスタ登録-V260326EX_登録者名.xlsx
内嵌 sheet：JD商品登録 / BM商品登録（提取于 2026-05-29）

映射策略（Boss 2026-05-29 拍板）：
- JD *货主编码：默认 "KH20000009340"（旧 HTML 商品登録ツール jdA 默认值）
- BM SPU：使用 NST JANコード（13 位纯数字、唯一）
- BM ERP 类目：留空，用户后填
- 英文名称（JD/BM 共）：留空，用户用别的工具翻

衍生规则：从 NST 主档行（dict, key=NST 模板列名）单行生成 JD/BM 行。
图片 URL 由调用方传入（来自 nst.item_image_cache 的查询结果）。
"""
from __future__ import annotations

import datetime
import io
from typing import Iterable

# ───────────────────────── JD 商品登録 schema ─────────────────────────

JD_GROUP = [
    "基本信息", "基本信息", "基本信息", "基本信息", "基本信息", "基本信息", "基本信息",
    "基本信息", "基本信息", "基本信息", "基本信息", "基本信息", "基本信息",
    "尺重信息", "尺重信息", "尺重信息", "尺重信息", "尺重信息",
    "商品申报信息", "商品申报信息", "商品申报信息", "商品申报信息", "商品申报信息",
    "商品申报信息", "商品申报信息", "商品申报信息", "商品申报信息", "商品申报信息",
    "商品申报信息", "商品申报信息",
    "销售渠道信息", "销售渠道信息", "销售渠道信息",
    "商品特性信息", "商品特性信息", "商品特性信息", "商品特性信息", "商品特性信息",
    "商品特性信息", "商品特性信息", "商品特性信息", "商品特性信息", "商品特性信息",
    "商品特性信息", "商品特性信息", "商品特性信息", "商品特性信息", "商品特性信息",
    "包装信息", "包装信息", "包装信息",
]

JD_HEADER = [
    "*货主编码", "*客户商品编码", "*英文名称", "*中文名称",
    "*是否有品牌\n（0-否；1-是）", "品牌名称", "*商品链接", "销售单位",
    "*件型\n（0-大件 \n1-中小件 \n2-小件 \n3-中件 ）",
    "三级品类编码", "规格型号", "款号", "原产国",
    "*重量(kg)", "*长(cm)", "*宽(cm)", "*高(cm)", "商品条码",
    "出口国/地区", "出口申报价值", "出口申报币种", "商品采购价格", "币种",
    "出口海关编码", "进口国/地区", "进口申报价值", "进口申报币种",
    "商品销售价格", "币种", "进口海关编码",
    "*销售渠道编码", "平台商品编码", "平台商品标题",
    "是否带电池\n（0-否；1-是）", "电池重量（kg）", "电池功率（wh）",
    "电池类型\n锂离子内置PI967/锂离子外置PI966/锂离子纯电PI965\n锂金属内置PI970/锂金属外置PI969/锂金属纯电PI968\n非锂电池",
    "是否危险品\n（0-否；1-是）",
    "UNCODE\n(1-UN3480; 2-UN3481; 3-UN1266; \n4-UN3090; 5-UN3091; 6-UN3171; \n7-UN1123; 8-UN1169; 9-UN1170; \n10-UN1230; 11-UN1263; 12-UN1770; \n13-UN1950; 14-UN1987; 15-UN1993; \n16-UN3082; 17-UN3175)",
    "是否带粉末\n（0-否；1-是）", "是否带液\n（0-否；1-是）",
    "是否带磁\n（0-否；1-是）", "是否带原木\n（0-否；1-是）",
    "是否易碎品\n（0-否；1-是）", "是否食品\n（0-否；1-是）",
    "是否有过敏原\n（0-否；1-是）", "过敏原",
    "是否耗材\n（0-否；1-是）", "是否自带物流原包\n（0-否；1-是）",
    "打包方式", "标准包装数",
]
assert len(JD_GROUP) == 51, f"JD_GROUP={len(JD_GROUP)}"
assert len(JD_HEADER) == 51, f"JD_HEADER={len(JD_HEADER)}"

DEFAULT_JD_CUSTOMER_CODE = "KH20000009340"
DEFAULT_JD_SALES_CHANNEL = "sc-三金商事株式会社"
DEFAULT_JD_PLATFORM_CODE = "Lazada/Shopee/coupang"
DEFAULT_PRODUCT_LINK = "https://kari"

# ───────────────────────── BM 商品登録 schema ─────────────────────────

BM_GROUP = [
    *(["SPU(相同信息可以填写一样的)"] * 12),
    *(["SKU"] * 17),
    *(["普通报关信息"] * 6),
    *(["报关特殊属性"] * 8),
    "图片信息",
    "质检制作要求", "质检制作要求",
]
assert len(BM_GROUP) == 46, len(BM_GROUP)

BM_HEADER = [
    "SPU", "产品标题", "ERP类目", "来源URL", "来源备注", "默认供应商名称",
    "富文本描述", "纯文本描述", "短描述", "SEO标题", "SEO关键字", "SEO描述",
    "SKU", "图片URL",
    "规格1名称", "规格1值", "规格2名称", "规格2值", "规格3名称", "规格3值",
    "规格4名称", "规格4值", "规格5名称", "规格5值",
    "成本价(￥)", "重量(g)", "长(cm)", "宽(cm)", "高(cm)",
    "中文名称", "英文名称",
    "材质", "申报价值(USD)", "报关重量(g)", "海关编码",
    "带电(非内置)", "带电(内置)", "带电(纯电池)", "带磁",
    "液体", "粉末", "刀具", "危险品",
    "产品图片(URL)",
    "项目名", "标准描述",
]


# ───────────────────────── NST → JD/BM 行映射 ─────────────────────────

def _g(nst_row: dict, *keys, default=""):
    """安全取 NST 行字段（多个候选 key 选第一个非空）。"""
    for k in keys:
        v = nst_row.get(k)
        if v is not None and str(v).strip() != "" and str(v).strip().lower() != "nan":
            return v
    return default


def _num_or_blank(v) -> str:
    if v in (None, ""): return ""
    s = str(v).strip()
    if s == "" or s.lower() == "nan": return ""
    return s


def nst_to_jd_row(
    nst_row: dict,
    *,
    image_url: str = "",
    jd_customer_code: str = DEFAULT_JD_CUSTOMER_CODE,
    sales_channel: str = DEFAULT_JD_SALES_CHANNEL,
    platform_code: str = DEFAULT_JD_PLATFORM_CODE,
) -> list:
    """NST 行 → JD 行（list, len=51, 顺序 = JD_HEADER）。"""
    jan = _g(nst_row, "JANコード")
    name_ja = _g(nst_row, "アイテム名")
    weight_g = _g(nst_row, "商品重量(g)", "パッケージ重量(g)")
    weight_kg = ""
    if weight_g not in ("", None):
        try:
            weight_kg = str(round(float(str(weight_g).replace(",", "")) / 1000, 3))
        except Exception:
            weight_kg = ""
    # NST 商品尺寸 → JD 长/宽/高（cm）
    length = _g(nst_row, "商品奥行(cm)", "パッケージ奥行(cm)")
    width = _g(nst_row, "商品幅(cm)", "パッケージ幅(cm)")
    height = _g(nst_row, "商品高さ(cm)", "パッケージ高さ(cm)")

    row = [""] * len(JD_HEADER)
    row[0]  = jd_customer_code            # *货主编码
    row[1]  = jan                         # *客户商品编码
    row[2]  = ""                          # *英文名称（留空，用户后填）
    row[3]  = name_ja                     # *中文名称（暂用日文，用户后改）
    row[4]  = "0"                         # *是否有品牌
    row[5]  = ""                          # 品牌名称
    row[6]  = DEFAULT_PRODUCT_LINK        # *商品链接
    row[7]  = "1"                         # 销售单位
    row[8]  = "1"                         # *件型
    row[10] = _g(nst_row, "メーカー型番")  # 规格型号
    row[13] = _num_or_blank(weight_kg)    # *重量(kg)
    row[14] = _num_or_blank(length)       # *长(cm)
    row[15] = _num_or_blank(width)        # *宽(cm)
    row[16] = _num_or_blank(height)       # *高(cm)
    row[17] = jan                         # 商品条码
    row[30] = sales_channel               # *销售渠道编码
    row[31] = platform_code               # 平台商品编码
    row[48] = "1"                         # 是否自带物流原包
    return row


def nst_to_bm_row(
    nst_row: dict,
    *,
    image_url: str = "",
) -> list:
    """NST 行 → BM 行（list, len=46, 顺序 = BM_HEADER）。"""
    jan = _g(nst_row, "JANコード")
    name_ja = _g(nst_row, "アイテム名")
    cost = _g(nst_row, "商品原価")
    weight_g = _g(nst_row, "商品重量(g)", "パッケージ重量(g)")
    length = _g(nst_row, "商品奥行(cm)", "パッケージ奥行(cm)")
    width = _g(nst_row, "商品幅(cm)", "パッケージ幅(cm)")
    height = _g(nst_row, "商品高さ(cm)", "パッケージ高さ(cm)")

    row = [""] * len(BM_HEADER)
    row[0]  = jan                         # SPU = JAN (Boss 拍板)
    row[1]  = name_ja                     # 产品标题
    row[2]  = ""                          # ERP类目（留空，Boss 拍板）
    row[12] = jan                         # SKU
    row[13] = image_url                   # 图片URL
    row[14] = "1"                         # 规格1名称（sample 模板值）
    row[15] = jan                         # 规格1值（sample 模板值）
    row[24] = _num_or_blank(cost)         # 成本价(￥)
    row[25] = _num_or_blank(weight_g)     # 重量(g)
    row[26] = _num_or_blank(length)       # 长(cm)
    row[27] = _num_or_blank(width)        # 宽(cm)
    row[28] = _num_or_blank(height)       # 高(cm)
    row[29] = name_ja                     # 中文名称（暂用日文）
    row[30] = ""                          # 英文名称（留空）
    row[43] = image_url                   # 产品图片(URL)
    return row


# ───────────────────────── xlsx 生成（openpyxl） ─────────────────────────

def _build_xlsx(sheet_name: str, group: list[str], header: list[str],
                data_rows: list[list]) -> bytes:
    """生成 xlsx bytes · row0=分组标题 / row1=列名 / row2+=数据。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(group)
    ws.append(header)
    for r in data_rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


def build_jd_xlsx(
    nst_rows: Iterable[dict],
    *,
    image_url_map: dict[str, str] | None = None,
    jd_customer_code: str = DEFAULT_JD_CUSTOMER_CODE,
    sales_channel: str = DEFAULT_JD_SALES_CHANNEL,
    platform_code: str = DEFAULT_JD_PLATFORM_CODE,
) -> bytes:
    image_url_map = image_url_map or {}
    data = []
    for n in nst_rows:
        jan = str(_g(n, "JANコード") or "").strip()
        data.append(nst_to_jd_row(
            n,
            image_url=image_url_map.get(jan, ""),
            jd_customer_code=jd_customer_code,
            sales_channel=sales_channel,
            platform_code=platform_code,
        ))
    return _build_xlsx("JD商品登録", JD_GROUP, JD_HEADER, data)


def build_bm_xlsx(
    nst_rows: Iterable[dict],
    *,
    image_url_map: dict[str, str] | None = None,
) -> bytes:
    image_url_map = image_url_map or {}
    data = []
    for n in nst_rows:
        jan = str(_g(n, "JANコード") or "").strip()
        data.append(nst_to_bm_row(n, image_url=image_url_map.get(jan, "")))
    return _build_xlsx("BM商品登録", BM_GROUP, BM_HEADER, data)


def dated_filename_jd() -> str:
    return f"JD_{datetime.date.today():%Y%m%d}.xlsx"


def dated_filename_bm() -> str:
    return f"BM_{datetime.date.today():%Y%m%d}.xlsx"
