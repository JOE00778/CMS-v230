"""UI 文言の 中文 / 日文 対応を全ページで保つ（Boss 2026-11-12「CMS全部一起做吧」）。

tools/i18n_scan.py の規則で欠けが 0 件であること:
  日文原文の t() に中文訳 / 漢字だけの t() は JA か ZH に登録 / t(f"…") を使わない（辞書に当たらない）/
  st.xxx("中日文") を t() なしで出さない / テンプレの {名前} が訳と一致。
新しい文言を足したら shared/i18n.py の TRANSLATIONS_JA（中文原文→日文）/ TRANSLATIONS_ZH（日文原文→中文）にも足す。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import i18n_scan  # noqa: E402

FILES = sorted([ROOT / "cms.py", *(ROOT / "pages").glob("*.py"), *(ROOT / "shared").glob("*.py")])


def test_ui_text_has_both_languages():
    ja, zh = i18n_scan.dictionaries([])
    gaps = []
    for f in FILES:
        r = i18n_scan.scan(str(f), ja, zh)
        for kind, items in r.items():
            if kind != "ok":
                gaps += [f"{f.relative_to(ROOT)}:{line} {kind} {s[:80]}" for line, s in items]
    assert not gaps, f"{len(gaps)} 件:\n" + "\n".join(gaps[:40])
