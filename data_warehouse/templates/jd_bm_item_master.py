"""BM（斑马）商品登録模板 schema + NST 主档行 → BM 行映射

下载模板（Boss 2026-06-23 更新·严格按格式·含合并单元格·尤重 row1/row2）：
- BM（斑马）：Product导入模板.xlsx · sheet「数据」(46 列)
row1=分区标题（合并单元格）/ row2=列头 / row3+=数据。生成与模板逐格一致（脚本校验）。
JD（京东）出力は商品登録 v2 で廃止（Boss 2026-10-03）。

映射策略：
- BM SPU：使用 NST JANコード（13 位纯数字、唯一）（Boss 2026-05-29）
- BM ERP 类目：留空，用户后填（Boss 2026-05-29）
- 报关列（v2 · Boss 2026-10-03）：中文名称 = NST に上げるアイテム名そのもの /
  英文名称 = 调用方が渡す "_name_en"（通関英文名·≤76 字·無ければ空）/ 海关编码 = "_hs" /
  重量・报关重量・长宽高 = パッケージ（外装）の値だけ（商品本体寸法は使わない）

衍生规则：从 NST 主档行（dict, key=NST 模板列名 + "_hs" / "_name_en"）单行生成 BM 行。
图片 URL 由调用方传入（来自 nst.item_image_cache 的查询结果）。
"""
from __future__ import annotations

import datetime
import io
from typing import Iterable

# ───────────────────────── BM「Product导入模板」schema（新·2026-06-23） ─────────────────────────
# 源模板：Product导入模板.xlsx · sheet「数据」(46 列)
# row1=分区標題（合并单元格）/ row2=列頭（旧 BM_HEADER と同一·映射不変）/ row3+=データ
BM_SHEET_NAME = "数据"

BM_GROUP = (
    ["SPU(相同信息可以填写一样的)"] + [""] * 11    # 1-12  A1:L1
    + ["SKU"] + [""] * 16                          # 13-29 M1:AC1
    + ["普通报关信息"] + [""] * 5                   # 30-35 AD1:AI1
    + ["报关特殊属性"] + [""] * 7                   # 36-43 AJ1:AQ1
    + ["图片信息"]                                 # 44    AR1（単独）
    + ["质检制作要求", ""]                          # 45-46 AS1:AT1
)
assert len(BM_GROUP) == 46, len(BM_GROUP)

BM_NAME_EN_MAX = 76   # 斑马 英文名称の上限（Boss 2026-10-03）

BM_MERGES = ["A1:L1", "M1:AC1", "AD1:AI1", "AJ1:AQ1", "AS1:AT1"]

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


# ───────────────────────── NST → BM 行映射 ─────────────────────────

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


def nst_to_bm_row(
    nst_row: dict,
    *,
    image_url: str = "",
) -> list:
    """NST 行 → BM 行（list, len=46, 顺序 = BM_HEADER）。"""
    jan = _g(nst_row, "JANコード")
    name_ja = _g(nst_row, "アイテム名")
    cost = _g(nst_row, "商品原価")
    weight_g = _g(nst_row, "パッケージ重量(g)")   # v2: パッケージのみ（商品寸法へ落ちない）
    length = _g(nst_row, "パッケージ奥行(cm)")
    width = _g(nst_row, "パッケージ幅(cm)")
    height = _g(nst_row, "パッケージ高さ(cm)")
    # 上流 shared/hs_classify が MAX_NAME_EN=76 で単語境界カット済み。ここは取込エラー防止の最終ガード
    name_en = str(_g(nst_row, "_name_en")).strip()[:BM_NAME_EN_MAX]

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
    row[29] = name_ja                     # 中文名称 = NST に上げるアイテム名そのもの
    row[30] = name_en                     # 英文名称 = 通関英文名（≤76 字·無ければ空）
    row[33] = _num_or_blank(weight_g)     # 报关重量(g) = パッケージ重量
    row[34] = str(_g(nst_row, "_hs")).strip()  # 海关编码（判定できなければ空）
    row[43] = image_url                   # 产品图片(URL)
    return row


# ───────────────────────── xlsx 生成（openpyxl） ─────────────────────────

def _build_xlsx(sheet_name: str, group: list[str], header: list[str],
                data_rows: list[list], merges: list[str] | None = None) -> bytes:
    """生成 xlsx bytes · row1=分区标题（含合并单元格）/ row2=列头 / row3+=数据。

    merges: 'A1:U1' 等の合并范围リスト（テンプレ row1 分区と一致させる）。
    合并時は非アンカーセルを None にしてから merge（openpyxl 警告回避）。
    """
    from openpyxl import Workbook
    from openpyxl.utils import range_boundaries
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(group)
    ws.append(header)
    for r in data_rows:
        ws.append(r)
    for rng in (merges or []):
        c1, r1, c2, r2 = range_boundaries(rng)
        for col in range(c1, c2 + 1):           # アンカー以外を空に（merge 警告回避）
            for row in range(r1, r2 + 1):
                if not (col == c1 and row == r1):
                    ws.cell(row=row, column=col).value = None
        ws.merge_cells(rng)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()


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
    return _build_xlsx(BM_SHEET_NAME, BM_GROUP, BM_HEADER, data, merges=BM_MERGES)


def dated_filename_bm() -> str:
    return f"BM_{datetime.date.today():%Y%m%d}.xlsx"
