"""斑马 SKU 三边/重量检查 → 可直接上传斑马「批量修改SKU信息」的 Excel（page46 用・纯函数）.

口径 = 2026-10-07〜09 Boss 逐条拍板的规则（斑马 Export 一括整备时用过的）:
- 范围: 有效 SKU、顶层类目 Export。只补「缺失」(占位值)，不覆盖已有实值。
  例外: 组合品重量 = 单品重量 × 件数，不一致就按计算值改（Boss 规则）。
- 重量占位值 = 0/1/10/100/200/300/500（g）。三边占位 = 有 0、1×1×1、10×10×10。
- 单品重量: JDL 实测毛重优先，NST 包装重量补位；两源都有且相差 ≥1.8 倍 → 人工。
  闸门: >5kg / <2g / JDL <6g → 人工。
- 单品三边: JDL 实测（cm 向上取整、长≥宽≥高）。JDL 会把整盒当单品 → 同 SPU 有实值的兄弟 SKU 时，
  三边都在 3cm 以内的兄弟至少一个才采用，否则人工；无兄弟参照的照用。最长边 >60cm → 人工。
- 组合品: 单品取斑马现值（实值）或本次补值。三边 = 同一单品×N 用 a×b×c 格子、混合用分列，
  各取「最长边→体积」最小的排法（10/7 Boss 确认「尽量组成长方形或正方形」）。
- 编码 `<JAN>_<N>` 的 N ≠ 明细件数、或 SKU 编码重复（模板按编码匹配）→ 不导入，列入问题。
"""
from __future__ import annotations

import io
import math
import re
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Font

W_PH = {0, 1, 10, 100, 200, 300, 500}
D_PH = {(1, 1, 1), (10, 10, 10)}
RATIO = 1.8
SIB_TOL = 3
MAX_EDGE = 60
CHUNK = 1000
TEMPLATE_HEAD = ["SKU编码", "成本价(CNY)", "重量(g)", "长(cm)", "宽(cm)", "高(cm)"]
TEMPLATE_EXAMPLE = [
    ["必填项，产品SKU基本编码", "可选项，单位是人民币", "可选项，单位是克", "可选项，单位是厘米", "可选项，单位是厘米", "可选项，单位是厘米"],
    ["ss-968-www-黑色", 88.88, 669, 12, 10, 5],
    ["ss-968-www-红色", 90.88, 789, 12, 9, 4],
]
_SUFFIX = re.compile(r"^\d{8,13}_(\d+)$")


def real_weight(w) -> int | None:
    return None if w is None or w in W_PH else int(w)


def real_dims(s: dict) -> tuple | None:
    d = (s.get("length"), s.get("width"), s.get("height"))
    if any(x is None or x <= 0 for x in d) or d in D_PH:
        return None
    return tuple(sorted(d, reverse=True))


def to_cm(mm: tuple) -> tuple | None:
    if not mm or any(x is None or x <= 0 for x in mm):
        return None
    return tuple(sorted((max(1, math.ceil(float(x) / 10 - 1e-9)) for x in mm), reverse=True))


def grid(d: tuple, n: int) -> tuple:
    """同一单品 n 个: 短×中×长 方向各放 a×b×c 个，不留整段空层，取最长边→体积最小。"""
    L, W, H = d
    best = None
    for a in range(1, n + 1):
        for b in range(1, n + 1):
            c = math.ceil(n / (a * b))
            if a * b * c - n >= a * b and c > 1:
                continue
            if a * b > n and c == 1 and a * b - n >= min(a, b):
                continue
            box = tuple(sorted((L * c, W * b, H * a), reverse=True))
            key = (box[0], box[0] * box[1] * box[2], box[1])
            if best is None or key < best[0]:
                best = (key, box)
    return best[1]


def columns(units: list[tuple]) -> tuple:
    """混合: 各单品以最短边叠放，分 k 列横排（贪心均衡列高），取最长边→体积最小的 k。"""
    Lm = max(u[0] for u in units)
    best = None
    for k in range(1, len(units) + 1):
        cols = [[] for _ in range(k)]
        for u in sorted(units, key=lambda u: -u[2]):
            min(cols, key=lambda c: sum(x[2] for x in c)).append(u)
        if any(not c for c in cols):
            continue
        box = tuple(sorted((Lm, sum(max(x[1] for x in c) for c in cols), max(sum(x[2] for x in c) for c in cols)),
                           reverse=True))
        key = (box[0], box[0] * box[1] * box[2], box[1])
        if best is None or key < best[0]:
            best = (key, box)
    return best[1]


def _single_weight(jdl_g, nst_g) -> tuple[int | None, str, str]:
    """→ (候选值, 来源, 问题)。问题非空 = 不补、转人工。"""
    j = round(float(jdl_g)) if jdl_g else None
    n = round(float(nst_g)) if nst_g else None
    if j and n and max(j, n) / min(j, n) >= RATIO:
        return None, "", f"JDL {j}g 与 NST {n}g 相差≥{RATIO}倍"
    v, src = (j, "JDL") if j else (n, "NST") if n else (None, "")
    if v is None:
        return None, "", ""
    if v > 5000 or v < 2 or (src == "JDL" and v < 6):
        return None, "", f"{src} {v}g 数值异常（>5kg / <2g / JDL<6g）"
    return v, src, ""


def build(skus: list[dict], jdl: dict, nst: dict) -> tuple[list[dict], list[dict]]:
    """skus = banma.sku_dims 全行；jdl = {jan: (weight_g, (l_mm, w_mm, h_mm))}；nst = {jan: package_weight_g}。
    → (导入行, 问题行)。导入行 weight/L/W/H 为 None = 该栏留空（斑马不改）。"""
    by_id = {s["sku_id"]: s for s in skus}
    valid = [s for s in skus if s.get("is_valid")]
    code_n: dict[str, int] = {}
    for s in valid:
        code_n[s["code"]] = code_n.get(s["code"], 0) + 1
    scope = [s for s in valid if s.get("category_top") == "Export"]
    sib: dict[str, list[tuple]] = {}
    for s in valid:
        if s.get("sku_type") != "组合" and (d := real_dims(s)):
            sib.setdefault(s["spu_id"], []).append((s["sku_id"], d))

    up: dict[str, dict] = {}
    issues: list[dict] = []
    fill_w: dict[str, int] = {}
    fill_d: dict[str, tuple] = {}

    def issue(s, kind, detail="", **kw):
        issues.append({"SKU编码": s["code"], "类型": s.get("sku_type"), "问题": kind, "说明": detail,
                       "斑马重量(g)": s.get("weight"),
                       "斑马三边(cm)": f"{s.get('length')}×{s.get('width')}×{s.get('height')}",
                       "商品名": s.get("spu_title"), "规格": s.get("specification"), **kw})

    def put(s, **kw):
        up.setdefault(s["sku_id"], {"code": s["code"], "s": s, "weight": None, "dims": None, "src": []})
        r = up[s["sku_id"]]
        if "weight" in kw:
            r["weight"] = kw["weight"]
            r["src"].append(f"重量:{kw['src']}")
        if "dims" in kw:
            r["dims"] = kw["dims"]
            r["src"].append(f"三边:{kw['src']}")

    # ── 单品 ──
    for s in scope:
        if s.get("sku_type") == "组合":
            continue
        jw, jmm = jdl.get(s["code"], (None, None))
        nw = nst.get(s["code"])
        cur_w = real_weight(s.get("weight"))
        if cur_w is None:
            v, src, why = _single_weight(jw, nw)
            if why:
                issue(s, "重量待人工", why, JDL重量=jw, NST重量=nw)
            elif v is None:
                issue(s, "重量缺失·无数据源", "JDL/NST 都没有")
            else:
                fill_w[s["sku_id"]] = v
                if v != s.get("weight"):
                    put(s, weight=v, src=src)
        elif jw and max(cur_w, float(jw)) / max(min(cur_w, float(jw)), 1) >= RATIO:
            issue(s, "斑马重量与JDL相差≥1.8倍", f"斑马 {cur_w}g / JDL {round(float(jw))}g", JDL重量=jw, NST重量=nw)

        if real_dims(s) is None:
            d = to_cm(jmm)
            if d is None:
                issue(s, "三边缺失·无数据源", "JDL 没有实测三边")
            elif d[0] > MAX_EDGE:
                issue(s, "三边待人工", f"JDL 最长边 {d[0]}cm >{MAX_EDGE}", JDL三边="×".join(map(str, d)))
            else:
                refs = [x for i, x in sib.get(s["spu_id"], []) if i != s["sku_id"]]
                if refs and not any(all(abs(a - b) <= SIB_TOL for a, b in zip(d, r)) for r in refs):
                    issue(s, "三边待人工", f"JDL 三边与同款兄弟 SKU 相差>{SIB_TOL}cm（JDL 可能按整盒测）",
                          JDL三边="×".join(map(str, d)), 同款三边=" / ".join("×".join(map(str, r)) for r in refs[:3]))
                else:
                    fill_d[s["sku_id"]] = d
                    put(s, dims=d, src="JDL")

    # ── 组合品 ──
    for s in scope:
        comb = s.get("combine") or []
        if s.get("sku_type") != "组合" or not comb:
            continue
        n = sum(int(c.get("Quantity") or 0) for c in comb)
        m = _SUFFIX.match(s["code"])
        if m and len(comb) == 1 and int(m.group(1)) != n:
            issue(s, "组合品·编码与明细件数不一致", f"编码后缀 {m.group(1)} / 明细 {n} 件")
            continue
        parts = []
        for c in comb:
            cs = by_id.get(str(c.get("SKUID")))
            cw = (real_weight(cs.get("weight")) or fill_w.get(cs["sku_id"])) if cs else None
            cd = (real_dims(cs) or fill_d.get(cs["sku_id"])) if cs else None
            parts.append((c.get("SKUCode"), int(c.get("Quantity") or 0), cw, cd))
        miss_w = [p[0] for p in parts if p[2] is None]
        if miss_w:
            if real_weight(s.get("weight")) is None:
                issue(s, "组合品·单品缺重量", "缺: " + ", ".join(map(str, miss_w)))
        else:
            exp = sum(q * w for _, q, w, _ in parts)
            if exp != s.get("weight"):
                put(s, weight=exp, src="组合计算" + ("" if real_weight(s.get("weight")) is None
                                                    else f"（原 {s.get('weight')}g≠单品×件数）"))
        if real_dims(s) is None:
            miss_d = [p[0] for p in parts if p[3] is None]
            if miss_d:
                issue(s, "组合品·单品缺三边", "缺: " + ", ".join(map(str, miss_d)))
            else:
                box = grid(parts[0][3], parts[0][1]) if len(parts) == 1 else \
                    columns([d for _, q, _, d in parts for _ in range(q)])
                put(s, dims=box, src="组合计算")

    rows = []
    for r in up.values():
        s = r["s"]
        if code_n.get(r["code"], 0) != 1:
            issue(s, "SKU编码重复", f"有效 SKU 中同编码 {code_n.get(r['code'])} 个，模板按编码匹配无法导入")
            continue
        d = r["dims"] or (None, None, None)
        rows.append({"SKU编码": r["code"], "重量(g)": r["weight"], "长(cm)": d[0], "宽(cm)": d[1], "高(cm)": d[2],
                     "来源": "；".join(r["src"]), "类型": s.get("sku_type"),
                     "原重量(g)": s.get("weight"), "原三边(cm)": f"{s.get('length')}×{s.get('width')}×{s.get('height')}",
                     "商品名": s.get("spu_title"), "规格": s.get("specification")})
    rows.sort(key=lambda r: (r["类型"] == "组合", r["SKU编码"]))
    return rows, issues


def _sheet(ws, head, rows):
    ws.append(head)
    for x in ws[1]:
        x.font = Font(bold=True)
    for r in rows:
        ws.append(r)


def template_xlsx(rows: list[dict]) -> bytes:
    """斑马「批量修改SKU信息」模板（数据 + 示例 两页）。成本价留空 = 不改。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append(TEMPLATE_HEAD)
    for i, r in enumerate(rows, start=2):
        ws.cell(i, 1, str(r["SKU编码"])).number_format = "@"
        for col, k in ((3, "重量(g)"), (4, "长(cm)"), (5, "宽(cm)"), (6, "高(cm)")):
            if r[k] is not None:
                ws.cell(i, col, int(r[k]))
    _sheet(wb.create_sheet("示例"), TEMPLATE_HEAD, TEMPLATE_EXAMPLE)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def table_xlsx(rows: list[dict], title: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    head = list(rows[0].keys()) if rows else ["(无)"]
    _sheet(ws, head, [[r.get(k) for k in head] for r in rows])
    ws.freeze_panes = "B2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload_zip(rows: list[dict], stamp: str) -> bytes:
    """≤1000 行/个的导入文件 + 一份带来源和原值的核对明细。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in range(0, len(rows), CHUNK):
            part = rows[k:k + CHUNK]
            z.writestr(f"斑马导入_{k // CHUNK + 1:02d}_{k + 1}-{k + len(part)}_{stamp}.xlsx", template_xlsx(part))
        z.writestr(f"导入明细_来源与原值_{stamp}.xlsx", table_xlsx(rows, "导入明细"))
    return buf.getvalue()
