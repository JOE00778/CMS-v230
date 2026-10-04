"""商品登録 v2 の HS を AI（Gemini）でもう一度判定する。規則（hs_classify）との突合用。

Boss 2026-10-03「CMS 能加吗」→ 規則 + AI の二重判定。一致は確定、不一致は AI の値を入れて警告。
モデル選定の実測（2026-10-04 · CMS 容器の既存 key · 商品名だけで判定）:
  米 CBP 裁定 69 件  規則 34 / gemini-3.6-flash 55（80%）/ 参考 Claude Opus 57（83%）
    規則と AI が一致した 34 件の正解 32 · 不一致 35 件で AI 正解 23・規則正解 2
  日本 税関事前教示 70 件 gemini 44（63%）/ Opus 51（73%）
AI の返すコードは 2026 輸出統計品目表に在る 6 桁だけ採る（無いコードは失敗扱い・規則の値を残す）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
MODEL = "gemini-3.6-flash"
BATCH = 25
TIMEOUT = 180

SYSTEM = (
    "あなたは日本からの越境EC輸出品の HS コード（HS2022、6桁）を判定する通関担当者です。\n"
    "会社の帰類原則（必ず守る）:\n"
    "1. 洗浄類（ボディソープ・洗顔・ハンドソープ）は 3401 に置き 3304 に入れない\n"
    "2. 30/33 章（米 FDA 管轄の医薬品・化粧品）を避けられる正当な帰類があるなら避ける"
    "（湿布 3401.19、便器洗浄 3402.90、虫よけ 3808.91 等）。避けられないもの（制汗剤 3307.20・乳液 3304.99 等）は 33 章のまま\n"
    '出力は JSON 配列のみ。各要素 {"id":..., "hs6":"6桁数字", "conf":0〜1の自信度, '
    '"en":"英語の品名(2-5語)", "why":"判定理由(日本語30字以内)"}。それ以外は書かない。'
)

with open(Path(__file__).parent / "data" / "hs6_2026.txt", encoding="utf-8") as _f:
    VALID_HS6 = frozenset(x.strip() for x in _f if x.strip() and not x.startswith("#"))


@dataclass
class AiHs:
    hs: str
    en: str
    why: str
    conf: float


def is_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def _post(body: dict) -> str:
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "User-Agent": "cms-item-register",
        "Authorization": "Bearer " + os.environ["GEMINI_API_KEY"].strip()})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)["choices"][0]["message"]["content"] or ""


def _prompt(batch: list[tuple[str, str, str]]) -> str:
    lines = [f"id={i} | 日本語商品名: {name} | メーカー: {maker or '不明'}"
             for i, (_, name, maker) in enumerate(batch)]
    return "次の商品の HS6 を判定してください。\n" + "\n".join(lines)


def judge(items: list[tuple[str, str, str]], *, post=_post, sleep=time.sleep) -> dict:
    """[(jan, 商品名, メーカー)] → {jan: AiHs | str(失敗理由)}。1 バッチ失敗でも他は続ける。"""
    out: dict = {}
    for k in range(0, len(items), BATCH):
        batch = items[k:k + BATCH]
        body = {"model": MODEL, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": _prompt(batch)}]}
        err, got = "", None
        for attempt in range(3):
            try:
                txt = post(body)
                m = re.search(r"\[.*\]", txt, re.S)
                got = {str(x.get("id")): x for x in json.loads(m.group(0))} if m else None
                if got is None:
                    err = "AI の応答に JSON がない"
                break
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                err = f"{type(e).__name__}: {str(e)[:80]}"
                sleep(10 * (attempt + 1))           # 503/429 が実測で出る
        for i, (jan, _, _) in enumerate(batch):
            x = (got or {}).get(str(i))
            if x is None:
                out[jan] = err or "AI の応答にこの商品がない"
                continue
            hs = re.sub(r"\D", "", str(x.get("hs6", "")))[:6]
            if hs not in VALID_HS6:
                out[jan] = f"AI のコード {hs or '空'} は 2026 輸出統計品目表に無い"
                continue
            out[jan] = AiHs(hs, str(x.get("en") or "").strip(), str(x.get("why") or "").strip(),
                            float(x.get("conf") or 0))
    return out
