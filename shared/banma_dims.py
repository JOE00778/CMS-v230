"""斑马信息健康度检查（page46 用・纯函数）: 三边/重量 → 可直接上传斑马「批量修改SKU信息」的 Excel；报关信息 → 问题清单.

口径 = 2026-10-07〜09 Boss 逐条拍板的规则（斑马 Export 一括整备时用过的）:
- 范围: 有效 SKU、顶层类目 Export、且 NST 有商品等级（A/B/C/NEW · Boss 2026-10-29「没有商品等级的不用检查」）。
  组合品看组成单品：全部单品都有等级才检查。
  NST 作成 NEW_DAYS 日内的单品不查 → 归 page47（Boss 2026-11-13「按对象分开」: 46 = 存量・47 = 新品；组合品 47 不看，留在 46）。只补「缺失」(占位值)，不覆盖已有实值。
  例外: 组合品重量 = 单品重量 × 件数，不一致就按计算值改（Boss 规则）。
- 重量占位值 = 0/1/10/100/200/300/500（g）。三边占位 = 有 0、1×1×1、10×10×10。
- 单品重量: NST 包装重量（JDL 已停用 · Boss 2026-11-13）。闸门: >5kg / <2g → 人工。
- 单品三边: NST 包装三边（page46 实时向 NST 取 · cm 向上取整、长≥宽≥高）。同 SPU 有实值的兄弟 SKU 时，
  三边都在 3cm 以内的兄弟至少一个才采用，否则人工；无兄弟参照的照用。最长边 >60cm → 人工。
- 组合品: 单品取斑马现值（实值）或本次补值。三边 = 同一单品×N 用 a×b×c 格子、混合用分列，
  各取「最长边→体积」最小的排法（10/7 Boss 确认「尽量组成长方形或正方形」）。
- 编码 `<JAN>_<N>` 的 N ≠ 明细件数、或 SKU 编码重复（模板按编码匹配）→ 不导入，列入问题。
"""
from __future__ import annotations

import io
import math
import re

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font

W_PH = {0, 1, 10, 100, 200, 300, 500}
D_PH = {(1, 1, 1), (10, 10, 10)}
RATIO = 1.8
SIB_TOL = 3
MAX_EDGE = 60
NEW_DAYS = 30   # NST 作成からこの日数以内 = 新品 → page47（page46 は見ない）
CHUNK = 1000
TEMPLATE_HEAD = ["SKU编码", "成本价(CNY)", "重量(g)", "长(cm)", "宽(cm)", "高(cm)"]
TEMPLATE_EXAMPLE = [
    ["必填项，产品SKU基本编码", "可选项，单位是人民币", "可选项，单位是克", "可选项，单位是厘米", "可选项，单位是厘米", "可选项，单位是厘米"],
    ["ss-968-www-黑色", 88.88, 669, 12, 10, 5],
    ["ss-968-www-红色", 90.88, 789, 12, 9, 4],
]
_SUFFIX = re.compile(r"^\d{8,13}_(\d+)$")


class Msg(str):
    """问题说明: 值 = 中文成句（Excel・测试照旧）；tpl/kw 留给 page46 按 UI 语言 t(tpl).format(**kw) 重排。"""

    def __new__(cls, tpl: str, **kw):
        o = super().__new__(cls, tpl.format(**kw))
        o.tpl, o.kw = tpl, kw
        return o


def real_weight(w) -> int | None:
    return None if w is None or w in W_PH else int(w)


def real_dims(s: dict) -> tuple | None:
    d = (s.get("length"), s.get("width"), s.get("height"))
    if any(x is None or x <= 0 for x in d) or d in D_PH:
        return None
    return tuple(sorted(d, reverse=True))


def nst_dims(d: tuple) -> tuple | None:
    """NST 包装三边（cm・小数あり）→ 向上取整・长≥宽≥高。欠け / 占位 = None。"""
    try:
        n = tuple(sorted((math.ceil(float(x)) for x in d), reverse=True))
    except (TypeError, ValueError):
        return None
    return n if n and all(x > 0 for x in n) and n not in D_PH else None


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


def _single_weight(nst_g) -> tuple[int | None, str, str]:
    """→ (候选值, 来源, 问题)。问题非空 = 不补、转人工。"""
    v = round(float(nst_g)) if nst_g else None
    if v is None:
        return None, "", ""
    if v > 5000 or v < 2:
        return None, "", Msg("NST {v}g 数值异常（>5kg / <2g）", v=v)
    return v, "NST", ""


def in_scope(skus: list[dict], ranked: set | None, new: set = frozenset()) -> list[dict]:
    """有效 · 顶层 Export · NST 有等级（组合品 = 全部组成单品有等级）。ranked=None 不限等级。
    new = NST 作成 NEW_DAYS 日内の JAN → その単品は page47 の担当なので除く（組合品は除かない）。"""
    def graded(s):
        if ranked is None:
            return True
        if s.get("sku_type") == "组合":
            comb = s.get("combine") or []
            return bool(comb) and all(str(c.get("SKUCode")) in ranked for c in comb)
        return s["code"] in ranked

    return [s for s in skus if s.get("is_valid") and s.get("category_top") == "Export" and graded(s)
            and not (s.get("sku_type") != "组合" and s["code"] in new)]


def build(skus: list[dict], nst: dict, dims: dict, ranked: set | None = None,
          new: set = frozenset()) -> tuple[list[dict], list[dict]]:
    """skus = banma.sku_dims 全行；nst = {jan: package_weight_g}；dims = {jan: NST 包装三边 cm（已 nst_dims）}；
    ranked = 有商品等级的 JAN（None = 不限）；new = 新品 JAN（page47 担当・不查）。
    → (导入行, 问题行)。导入行 weight/L/W/H 为 None = 该栏留空（斑马不改）。"""
    by_id = {s["sku_id"]: s for s in skus}
    valid = [s for s in skus if s.get("is_valid")]
    code_n: dict[str, int] = {}
    for s in valid:
        code_n[s["code"]] = code_n.get(s["code"], 0) + 1
    scope = in_scope(skus, ranked, new)
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
        nw = nst.get(s["code"])
        cur_w = real_weight(s.get("weight"))
        if cur_w is None:
            v, src, why = _single_weight(nw)
            if why:
                issue(s, "重量待人工", why, NST重量=nw)
            elif v is None:
                issue(s, "重量缺失·无数据源", Msg("NST 没有包装重量"))
            else:
                fill_w[s["sku_id"]] = v
                if v != s.get("weight"):
                    put(s, weight=v, src=src)
        elif nw and max(cur_w, float(nw)) / max(min(cur_w, float(nw)), 1) >= RATIO:
            issue(s, "斑马重量与NST相差≥1.8倍", f"斑马 {cur_w}g / NST {round(float(nw))}g", NST重量=nw)

        if real_dims(s) is None:
            d = dims.get(s["code"])
            if d is None:
                issue(s, "三边缺失·无数据源", Msg("NST 没有包装三边"))
            elif d[0] > MAX_EDGE:
                issue(s, "三边待人工", Msg("NST 最长边 {d}cm >{m}", d=d[0], m=MAX_EDGE), NST三边="×".join(map(str, d)))
            else:
                refs = [x for i, x in sib.get(s["spu_id"], []) if i != s["sku_id"]]
                if refs and not any(all(abs(a - b) <= SIB_TOL for a, b in zip(d, r)) for r in refs):
                    issue(s, "三边待人工", Msg("NST 三边与同款兄弟 SKU 相差>{tol}cm", tol=SIB_TOL),
                          NST三边="×".join(map(str, d)), 同款三边=" / ".join("×".join(map(str, r)) for r in refs[:3]))
                else:
                    fill_d[s["sku_id"]] = d
                    put(s, dims=d, src="NST")

    # ── 组合品 ──
    for s in scope:
        comb = s.get("combine") or []
        if s.get("sku_type") != "组合" or not comb:
            continue
        n = sum(int(c.get("Quantity") or 0) for c in comb)
        m = _SUFFIX.match(s["code"])
        if m and len(comb) == 1 and int(m.group(1)) != n:
            issue(s, "组合品·编码与明细件数不一致", Msg("编码后缀 {s} / 明细 {n} 件", s=m.group(1), n=n))
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
                issue(s, "组合品·单品缺重量", Msg("缺: {x}", x=", ".join(map(str, miss_w))))
        else:
            exp = sum(q * w for _, q, w, _ in parts)
            if exp != s.get("weight"):
                put(s, weight=exp, src="组合计算" + ("" if real_weight(s.get("weight")) is None
                                                    else f"（原 {s.get('weight')}g≠单品×件数）"))
        if real_dims(s) is None:
            miss_d = [p[0] for p in parts if p[3] is None]
            if miss_d:
                issue(s, "组合品·单品缺三边", Msg("缺: {x}", x=", ".join(map(str, miss_d))))
            else:
                box = grid(parts[0][3], parts[0][1]) if len(parts) == 1 else \
                    columns([d for _, q, _, d in parts for _ in range(q)])
                put(s, dims=box, src="组合计算")

    rows = []
    for r in up.values():
        s = r["s"]
        if code_n.get(r["code"], 0) != 1:
            issue(s, "SKU编码重复", Msg("有效 SKU 中同编码 {n} 个，模板按编码匹配无法导入", n=code_n.get(r["code"])))
            continue
        d = r["dims"] or (None, None, None)
        rows.append({"SKU编码": r["code"], "重量(g)": r["weight"], "长(cm)": d[0], "宽(cm)": d[1], "高(cm)": d[2],
                     "来源": "；".join(r["src"]), "类型": s.get("sku_type"),
                     "原重量(g)": s.get("weight"), "原三边(cm)": f"{s.get('length')}×{s.get('width')}×{s.get('height')}",
                     "商品名": s.get("spu_title"), "规格": s.get("specification")})
    rows.sort(key=lambda r: (r["类型"] == "组合", r["SKU编码"]))
    return rows, issues


_HS = re.compile(r"\d{6}(\d{2}){0,2}")
_SPEC = re.compile(r", \d+(?:\.\d+)?(?:ml|g|L|kg)$")
_SET = re.compile(r" (\d+)set$")


def customs_issues(skus: list[dict], customs: dict, std: list[tuple], ranked: set | None,
                   new: set = frozenset()) -> list[dict]:
    """报关信息检查（只列问题，斑马「批量修改SKU信息」模板不含报关栏 → 无导入文件）。
    customs = {sku_id: {name_cn, name_en, hs_code}}；std = [(hs, name_cn, name_en)]（9/19 统一的标准英文报关名）。
    英文名标准 = 「标准品类句」或「标准品类句, 容量(ml/g/L/kg)」，组合品末尾再加「 Nset」（10/23 Boss）。"""
    hs_of: dict[str, set] = {}
    for hs, _, en in std:
        hs_of.setdefault(en, set()).update({hs} if hs else set())  # 审查规则句无 HS → 只算「标准名」，不判对应
    out = []
    for s in in_scope(skus, ranked, new):
        c = customs.get(s["sku_id"])
        base = {"SKU编码": s["code"], "类型": s.get("sku_type"), "商品名": s.get("spu_title"),
                "规格": s.get("specification"), "斑马英文名": (c or {}).get("name_en"),
                "斑马HS": (c or {}).get("hs_code"), "斑马中文名": (c or {}).get("name_cn")}

        def add(kind, detail=""):
            out.append({**base, "问题": kind, "说明": detail})

        if c is None:
            add("无报关信息")
            continue
        hs = str(c.get("hs_code") or "").strip()
        en = str(c.get("name_en") or "").strip()
        if not _HS.fullmatch(hs):
            add("HS编码缺失或格式错误", Msg("「{hs}」不是 6/8/10 位数字", hs=hs))
        if en in ("", "-"):
            add("英文名为空或「-」")
            continue
        comb = s.get("combine") or []
        if s.get("sku_type") == "组合" and len(comb) == 1:
            n = int(comb[0].get("Quantity") or 0)
            m = _SET.search(en)
            if not m or int(m.group(1)) != n:
                add("组合品英文名缺「Nset」", Msg("应以「 {n}set」结尾", n=n))
        core = _SPEC.sub("", _SET.sub("", en))
        if core not in hs_of:
            add("英文名不是标准报关名", Msg("含品牌/商品名，或不在标准品类名表里"))
        elif _HS.fullmatch(hs) and hs_of[core] and hs[:6] not in hs_of[core]:
            add("英文名与HS不对应", Msg("「{core}」对应 HS {hs}", core=core, hs="/".join(sorted(hs_of[core]))))
    return out


def _sheet(ws, head, rows):
    ws.append(head)
    for x in ws[1]:
        x.font = Font(bold=True)
    for r in rows:  # 斑马商品名里混有控制字符，openpyxl 会直接报错 → 去掉
        ws.append([ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v for v in r])


# 问题类型 → 检查项（page46 勾选用）。「通用」= 任一项勾选都列；报关信息有无 = 英文名或 HS 勾选时列
GROUP = {
    "重量待人工": "重量", "重量缺失·无数据源": "重量", "斑马重量与NST相差≥1.8倍": "重量", "组合品·单品缺重量": "重量",
    "三边缺失·无数据源": "三边", "三边待人工": "三边", "组合品·单品缺三边": "三边",
    "英文名为空或「-」": "英文名", "英文名不是标准报关名": "英文名", "组合品英文名缺「Nset」": "英文名",
    "英文名与HS不对应": "英文名", "HS编码缺失或格式错误": "HS",
    "无报关信息": "报关", "组合品·编码与明细件数不一致": "通用", "SKU编码重复": "通用",
}


def select(rows: list[dict], issues: list[dict], checks: set[str]) -> tuple[list[dict], list[dict]]:
    """按勾选（重量 / 三边 / 英文名 / HS）筛导入行与问题。未勾的栏置空 = 斑马不改。"""
    out = []
    for r in rows:
        r = dict(r)
        if "重量" not in checks:
            r["重量(g)"] = None
        if "三边" not in checks:
            r["长(cm)"] = r["宽(cm)"] = r["高(cm)"] = None
        if r["重量(g)"] is not None or r["长(cm)"] is not None:
            r["来源"] = "；".join(x for x in r["来源"].split("；") if x.split(":")[0] in checks)
            out.append(r)
    keep = checks | {"通用"} | ({"报关"} if checks & {"英文名", "HS"} else set())
    return out, [i for i in issues if GROUP.get(i["问题"], "通用") in keep]


def report_xlsx(rows: list[dict], issues: list[dict]) -> bytes:
    """一个 Excel: 「数据」「示例」= 斑马「批量修改SKU信息」模板（数据 放第一页；成本价留空 = 不改），
    「问题清单」「导入明细」（来源与原值）。斑马每次 ≤1000 行 → 超出另起「数据2」「数据3」…
    （Boss 2026-10-29「行数超出另起一个工作表，会有人做检查和处理」）。"""
    wb = Workbook()
    wb.remove(wb.active)
    for n, k in enumerate(range(0, max(len(rows), 1), CHUNK), 1):
        ws = wb.create_sheet("数据" if n == 1 else f"数据{n}")
        ws.append(TEMPLATE_HEAD)
        for i, r in enumerate(rows[k:k + CHUNK], start=2):
            ws.cell(i, 1, str(r["SKU编码"])).number_format = "@"
            for col, key in ((3, "重量(g)"), (4, "长(cm)"), (5, "宽(cm)"), (6, "高(cm)")):
                if r[key] is not None:
                    ws.cell(i, col, int(r[key]))
    _sheet(wb.create_sheet("示例"), TEMPLATE_HEAD, TEMPLATE_EXAMPLE)
    for title, data in (("问题清单", issues), ("导入明细", rows)):
        sh = wb.create_sheet(title)
        head = list(data[0].keys()) if data else ["(无)"]
        _sheet(sh, head, [[d.get(k) for k in head] for d in data])
        sh.freeze_panes = "B2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
