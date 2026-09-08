"""PCCC（개인통관고유부호）を韓国関税庁の実データに照らして検証する。

Boss 2026-09-04 拍板「PCCC 走1」——自前で UNIPASS の企業アカウントを取るのではなく、
**GSI Express（米韓の国際特送業者）が自社サイトで無料公開している検証ツール**を使う。
あちらが取得済みの UNIPASS API を、ログイン不要のフォームとして開放しているもの。

  POST https://www.gsiexpress.com/pcc_chk.php
    action_type=query
    chk_data=이름/통관고유부호/핸드폰번호/우편번호   （1 行 1 件・複数行可）
  → 検証結果テーブルを含む HTML が返る

実測（2026-09-04・ダミーデータ）:
  P123412341234 → 오류「납세의무자 개인통관고유부호가 존재しない」
  つまり**形式だけでなく実在確認まで効いている**（関税庁に問い合わせている証拠）。

結果は 3 種:
  정상  4 点すべてが関税庁の登録内容と一致
  오류  一致しない（**番号が無効とは限らない**——下記）
  장애  関税庁サーバ側の障害・通信障害（**再試行対象**。NG と混同しない）

⚠️ **오류 ≠ PCCC が無効**（2026-09-08 運営指摘「我人工核后确认实际信息是正确的，
   但是这个功能显示是错误的」）。関税庁は 부호 + 氏名 + 電話 + **登録済み配送地の
   郵便番号**の 4 点を照合する。引っ越し・番号変更のあと関税庁側を更新していない、
   あるいは職場や家族の住所に送っている——それだけで 오류 になるが、番号自体は
   生きていて通関もできる（特送業者が受荷主情報を確認・修正して申告する運用）。
   出典: 한국관세무역개발원「등록된 우편번호까지 일치해야 통관 가능」
   だから**この結果で PCCC を自動的に消してはいけない**。画面に理由を出して、
   運営が見て判断する。以前は 오류 を自動削除していた（`apply_results`）——
   正しい番号まで捨てていたので 2026-09-08 に撤去した。

⚠️ ponytail: 他社の無料ツールに乗っている。API 契約ではないので、先方の都合で
   いつ止まっても・仕様が変わってもおかしくない。**落ちても業務を止めない**設計に
   してある（例外は投げず fault を返す）。恒久運用にするなら自前で UNIPASS の
   企業アカウントを取るのが本筋——判断は Boss に上げ済み。

⚠️ 顧客の氏名・電話・郵便番号・PCCC を社外サイトへ送る。Boss 承認済み（2026-09-04）。
   ここを別用途に流用しないこと。
"""
from __future__ import annotations

import re
import time
from html import unescape

import requests

ENDPOINT = "https://www.gsiexpress.com/pcc_chk.php"
TIMEOUT = 60
CHUNK = 20            # 1 リクエストあたりの件数。相手は無料ツールなので欲張らない
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# 「정상」以外はすべて NG 扱いだが、장애 だけは相手側の一時障害なので区別する
STATUS_OK = "ok"
STATUS_NG = "ng"
STATUS_FAULT = "fault"


def _text(html: str) -> str:
    return unescape(_TAG_RE.sub("", html)).strip()


def _parse(html: str) -> list[dict]:
    """検証結果テーブル → [{name, pccc, phone, zip, status, result, message}]。"""
    out = []
    for m in re.finditer(r"<tr>.*?</tr>", html, re.S):
        tds = [_text(x) for x in _TD_RE.findall(m.group(0))]
        if len(tds) != 6:
            continue                      # ヘッダ行や「사용방법」の説明表は td 数が違う
        name, pccc, phone, zipcode, result, message = tds
        status = {"정상": STATUS_OK, "장애": STATUS_FAULT, "오류": STATUS_NG}.get(result)
        if status is None:
            continue                      # 説明表を拾わないための保険
        out.append({"name": name, "pccc": pccc, "phone": phone, "zip": zipcode,
                    "status": status, "result": result, "message": message})
    return out


def check(rows: list[dict], chunk: int = CHUNK) -> tuple[list[dict], str]:
    """[{name, pccc, phone, zip}] を検証する。戻り値 (結果一覧, エラー文字列)。

    **例外を投げない**。相手が落ちていたら全件 fault にして理由を返すだけ——
    他社の無料ツールなので、止まったときに ECMS 出力まで止めてはいけない。
    """
    targets = [r for r in rows if (r.get("pccc") or "").strip()]
    if not targets:
        return [], ""

    results: list[dict] = []
    errors: list[str] = []
    for i in range(0, len(targets), max(1, chunk)):
        batch = targets[i:i + max(1, chunk)]
        payload = "\n".join(
            "/".join([(r.get("name") or "").strip(), (r.get("pccc") or "").strip(),
                      (r.get("phone") or "").strip(), str(r.get("zip") or "").strip()])
            for r in batch)
        try:
            resp = requests.post(
                ENDPOINT, data={"action_type": "query", "chk_data": payload},
                headers={"User-Agent": UA}, timeout=TIMEOUT)
            resp.encoding = resp.encoding or "utf-8"
            parsed = _parse(resp.text)
        except requests.RequestException as e:
            parsed = []
            errors.append(f"{type(e).__name__}: {e}")
        except Exception as e:                       # 解析側が壊れても業務は止めない
            parsed = []
            errors.append(f"解析失敗: {e}")

        got = {p["pccc"].strip().upper() for p in parsed}
        if len(parsed) != len(batch):
            # 件数が合わない＝相手の HTML が変わった可能性。分かる分だけ使い、残りは fault
            for r in batch:
                if (r.get("pccc") or "").strip().upper() not in got:
                    parsed.append({"name": r.get("name", ""), "pccc": r.get("pccc", ""),
                                   "phone": r.get("phone", ""), "zip": r.get("zip", ""),
                                   "status": STATUS_FAULT, "result": "장애",
                                   "message": "検証ツールから結果が返らなかった"})
            if not errors:
                errors.append(f"返却件数が不一致（{len(batch)} 件送って {len(got)} 件）")
        results.extend(parsed)
        if i + chunk < len(targets):
            time.sleep(1)                            # 無料ツールへの配慮
    return results, "；".join(errors)


def status_map(results: list[dict]) -> dict[str, dict]:
    """PCCC → 結果。同じ PCCC が複数注文にまたがっても 1 回引けば足りる。"""
    return {r["pccc"].strip().upper(): r for r in results if r.get("pccc")}
