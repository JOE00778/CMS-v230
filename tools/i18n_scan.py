"""UI 文言の 中文 / 日文 対応の静的検査（Boss 2026-11-12「CMS全部一起做吧」）.

規則（tests/test_i18n_coverage.py が 0 件を要求）:
  no_zh  t("…かな…")            → TRANSLATIONS_ZH に訳が要る（中文モードで日本語が出る）
  no_ja  t("漢字だけ")           → TRANSLATIONS_JA か TRANSLATIONS_ZH のどちらかに登録（中文原文 → JA / 日本語の漢字語 → ZH）
  fstr   t(f"…{x}…")             → 辞書に当たらない。t("…{x}…").format(x=…) に書き換える
  raw    st.xxx("日本語/中文")    → t() を通っていない表示
  ph     テンプレの {名前} が訳と原文で食い違う（.format が KeyError / 値が消える）

使い方:
  uv run python tools/i18n_scan.py pages/15_📝_商品登录.py ...          # 欠けの一覧（行番号つき）
  uv run python tools/i18n_scan.py --extra extra.json pages/*.py        # 未マージの訳 JSON {"ja": {}, "zh": {}} も足して判定
  uv run python tools/i18n_scan.py --summary pages/*.py shared/*.py
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
CJK = re.compile(r"[一-鿿ぁ-ゖァ-ヺ]")
ST_UI = {"title", "header", "subheader", "caption", "info", "warning", "error", "success", "button", "download_button",
         "checkbox", "radio", "selectbox", "multiselect", "expander", "progress", "spinner", "markdown", "write",
         "metric", "text_input", "text_area", "number_input", "date_input", "file_uploader", "toggle", "toast",
         "page_link", "form_submit_button", "link_button", "slider", "select_slider", "tabs", "set_page_config"}


def _fields(s: str) -> set[str]:
    try:
        return {f for _, f, _, _ in string.Formatter().parse(s) if f is not None}
    except ValueError:
        return {"<壊れた書式>"}


def scan(path: str, ja: dict, zh: dict) -> dict[str, list[tuple[int, str]]]:
    src = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, list[tuple[int, str]]] = {k: [] for k in ("ok", "no_zh", "no_ja", "fstr", "raw", "ph")}
    wrapped: set[int] = set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t" and n.args):
            continue
        a = n.args[0]
        wrapped.add(id(a))
        if isinstance(a, ast.JoinedStr):
            seg = ast.get_source_segment(src, a) or ""
            if CJK.search(seg):
                out["fstr"].append((n.lineno, seg))
            continue
        if not (isinstance(a, ast.Constant) and isinstance(a.value, str)):
            continue                                   # 変数・式は静的に見られない
        s = a.value
        if not CJK.search(s):
            out["ok"].append((n.lineno, s))
            continue
        if KANA.search(s):
            hit = s in zh
            out["ok" if hit else "no_zh"].append((n.lineno, s))
        else:
            hit = s in ja or s in zh
            out["ok" if hit else "no_ja"].append((n.lineno, s))
        for d in (ja, zh):
            if s in d and _fields(s) != _fields(d[s]):
                out["ph"].append((n.lineno, f"{s!r} → {d[s]!r}"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ST_UI:
            for a in [*n.args[:1], *[k.value for k in n.keywords if k.arg in ("label", "page_title", "text", "body")]]:
                if id(a) in wrapped or not isinstance(a, (ast.Constant, ast.JoinedStr)):
                    continue
                seg = ast.get_source_segment(src, a) or ""
                if CJK.search(seg):
                    out["raw"].append((n.lineno, seg))
    return out


def dictionaries(extra: list[str]) -> tuple[dict, dict]:
    from shared.i18n import TRANSLATIONS_JA, TRANSLATIONS_ZH
    ja, zh = dict(TRANSLATIONS_JA), dict(TRANSLATIONS_ZH)
    for p in extra:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        ja.update(d.get("ja", {}))
        zh.update(d.get("zh", {}))
    return ja, zh


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--extra", action="append", default=[], help="未マージの訳 JSON（複数可）")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    ja, zh = dictionaries(args.extra)
    bad = 0
    for f in args.files:
        r = scan(f, ja, zh)
        n = sum(len(v) for k, v in r.items() if k != "ok")
        bad += n
        if args.summary:
            if n:
                print(f"{n:5d}  " + " ".join(f"{k}={len(v)}" for k, v in r.items() if k != "ok" and v) + f"  {f}")
            continue
        if n:
            print(f"== {f}")
            for k, v in r.items():
                if k != "ok":
                    for line, s in v:
                        print(f"  {k:6s} L{line}: {s[:120]}")
    print(f"TOTAL gaps={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
