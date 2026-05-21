"""NST「アイテム」マスタ登録 模板（V260326EX）列定义 + 统一 CSV 导出器。

来源: data_warehouse/templates/nst_item_master_template.xlsx
      （Boss 2026-05-21 指定为今后一切 NST 数据变更的统一上传模板）

模板主表 NetSuiteマスタ登録 共 71 列。原 Excel 行结构:
  R1 必須标记 / R2 字段名(表头) / R3 输入说明 / R4 示例 / R5+ 数据
生成 CSV 时按手册流程: 删 R1/R3/R4(及示例数据), 保留 R2 作表头, 删非输入空列。

NetSuite CSV Import(Update) 用 Internal ID 作匹配键, 故 CSV 第一列固定为
"Internal ID"(非模板内列), 其后接本次实际变更的模板列(已删空列)。

字段映射约定(Boss 2026-05-21 拍板):
  - 定義原価(page03 Standard Cost) → 模板「商品原価」列(模板叫法不同, 同 Q 列位置)
  - 商品等级(page07 new_rank)       → 模板「商品ランク」列
"""
from __future__ import annotations

import csv
import datetime
import io

# NetSuite CSV Import 匹配键(非模板内列, 固定第一列)
ID_LABEL = "Internal ID"

# 常用字段列名(模板 R2 原文)
COL_ITEM_CODE = "型番"
COL_RANK = "商品ランク"
COL_COST = "商品原価"  # ← 定義原価 填此列(Boss 2026-05-21)

# 下载文件名基底：原模板名（前缀加当天日期 YYYYMMDD · Boss 2026-05-21）
TEMPLATE_BASENAME = "NetSuite【アイテム】マスタ登録-V260326EX_登録者名"

# 模板主表 71 列: (必須标记, 列名) · 顺序 = Excel 列顺序
NST_MASTER_COLUMNS: list[tuple[str | None, str]] = [
    ('必須', '型番'),
    ('必須', 'アイテム名'),
    ('必須', 'JANコード'),
    ('★ASEAN必須', 'メーカー名'),
    (None, 'メーカー型番'),
    ('※JD倉庫用', '商品サイズ'),
    ('※JD倉庫用', '段ボール梱包'),
    ('★輸出専用項目', 'SKUID'),
    ('必須', '取扱区分'),
    ('★ASEAN必須', '商品ランク'),
    ('必須', '商品担当者'),
    ('必須', '部門'),
    ('必須', '仕入区分'),
    ('必須', '大分類'),
    ('必須', '中分類'),
    ('必須', '税率'),
    ('必須', '商品原価'),
    ('必須', '仕入先1：仕入先'),
    (None, '発売日'),
    (None, '商品高さ(cm)'),
    (None, '商品幅(cm)'),
    (None, '商品奥行(cm)'),
    (None, '商品重量(g)'),
    ('必須', 'パッケージ高さ(cm)'),
    ('必須', 'パッケージ幅(cm)'),
    ('必須', 'パッケージ奥行(cm)'),
    (None, 'パッケージ重量(g)'),
    ('必須', 'カートン高さ(cm)'),
    ('必須', 'カートン幅(cm)'),
    ('必須', 'カートン奥行(cm)'),
    (None, 'カートン重量(g)'),
    ('必須', 'カートン入数'),
    (None, '発注ロット'),
    ('必須', '発注通貨'),
    (None, '生産納期'),
    (None, '製造工場名'),
    (None, '保証期間'),
    ('海外仕入：必須', '20GP入数'),
    ('海外仕入：必須', '不良対応方法（社内）'),
    ('海外仕入：必須', '不良対応方法（仕入先）'),
    ('海外仕入：必須', '仕入先不良対応詳細'),
    ('海外仕入：必須', 'コンテナ運送費'),
    ('海外仕入：必須', '開発費単価'),
    ('海外仕入：必須', '不良率自社負担単価'),
    ('海外仕入：必須', '不良率(PO)'),
    ('海外仕入：必須', '関税(AT)'),
    ('海外仕入：必須', '関税率'),
    ('海外仕入：必須', '生原価'),
    ('海外仕入：必須', '中国手数料'),
    (None, 'ライセンス料(AT)'),
    (None, 'ライセンス率'),
    (None, 'その他費用'),
    (None, '開発の説明'),
    ('ライセンス時：必須', '参考販売価格(税抜)'),
    (None, ' 直送アイテム　'),
    (None, '特注アイテム'),
    (None, '直送送料'),
    ('！！！自動入力項目　編集禁止！！！', '適正在庫水準の日数'),
    (None, '購買リード・タイム'),
    (None, '安全在庫日数'),
    (None, 'リードタイムを自動計算'),
    (None, '購入価格'),
    (None, '仕入先1：優先'),
    (None, '輸入諸掛をトラッキング'),
    (None, '優先場所'),
    (None, '保管棚を使用'),
    (None, '売上原価勘定'),
    (None, 'サポート提供 '),
    (None, 'NE_連携対象'),
    (None, '納税スケジュール'),
    (None, '原価計算法'),
]

VALID_COLUMNS = {name for _, name in NST_MASTER_COLUMNS}
_COL_ORDER = {name: i for i, (_, name) in enumerate(NST_MASTER_COLUMNS)}


def build_nst_master_csv(
    rows: list[dict],
    field_columns: list[str],
    *,
    id_label: str = ID_LABEL,
) -> bytes:
    """生成 NST 上传模板格式 CSV（bytes, UTF-8 BOM）。

    Args:
        rows: 每行 dict, 含 id_label(Internal ID) + 各 field_columns 的值
        field_columns: 本次输出的模板列名(只传有数据的列 = 已删空列)

    Returns:
        CSV bytes(utf-8-sig), 表头 = [id_label] + field_columns(按模板列序)
    """
    bad = [c for c in field_columns if c not in VALID_COLUMNS]
    if bad:
        raise ValueError(f"非模板列名: {bad}")
    ordered = sorted(field_columns, key=lambda c: _COL_ORDER[c])
    header = [id_label] + ordered
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([r.get(id_label, "")] + [r.get(c, "") for c in ordered])
    return buf.getvalue().encode("utf-8-sig")


def dated_filename(ext: str = "csv") -> str:
    """下载文件名：当天日期前缀 + 原模板名。

    例: 20260521_NetSuite【アイテム】マスタ登録-V260326EX_登録者名.csv
    """
    return f"{datetime.date.today():%Y%m%d}_{TEMPLATE_BASENAME}.{ext}"
