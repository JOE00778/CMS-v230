"""pages/ 横断の DB ヘルパ — 各页で重複していた _df（SELECT→DataFrame）を集約。

各 page は従来 module/関数スコープで同型の `_df` を定義していた（06/14/17/18/25/28/30）。
本ヘルパに寄せて重複を解消する。conn は呼び出し側が持つ（page ごとに get_connection 済み）
ので引数で受ける。page32 の `_df` は失敗時 rollback を持つ別物なので集約対象外。
"""
from __future__ import annotations

import pandas as pd


def df(conn, sql: str, params=None) -> pd.DataFrame:
    """SELECT クエリを実行し DataFrame で返す。

    params 無し → `conn.execute(sql)`（page17/25 の既存挙動·PG/SQLite 両対応）。
    params 有り（dict / tuple）→ `conn.execute(sql, params)`。
    """
    cur = conn.execute(sql, params) if params else conn.execute(sql)
    return pd.DataFrame([dict(r) for r in cur.fetchall()])
