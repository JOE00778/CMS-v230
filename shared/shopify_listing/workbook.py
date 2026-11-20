"""上架包 Excel：生成（README + 商品 + 字段说明 + 选项 + 样例 + 禁用词）与读回。

README 来自 rules_us.md（一行一条）；读回只读「商品」表、按第 1 行列名取值，其他工作表忽略。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from shared.shopify_listing import spec as S

_DIR = Path(__file__).resolve().parent
SHEET = "商品"
GREY = PatternFill("solid", fgColor="E5E7EB")
HEAD = PatternFill("solid", fgColor="1F2937")
WRAP = Alignment(wrap_text=True, vertical="top")


def rules_rows() -> list[tuple[str, str]]:
    """rules_us.md → [(章节, 规则)]。"""
    out, sec = [], ""
    for ln in (_DIR / "rules_us.md").read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            sec = ln[3:].strip()
        elif ln.startswith("# "):
            out.append(("", ln[2:].strip()))
        elif ln.startswith("- "):
            out.append((sec, ln[2:].strip()))
    return out


def samples() -> list[dict]:
    return json.loads((_DIR / "sample_us.json").read_text(encoding="utf-8"))


def _header(ws, keys: list[str], widths: dict[str, int] | None = None) -> None:
    for i, k in enumerate(keys, 1):
        c = ws.cell(1, i, k)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD
        ws.column_dimensions[c.column_letter].width = (widths or {}).get(k, 24)
    ws.freeze_panes = "B2"


def build(cands: list[dict]) -> bytes:
    wb = Workbook()
    # ① README
    ws = wb.active
    ws.title = "README"
    ws.append(["章节", "规则（AI 请逐条遵守）"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD
    for sec, rule in rules_rows():
        ws.append([sec, rule])
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 120
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = WRAP

    # ② 商品
    ws = wb.create_sheet(SHEET)
    _header(ws, S.COL_KEYS, {"jan": 16, "title": 50, "description": 60, "geo_us": 60, "nst_name": 36})
    kind = {k: kd for k, _, kd, *_ in S.COLUMNS}
    for i, (k, zh, kd, req, mx, note) in enumerate(S.COLUMNS, 1):
        who = {"pre": "系统预填·不要改", "info": "系统预填·仅参考", "ai": "AI 填"}[kd]
        ws.cell(1, i).comment = Comment(f"{zh}（{who}{'·必填' if req else ''}{f'·≤{mx}字' if mx else ''}）\n{note}", "SmikieJapan")
    for r, cand in enumerate(cands, 2):
        for i, k in enumerate(S.COL_KEYS, 1):
            v = cand.get(k)
            c = ws.cell(r, i, v if kind[k] != "ai" else None)
            if k == "jan":
                c.value = str(v)
                c.number_format = "@"
            open_vendor = k in ("vendor", "brand") and not cand.get("vendor")   # 口径表没有 → AI 填
            if kind[k] != "ai" and not open_vendor:
                c.fill = GREY
            c.alignment = WRAP
    n = max(len(cands) + 1, 2)
    col = {k: ws.cell(1, i).column_letter for i, k in enumerate(S.COL_KEYS, 1)}
    for k in ("jan",):
        for r in range(2, n + 50):
            ws[f"{col[k]}{r}"].number_format = "@"
    dv = DataValidation(type="list", formula1="=选项!$A$2:$A$" + str(1 + len(S.CATEGORIES)), allow_blank=True)
    dv.add(f"{col['l1']}2:{col['l1']}{n + 50}")
    n_l2 = sum(len(v[1]) for v in S.CATEGORIES.values())
    dv2 = DataValidation(type="list", formula1=f"=选项!$C$2:$C${1 + n_l2}", allow_blank=True)
    dv2.add(f"{col['l2']}2:{col['l2']}{n + 50}")
    dv3 = DataValidation(type="list", formula1='"standard,otc,food"', allow_blank=True)
    dv3.add(f"{col['page_type']}2:{col['page_type']}{n + 50}")
    for d in (dv, dv2, dv3):
        ws.add_data_validation(d)

    # ③ 字段说明
    ws = wb.create_sheet("字段说明")
    _header(ws, ["列名", "中文名", "谁填", "必填", "字数上限", "说明"], {"说明": 90})
    for k, zh, kd, req, mx, note in S.COLUMNS:
        ws.append([k, zh, {"pre": "系统预填·不要改", "info": "系统预填·仅参考", "ai": "AI 填"}[kd],
                   "是" if req else "", mx or "", note])

    # ④ 选项
    ws = wb.create_sheet("选项")
    _header(ws, ["大类 l1", "大类标签", "小类 l2（所属大类）", "小类标签", "所属大类",
                 "feat- 清单", "concern- 清单", "skin- 清单"], {"小类 l2（所属大类）": 22})
    for i, (l1, (t1, _)) in enumerate(S.CATEGORIES.items(), 2):
        ws.cell(i, 1, l1)
        ws.cell(i, 2, t1)
    r = 2
    for l1, (_, subs) in S.CATEGORIES.items():
        for l2, t2 in subs.items():
            ws.cell(r, 3, l2)
            ws.cell(r, 4, t2)
            ws.cell(r, 5, l1)
            r += 1
    for j, lst in enumerate((sorted(S.FEAT), sorted(S.CONCERN), sorted(S.SKIN)), 6):
        for i, v in enumerate(lst, 2):
            ws.cell(i, j, v)

    # ⑤ 样例
    ws = wb.create_sheet("样例")
    _header(ws, S.COL_KEYS, {"title": 50, "description": 60, "geo_us": 60})
    for s in samples():
        ws.append([s.get(k) for k in S.COL_KEYS])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = WRAP

    # ⑥ 禁用词
    ws = wb.create_sheet("禁用词")
    _header(ws, ["正则（不分大小写）", "原因", "级别"], {"原因": 60})
    for p, msg in S.BANNED_ERROR:
        ws.append([p, msg, "禁止（会被拦下）"])
    for p, msg in S.BANNED_WARN:
        ws.append([p, msg, "警告（品名/色号等误伤时可人工确认）"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def read(data: bytes) -> list[dict]:
    """上传的 Excel → [{列名: 值}]（只读「商品」表；空行跳过；JAN 一律转字符串）。"""
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    if SHEET not in wb.sheetnames:
        raise ValueError(f"Excel 里没有「{SHEET}」工作表")
    rows = list(wb[SHEET].iter_rows(values_only=True))
    if not rows:
        return []
    keys = [str(h).strip() if h is not None else "" for h in rows[0]]
    missing = [k for k in S.COL_KEYS if k not in keys]
    if missing:
        raise ValueError("「商品」表缺列（列名被改了？）：" + ", ".join(missing))
    out = []
    for r in rows[1:]:
        d = {k: v for k, v in zip(keys, r) if k}
        if not any(v not in (None, "") for v in d.values()):
            continue
        j = d.get("jan")
        if isinstance(j, float) and j.is_integer():
            d["jan"] = str(int(j))
        elif isinstance(j, int):
            d["jan"] = str(j)
        elif j is not None:
            d["jan"] = str(j).strip()
        out.append(d)
    return out
