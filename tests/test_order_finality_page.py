"""page40（订单确定度）で踏んだ罠の回帰テスト — DB 不要。

2026-07-30 の実測で 1 件のサイレント故障を出した:
  open_reasons を `.str.split(" + ")` で割ったが 1 件も分割されなかった。
  pandas は長さ 2 以上の pat を **既定で正規表現扱い**するので、" + " の "+" が
  直前スペースの量詞になり、リテラルの「 + 」にマッチしない。
  例外も警告も出ず、「未入金 + 返金期間内」が 1 つの理由として集計され続ける。
"""
from __future__ import annotations

import pathlib

import pandas as pd

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "40_🧭_订单确定度.py"


def test_pandas_split_needs_regex_false():
    """罠そのものを固定する。既定では割れず、regex=False で割れる。"""
    s = pd.Series(["未入金 + 返金期間内"])
    assert s.str.split(" + ").iloc[0] == ["未入金 + 返金期間内"], (
        "pandas の既定挙動が変わった。page40 の分割方法を見直すこと")
    assert s.str.split(" + ", regex=False).iloc[0] == ["未入金", "返金期間内"]


def test_page_splits_open_reasons_literally():
    """page40 が regex=False 付きで割っていること。"""
    src = PAGE.read_text(encoding="utf-8")
    assert 'str.split(" + ", regex=False)' in src, (
        "open_reasons の分割から regex=False が消えている — "
        "理由が 1 件も分割されず集計が静かに壊れる")


def test_page_buckets_are_exhaustive():
    """open の内訳バケットは A〜D の 4 つで、ラベルが全部揃っていること。

    どれか欠けると内訳表が KeyError で落ちる（reindex(["A".."D"]) しているため）。
    """
    src = PAGE.read_text(encoding="utf-8")
    for k in "ABCD":
        assert f'"{k}": _u(' in src, f"バケット {k} のラベルが無い"
