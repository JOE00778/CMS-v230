"""模块 #商品检索 · CSV 导出.

把检索结果 DataFrame 导出为 CSV。两个口径：
- to_csv_bytes()：返回 utf-8-sig 编码 bytes，直接喂 streamlit download_button
  （BOM 让 Excel 正确识别中日文）。
- write_csv()：落盘（参照 modules/rank_classifier/proposal.export_csv 风格）。

可选 column_labels 把内部列名换成三语表头（page 传 i18n 后的 dict）。
导出行数 == 输入 DataFrame 行数（DoD 断言点）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .queries import RESULT_COLUMNS


def _prepare(df: pd.DataFrame, column_labels: dict | None) -> pd.DataFrame:
    """只保留 RESULT_COLUMNS 中存在的列，按需重命名表头。"""
    cols = [c for c in RESULT_COLUMNS if c in df.columns]
    out = df[cols].copy() if cols else df.copy()
    if column_labels:
        out = out.rename(columns=column_labels)
    return out


def to_csv_bytes(df: pd.DataFrame, column_labels: dict | None = None) -> bytes:
    """检索结果 → CSV bytes（utf-8-sig · 含表头）。"""
    return _prepare(df, column_labels).to_csv(index=False).encode("utf-8-sig")


def write_csv(df: pd.DataFrame, path: str | Path,
              column_labels: dict | None = None) -> int:
    """落盘 CSV，返回写入的数据行数（不含表头）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = _prepare(df, column_labels)
    out.to_csv(p, index=False, encoding="utf-8-sig")
    return len(out)
