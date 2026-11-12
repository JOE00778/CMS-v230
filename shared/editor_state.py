"""言語を切り替えても st.data_editor の未保存の編集を失わない（全 CMS 中日対応・2026-11-12）。

Streamlit 1.57 の data_editor は data と column_config を element id に入れる（key は主キーではない）。表頭・表示値が
t() で言語ごとに変わる = 切替の rerun で別の表になり、フロントの編集（edited/added/deleted_rows）は捨てられる。

lang_stable_editor は st.data_editor の代わり。各 rerun の結果を覚えておき、言語が変わった rerun でだけ
「前の言語での結果」の編集可能列を新しい言語の df に重ねて data として渡す（行は元 df の位置で対応・足した行は末尾）。
同じ言語の間は同じ data を渡すので、フロントの編集はそのまま効く。

呼び出し側の約束:
  - 編集できる列の列名は言語で変えない固定キー（表示名は column_config の label）。disabled 列は値も列名も翻訳してよい。
  - データ元が変わった（再処理・保存後の読み直し・絞り込み）ら累計編集を捨てる。既定の判定 = 表の形・index・
    編集できる列・表示しない列（column_order 外 / column_config が None）の hash。表示する disabled 列は言語で
    変わりうるので入らない → 行の集合が変わっても行数が同じことがある表は、言語で変わらない行の識別値
    （line_id などのタプル）を reset= に渡す（既定の判定に足される）。
  - 別のページへ移ったら累計編集は捨てる（i18n.lang_selector → forget_all。原生 data_editor も表が消えれば捨てる）。
"""
from __future__ import annotations

import hashlib

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from shared import i18n


_SK = "_lang_stable_editor:"


def forget_all() -> None:
    """累計編集を全部捨てる。別のページから入った run で i18n.lang_selector が呼ぶ。"""
    for k in [k for k in st.session_state if str(k).startswith(_SK)]:
        del st.session_state[k]


def _editable(df: pd.DataFrame, kwargs: dict) -> list:
    """編集できる列（disabled・column_config で disabled / 非表示・column_order 外を除く）。"""
    disabled = kwargs.get("disabled", False)
    if disabled is True:
        return []
    off = set(disabled or ())
    cc = kwargs.get("column_config") or {}
    order = kwargs.get("column_order")
    for c, v in cc.items():
        if v is None or (isinstance(v, dict) and v.get("disabled")):
            off.add(c)
    return [c for c in df.columns if c not in off and (order is None or c in order)]


def _hidden(df: pd.DataFrame, kwargs: dict) -> list:
    """表示しない列（column_order 外・column_config が None）。訳さない保存キー（line_id など）が多い。"""
    cc = kwargs.get("column_config") or {}
    order = kwargs.get("column_order")
    return [c for c in df.columns if (c in cc and cc[c] is None) or (order is not None and c not in order)]


def _token(df: pd.DataFrame, cols: list, reset) -> str:
    """データ元の識別。index（to_csv が書く）も入れる。"""
    return hashlib.sha256(f"{df.shape}|{cols}|{reset!r}|{df[cols].to_csv()}".encode()).hexdigest()


def _replay(df: pd.DataFrame, snap: tuple, cols: list, column_config: dict) -> pd.DataFrame:
    """前の言語での結果 R（行 i の元位置 = origins[i]・None は足した行）の編集可能列を、新しい df に重ねる。"""
    R, origins = snap
    keep = [i for i, o in enumerate(origins) if o is not None]
    base = df.iloc[[origins[i] for i in keep]].copy()
    for c in cols:
        base[c] = R[c].iloc[keep].set_axis(base.index)
    added = R.iloc[[i for i, o in enumerate(origins) if o is None]]
    if len(added):   # 足した行は末尾（Streamlit も 削除 → 追加 の順で末尾に足す）
        base = pd.concat([base, added.reindex(columns=df.columns)])
    if isinstance(df.index, pd.RangeIndex):   # 行を消した後も RangeIndex（でないと動的表で index 列が必須表示になる）
        base = base.reset_index(drop=True)
    # 空の列（全部 None）× 日付系の列の編集は ISO 文字列で返る。そのまま data に入れると文字列列になり
    # data_editor が「date 列に文字列」で例外 → 日付に戻す
    for c, v in column_config.items():
        kind = isinstance(v, dict) and (v.get("type_config") or {}).get("type")
        if kind in ("date", "datetime", "time") and c in base.columns and base[c].map(type).eq(str).any():
            parsed = pd.to_datetime(base[c], errors="coerce")
            base[c] = parsed if kind == "datetime" else (
                parsed.dt.date if kind == "date" else parsed.dt.time).astype(object).where(parsed.notna(), None)
    return base


def lang_stable_editor(data: pd.DataFrame, *, key: str, reset=None, **kwargs) -> pd.DataFrame:
    """st.data_editor(data, key=key, **kwargs) と同じ戻り値。言語切替をまたいで編集を保つ。

    reset: 行の識別値（言語で変わらないもの・タプルなど）。既定の判定（形・index・編集列・非表示列）に足す。
    """
    lang = i18n.get_lang()
    cols = _editable(data, kwargs)
    token = _token(data, list(dict.fromkeys([*cols, *_hidden(data, kwargs)])), reset)
    ctx = get_script_run_ctx()   # 同じ key の表が別ページにもある（wl_editor）→ ページごとに持つ
    sk = f"{_SK}{ctx.page_script_hash if ctx else ''}:{key}"
    s = st.session_state.get(sk)
    if s is None or s["token"] != token:
        s = {"token": token, "lang": lang, "snap": None, "last": None}
    elif s["lang"] != lang:
        s["lang"], s["snap"] = lang, s["last"]
    # ponytail: 同じページ内で表を if で隠して同じデータで戻すと累計編集が戻る（ページ移動は forget_all で捨てる）
    if s["snap"] is not None:
        base, origins = _replay(data, s["snap"], cols, kwargs.get("column_config") or {}), list(s["snap"][1])
    else:
        base, origins = data, list(range(len(data)))
    out = st.data_editor(base, key=key, **kwargs)
    ed = st.session_state.get(key) or {}
    gone = set(ed.get("deleted_rows") or ())
    origins = [o for i, o in enumerate(origins) if i not in gone] + [None] * len(ed.get("added_rows") or ())
    s["last"] = (out.copy(), origins)
    st.session_state[sk] = s
    return out
