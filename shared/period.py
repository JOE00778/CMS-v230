"""月次ページ共通の「単月 / 期間」切替ウィジェット（川崎さん 2026-07-24 依頼）。

「年月で絞るところは全て期間で絞れるようにしたい」に対して、既存の単月
selectbox を**置き換えず**、その隣に切替を足す方針（Boss 2026-08-18「单独加一个
区间选择」）。単月しか使わない人の操作は 1 つも変わらない。

戻り値は常に (from_ym, to_ym) の両端含む閉区間。単月モードでは両方同じ値。
`year_month` は 'YYYY-MM' のゼロ埋め固定長なので文字列比較がそのまま日付順に
なる（`BETWEEN from AND to` / `>= <=` がそのまま使える）。
"""
from __future__ import annotations

import streamlit as st

from shared.i18n import get_lang


def _lbl(zh: str, ja: str) -> str:
    return ja if get_lang() == "ja" else zh


def month_range_selector(
    months: list[str], *, key: str,
    label: str | None = None,
    index: int = 0,
    all_label: str | None = None,
    container=None,
) -> tuple[str, str]:
    """単月/期間 切替つき月選択。

    months: 'YYYY-MM' の降順リスト（先頭が最新）。空なら ("", "") を返す。
    index:  単月モードの既定位置（page09 のように既定を「先月」にしたい時に使う）。
            all_label を渡した場合は「全部」を 0 番目に足した後の位置で数える。
    all_label: 「全部」相当の選択肢を単月モードにだけ足す（page09）。選ばれたら
            ("", "") を返す＝月で絞らない、の意味。期間モードには出さない。
    container: st / st.columns(...)[i] など描画先。省略時は st。

    returns: (from_ym, to_ym) 両端含む。("", "") は月フィルタなし。
    """
    _c = container if container is not None else st
    if not months:
        return "", ""

    _single = _lbl("单月", "単月")
    _range = _lbl("期间", "期間")
    mode = _c.radio(
        _lbl("筛选方式", "絞り込み"), [_single, _range],
        index=0, horizontal=True, key=f"{key}_mode",
    )
    if mode == _single:
        _opts = ([all_label] + months) if all_label else months
        _idx = index if 0 <= index < len(_opts) else 0
        ym = _c.selectbox(label or _lbl("対象月", "対象月"), _opts,
                          index=_idx, key=f"{key}_one")
        return ("", "") if (all_label and ym == all_label) else (ym, ym)

    # 期間モード: 開始 ≤ 終了 を UI 側で保証（逆順に選ばれたら黙って入れ替える）
    _asc = list(reversed(months))
    c1, c2 = _c.columns(2)
    _from = c1.selectbox(_lbl("开始月", "開始月"), _asc,
                         index=max(0, len(_asc) - 3), key=f"{key}_from")
    _to = c2.selectbox(_lbl("结束月", "終了月"), _asc,
                       index=len(_asc) - 1, key=f"{key}_to")
    if _from > _to:
        _from, _to = _to, _from
    return _from, _to


def range_caption(from_ym: str, to_ym: str) -> str:
    """表やグラフの下に出す「今どの範囲を見ているか」の 1 行。"""
    if not from_ym:
        return ""
    if from_ym == to_ym:
        return from_ym
    _n = _month_count(from_ym, to_ym)
    return f"{from_ym} 〜 {to_ym}" + _lbl(f"（共 {_n} 个月合计）", f"（{_n} ヶ月の合計）")


def _month_count(from_ym: str, to_ym: str) -> int:
    """両端含む月数。'2026-01','2026-03' → 3。"""
    fy, fm = int(from_ym[:4]), int(from_ym[5:7])
    ty, tm = int(to_ym[:4]), int(to_ym[5:7])
    return (ty - fy) * 12 + (tm - fm) + 1


def prev_range(from_ym: str, to_ym: str) -> tuple[str, str]:
    """同じ長さの直前の期間（前年比ではなく「前期比」）。

    2026-06〜2026-08（3 ヶ月）→ 2026-03〜2026-05。単月なら前月。
    """
    _n = _month_count(from_ym, to_ym)
    return _shift(from_ym, -_n), _shift(to_ym, -_n)


def _shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    _t = y * 12 + (m - 1) + months
    return f"{_t // 12:04d}-{_t % 12 + 1:02d}"
