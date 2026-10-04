"""商品登録 v2 の入力テンプレ（Excel）生成とアップロード解析。DB 非依存。

人が埋めるのは INPUT_COLUMNS の 12 列だけ。列名は NST テンプレ原本
（nst_item_master_template.xlsx · NetSuiteマスタ登録 row2）の原文そのまま。
固定値列・アイテム名・メーカー名・税率などは呼び出し側が NST 行を組むときに足す。

シート構成:
  商品登録  … row1=見出し（原文列名・説明はセルのコメント）/ row2〜=入力。read_upload はここだけ読む
  記入例    … 見本 1 行（読まない）
  _選択肢   … hidden。仕入先 / 商品担当者 / 大分類→定義名 対応表 / 大分類ごとの中分類列

中分類の連動（2026-10-03 実測して決めた）:
  openpyxl 3.1.5 は中黒入りの定義名（アウトドア・防災）も書けて読み戻せる。ただし
  Excel/LibreOffice で開いての確認は Mac に無いため不可。大分類は SuiteQL から動的に来るので、
  将来 空白・記号・セル番地に見える名前（例 "USA" は列名としても有効）が来ても壊れないよう、
  定義名は安全な別名 MID_01.. にし、中分類の入力規則は
  INDIRECT(VLOOKUP(大分類, 対応表, 2, 0)) で引く。"L01" は Excel が L1 セルと解釈しうるので避けた。
  連動はあくまで入力補助。正しさは read_upload の (大分類, 中分類) 組合せ検証で担保する。
"""
from __future__ import annotations

import datetime
import io
import unicodedata
from dataclasses import dataclass

from data_warehouse.templates.nst_item_master import NST_MASTER_COLUMNS, _norm

COL_JAN = "JANコード"
COL_COST = "商品原価"
COL_SUPPLIER = "仕入先1：仕入先"
COL_OWNER = "商品担当者"
COL_LARGE = "大分類"
COL_MIDDLE = "中分類"
COL_CARTON = "カートン入数"
COL_LOT = "発注ロット"
COL_PKG_DIMS = ("パッケージ高さ(cm)", "パッケージ幅(cm)", "パッケージ奥行(cm)")
COL_PKG_WEIGHT = "パッケージ重量(g)"

INPUT_COLUMNS: list[str] = [COL_JAN, COL_COST, COL_SUPPLIER, COL_OWNER, COL_LARGE, COL_MIDDLE,
                            COL_CARTON, COL_LOT, *COL_PKG_DIMS, COL_PKG_WEIGHT]
# 2026-10-03: jancode.xyz に商品名が無い JAN を登録できるよう任意列を足した。NST の列ではないので
# INPUT_COLUMNS には入れない（無い旧テンプレもそのまま読める）。rows には常にキーとして入る（空可）
COL_NAME_MANUAL = "アイテム名（任意）"
TEMPLATE_COLUMNS: list[str] = [COL_JAN, COL_NAME_MANUAL, *INPUT_COLUMNS[1:]]

SHEET_INPUT = "商品登録"
SHEET_EXAMPLE = "記入例"
SHEET_CHOICES = "_選択肢"
FIRST_DATA_ROW = 2
LAST_DATA_ROW = 1001

_REQUIRED = {name for mark, name in NST_MASTER_COLUMNS if mark == "必須"}

# 見出しコメント（原本 row3 を短くしたもの）
_HELP = {
    COL_JAN: "JANコード（8 桁 or 13 桁）。型番にも同じ値が入ります",
    COL_NAME_MANUAL: "通常は空のまま（jancode.xyz の商品名が自動で入ります）。"
                     "jancode に商品名が無い JAN だけ記入。記入した場合はこちらが優先。最大 60 字",
    COL_COST: "商品の原価（日本円・仕入価格）。「定義原価」ではありません",
    COL_SUPPLIER: "選択肢以外入力禁止。無い場合は「NetSuite【仕入先】マスタ登録依頼書」で登録依頼",
    COL_OWNER: "このアイテムの担当者を選択",
    COL_LARGE: "大分類を選択",
    COL_MIDDLE: "大分類を先に選ぶと、その中分類だけが出ます",
    COL_CARTON: "カートン入数（正の整数）",
    COL_LOT: "発注時のロット数（正の整数）",
    COL_PKG_DIMS[0]: "外装パッケージの高さ（整数または小数）。空なら網調べ or 後で記入",
    COL_PKG_DIMS[1]: "外装パッケージの幅（整数または小数）",
    COL_PKG_DIMS[2]: "外装パッケージの奥行（整数または小数）",
    COL_PKG_WEIGHT: "外装込みの重量 g（整数または小数）。分からなければ空",
}


@dataclass
class Issue:
    row: int        # Excel 上の行番号（見出し不備などファイル全体の問題は 0）
    jan: str
    level: str      # "error" | "warn"
    message: str


def template_filename() -> str:
    return f"商品登録_入力テンプレ_{datetime.date.today():%Y%m%d}.xlsx"


# ───────────────────────── テンプレ生成 ─────────────────────────

def _quoted(sheet: str) -> str:
    return "'" + sheet.replace("'", "''") + "'"


def _list_dv(formula1: str, col_letter: str, title: str):
    from openpyxl.worksheet.datavalidation import DataValidation
    # showErrorMessage は openpyxl 既定 False（選択肢外も通る）なので必ず True。showDropDown は触らない
    dv = DataValidation(type="list", formula1=formula1, allow_blank=True,
                        showErrorMessage=True, errorStyle="stop",
                        errorTitle=title, error="選択肢から選んでください")
    dv.add(f"{col_letter}{FIRST_DATA_ROW}:{col_letter}{LAST_DATA_ROW}")
    return dv


def _write_header(ws) -> None:
    from openpyxl.comments import Comment
    from openpyxl.styles import Font, PatternFill
    req_fill = PatternFill("solid", fgColor="F4B084")
    opt_fill = PatternFill("solid", fgColor="D9D9D9")
    for i, name in enumerate(TEMPLATE_COLUMNS, start=1):
        c = ws.cell(row=1, column=i, value=name)
        c.font = Font(bold=True)
        c.fill = req_fill if name in _REQUIRED else opt_fill
        c.comment = Comment(("【必須】" if name in _REQUIRED else "") + _HELP[name], "CMS")
        ws.column_dimensions[c.column_letter].width = 34 if name in (COL_SUPPLIER, COL_NAME_MANUAL) else 16


def build_template(choices) -> bytes:
    """choices は shared.nst_choices.Choices（suppliers / owners / middle_by_large / large）。"""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_INPUT
    ex = wb.create_sheet(SHEET_EXAMPLE)
    ch = wb.create_sheet(SHEET_CHOICES)
    ch.sheet_state = "hidden"
    q = _quoted(SHEET_CHOICES)

    # _選択肢: A=仕入先 B=商品担当者 C=大分類 D=定義名 E〜=大分類ごとの中分類
    large = list(choices.large)
    for col, title, values in ((1, "仕入先", choices.suppliers), (2, "商品担当者", choices.owners),
                               (3, "大分類", large),
                               (4, "定義名", [f"MID_{i:02d}" for i in range(1, len(large) + 1)])):
        ch.cell(row=1, column=col, value=title)
        for r, v in enumerate(values, start=2):
            ch.cell(row=r, column=col, value=v)
    for i, lg in enumerate(large, start=1):
        col = 4 + i
        letter = get_column_letter(col)
        middles = choices.middle_by_large[lg]
        ch.cell(row=1, column=col, value=lg)
        for r, v in enumerate(middles, start=2):
            ch.cell(row=r, column=col, value=v)
        end = max(len(middles), 1) + 1
        wb.defined_names[f"MID_{i:02d}"] = DefinedName(
            f"MID_{i:02d}", attr_text=f"{q}!${letter}$2:${letter}${end}")

    def rng(letter: str, n: int) -> str:
        return f"{q}!${letter}$2:${letter}${max(n, 1) + 1}"

    _write_header(ws)
    ws.freeze_panes = "B2"
    letter_of = {name: get_column_letter(i) for i, name in enumerate(TEMPLATE_COLUMNS, start=1)}
    for r in range(FIRST_DATA_ROW, LAST_DATA_ROW + 1):   # JAN は文字列で入れてもらう（指数表記・桁落ち防止）
        ws[f"{letter_of[COL_JAN]}{r}"].number_format = "@"
    large_cell = f"${letter_of[COL_LARGE]}{FIRST_DATA_ROW}"
    for dv in (
        _list_dv(rng("A", len(choices.suppliers)), letter_of[COL_SUPPLIER], COL_SUPPLIER),
        _list_dv(rng("B", len(choices.owners)), letter_of[COL_OWNER], COL_OWNER),
        _list_dv(rng("C", len(large)), letter_of[COL_LARGE], COL_LARGE),
        _list_dv(f"INDIRECT(VLOOKUP({large_cell},{q}!$C$2:$D${max(len(large), 1) + 1},2,0))",
                 letter_of[COL_MIDDLE], COL_MIDDLE),
    ):
        ws.add_data_validation(dv)

    # 記入例
    _write_header(ex)
    lg0 = large[0] if large else ""
    sample = {COL_JAN: "4901234567894", COL_NAME_MANUAL: "", COL_COST: 1250,
              COL_SUPPLIER: choices.suppliers[0] if choices.suppliers else "",
              COL_OWNER: choices.owners[0] if choices.owners else "",
              COL_LARGE: lg0, COL_MIDDLE: (choices.middle_by_large.get(lg0) or ("",))[0],
              COL_CARTON: 48, COL_LOT: 12, COL_PKG_DIMS[0]: 6, COL_PKG_DIMS[1]: 23,
              COL_PKG_DIMS[2]: 17, COL_PKG_WEIGHT: 250}
    for i, name in enumerate(TEMPLATE_COLUMNS, start=1):
        ex.cell(row=2, column=i, value=sample[name])
    ex.cell(row=4, column=1, value="※ このシートは読み込まれません。「商品登録」シートに記入してください")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ───────────────────────── アップロード解析 ─────────────────────────

def _s(v) -> str:
    """セル値を文字列に。Excel 由来の 4901234567890.0 を潰し、前後空白を落とす。"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip().strip("　").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def _nfkc(s: str) -> str:
    """数値系の列だけ全角数字・全角ピリオドを半角に。"""
    return _s(unicodedata.normalize("NFKC", s))


def jan_check_ok(jan: str) -> bool:
    """GTIN（JAN-8 / JAN-13）のチェックディジット。右端の 1 つ手前から 3,1,3,1… の重み。"""
    d = [int(c) for c in jan]
    total = sum(x * (3 if i % 2 == 0 else 1) for i, x in enumerate(reversed(d[:-1])))
    return (10 - total % 10) % 10 == d[-1]


def _open(file):
    from openpyxl import load_workbook
    if isinstance(file, (bytes, bytearray)):
        file = io.BytesIO(file)
    return load_workbook(file, data_only=True, read_only=True)


def read_upload(file, choices) -> tuple[list[dict], list[Issue]]:
    """「商品登録」シートを読む。正常行（warn 込み）だけ rows で返し、error 行は issues にだけ入れる。

    件数: ok = len(rows) / error 行数 = len({i.row for i in issues if i.level == "error" and i.row})
          / warn 行数 = len({i.row for i in issues if i.level == "warn"})
    """
    wb = _open(file)
    if SHEET_INPUT not in wb.sheetnames:
        return [], [Issue(0, "", "error", f"シート「{SHEET_INPUT}」がありません")]
    all_rows = list(wb[SHEET_INPUT].iter_rows(values_only=True))
    wb.close()

    # 見出し行: 先頭 10 行で JANコード を含む最初の行
    header_idx = next((i for i, r in enumerate(all_rows[:10])
                       if any(_norm(_s(v)) == COL_JAN for v in r)), None)
    if header_idx is None:
        return [], [Issue(0, "", "error", f"見出し「{COL_JAN}」が見つかりません")]
    pos = {}
    for j, v in enumerate(all_rows[header_idx]):
        pos.setdefault(_norm(_s(v)), j)
    missing = [c for c in INPUT_COLUMNS if _norm(c) not in pos]
    if missing:
        return [], [Issue(header_idx + 1, "", "error", "見出しが見つかりません: " + " / ".join(missing))]

    suppliers, owners = set(choices.suppliers), set(choices.owners)
    mbl = choices.middle_by_large
    rows: list[dict] = []
    issues: list[Issue] = []
    seen: dict[str, int] = {}

    for i, raw in enumerate(all_rows[header_idx + 1:], start=header_idx + 2):
        rec = {c: _s(raw[pos[_norm(c)]]) if pos[_norm(c)] < len(raw) else "" for c in INPUT_COLUMNS}
        p_name = pos.get(_norm(COL_NAME_MANUAL))
        rec[COL_NAME_MANUAL] = _s(raw[p_name]) if p_name is not None and p_name < len(raw) else ""
        if not any(rec.values()):
            continue
        for c in (COL_JAN, COL_COST, COL_CARTON, COL_LOT, *COL_PKG_DIMS, COL_PKG_WEIGHT):
            rec[c] = _nfkc(rec[c])
        jan = rec[COL_JAN]
        errs: list[str] = []
        warns: list[str] = []

        if not (jan.isdigit() and len(jan) in (8, 13)):
            errs.append(f"JANコードが 8 桁 / 13 桁の数字ではありません（{jan or '空'}）")
        elif not jan_check_ok(jan):
            errs.append("JANコードのチェックディジットが合いません")
        elif jan in seen:
            errs.append(f"JANコードがファイル内で重複しています（{seen[jan]} 行目と同じ）")
        else:
            seen[jan] = i

        cost = _num(rec[COL_COST])
        if cost is None or not cost > 0:
            errs.append(f"商品原価が正の数ではありません（{rec[COL_COST] or '空'}）")
        if rec[COL_SUPPLIER] not in suppliers:
            errs.append(f"仕入先が選択肢にありません（{rec[COL_SUPPLIER] or '空'}）")
        if rec[COL_OWNER] not in owners:
            errs.append(f"商品担当者が選択肢にありません（{rec[COL_OWNER] or '空'}）")
        if rec[COL_LARGE] not in mbl:
            errs.append(f"大分類が選択肢にありません（{rec[COL_LARGE] or '空'}）")
        elif rec[COL_MIDDLE] not in mbl[rec[COL_LARGE]]:
            errs.append(f"大分類「{rec[COL_LARGE]}」に中分類「{rec[COL_MIDDLE] or '空'}」はありません")
        for c in (COL_CARTON, COL_LOT):
            if rec[c] == "":
                warns.append(f"{c} が空です")
            elif not (rec[c].isdigit() and int(rec[c]) > 0):
                errs.append(f"{c} が正の整数ではありません（{rec[c]}）")

        for c in (*COL_PKG_DIMS, COL_PKG_WEIGHT):
            if rec[c] and not ((n := _num(rec[c])) is not None and n > 0):
                warns.append(f"{c} が正の数ではないので空にしました（{rec[c]}）")
                rec[c] = ""
        if any(rec[c] == "" for c in COL_PKG_DIMS):
            warns.append("パッケージ 3 辺のどれかが空です（網調べ or 手入力）")

        if errs:
            issues.extend(Issue(i, jan, "error", m) for m in errs)
            continue
        issues.extend(Issue(i, jan, "warn", m) for m in warns)
        rows.append(rec)
    return rows, issues
