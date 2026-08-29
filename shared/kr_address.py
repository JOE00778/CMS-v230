"""韩国地址 → ECMS 的 省(Province) / 市(City) / 详细地址 三段切分。

**为什么不用 AI 翻译**：xlsx「地址分析 ecms」里 AI 译文出过明显错误
（「경기도 수원시」→「京畿道京畿道水原市」、「부산광역시」→「釜山广域市釜山广域市」，
还有一条把「（注：因原文韩语地名…）」这类解释文本写进了地址栏）。
韩国行政区是**封闭集合**——一级 17 个、二级 264 个，前缀匹配即可，不需要 NLP。

**数据源**：행정안전부 법정동코드 전체자료（code.go.kr，2026-03-01 판）→
`kr_admin_divisions.json`（只抽两级，4KB；全表 50,100 行不进仓库）。

**切分口径**取自运营已在用的做法（xlsx「地址分析 ecms」B/C/D 列）：
    省   = 시도
    市   = 「시도 + 시」（광역시/특별시/세종은 시도 그 자체）
    详细 = 구/군 以下全部
実測: 311 件の実注文住所で三段とも取れたのが 311/311、
運営の手作業結果と完全一致 15/16（残り 1 件は運営が略称「대구」のまま、こちらは正式名に正規化）。

**切れなかったら None を返す**——推測で埋めない。page 側で赤表示して人が直す。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_DATA = json.loads((Path(__file__).parent / "kr_admin_divisions.json").read_text(encoding="utf-8"))
SIDO: list[str] = sorted(_DATA["sido"], key=len, reverse=True)
SIGUNGU: dict[str, list[str]] = _DATA["sigungu"]

# 口语简称 → 正式名。Coupang の住所欄は自由入力なので略称が実際に来る（実測「대구 달서구」）。
ALIAS: dict[str, str] = {}
for _full, _short in (("서울특별시", "서울"), ("부산광역시", "부산"), ("대구광역시", "대구"),
                      ("인천광역시", "인천"), ("광주광역시", "광주"), ("대전광역시", "대전"),
                      ("울산광역시", "울산"), ("세종특별자치시", "세종")):
    ALIAS[_short] = _full
    ALIAS[_short + "시"] = _full
ALIAS.update({
    "세종특별시": "세종특별자치시", "강원도": "강원특별자치도", "강원": "강원특별자치도",
    "전라북도": "전북특별자치도", "전북": "전북특별자치도",
    "제주도": "제주특별자치도", "제주": "제주특별자치도", "경기": "경기도",
    "충북": "충청북도", "충남": "충청남도", "전남": "전라남도",
    "경북": "경상북도", "경남": "경상남도",
})
_ALIAS_KEYS = sorted(ALIAS, key=len, reverse=True)


def split(addr: str) -> tuple[str | None, str | None, str, str]:
    """→ (시도, 시군구, 残り, how)。시도 が判らなければ (None, None, 原文, 'no_sido')。

    how は 'exact' / 'alias:<略称>' / 'no_sido' / 'no_sgg'（세종は시군구が無いので正常系）。
    """
    s = re.sub(r"\s+", " ", (addr or "").strip())
    if not s:
        return None, None, "", "no_sido"

    how = "exact"
    hit = next((k for k in SIDO if s.startswith(k)), None)
    if not hit:
        a = next((k for k in _ALIAS_KEYS if s.startswith(k)), None)
        if not a:
            return None, None, s, "no_sido"
        hit, how = ALIAS[a], f"alias:{a}"
        s = hit + s[len(a):]

    rest = s[len(hit):].strip()
    sgg = next((g for g in SIGUNGU.get(hit, []) if rest.startswith(g)), None)
    if not sgg:
        # 세종특별자치시は単層制で시군구が無い。それ以外は書式崩れ。
        return hit, None, rest, how if hit == "세종특별자치시" else "no_sgg"
    return hit, sgg, rest[len(sgg):].strip(), how


def to_ecms(addr: str) -> dict:
    """ECMS の Consignee Province / City / Address 三段に整形。

    切れなければ province/city が None のまま返る（`ok` が False）。埋めない。
    """
    sido, sgg, detail, how = split(addr)
    if not sido:
        return {"province": None, "city": None, "address": detail, "how": how, "ok": False}

    if sgg is None:                                    # 세종특별자치시（시군구なし）
        province, city = sido, sido
    else:
        parts = sgg.split(" ")
        if len(parts) == 2:                            # 「성남시 분당구」→ 市は시、구は詳細へ
            province, city, detail = sido, f"{sido} {parts[0]}", f"{parts[1]} {detail}".strip()
        elif parts[0].endswith(("구", "군")):           # 광역시 아래의 구/군 → 詳細へ
            province, city, detail = sido, sido, f"{parts[0]} {detail}".strip()
        else:                                          # 도 아래의 시
            province, city = sido, f"{sido} {parts[0]}"

    return {"province": province, "city": city, "address": detail, "how": how,
            "ok": bool(province and city and detail)}
