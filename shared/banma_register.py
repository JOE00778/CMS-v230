"""page15 商品登録 → 斑马に API で新規登録（Boss 2026-11-09「生成 NST CSV，同时通过 API 写入斑马」）.

1 件ずつ: Code で照会（あれば skip）→ POST /v1/product/ → 報関 POST /v1/product/sku/{SKUID}/customs
→ Code で回読して原価・重量を照合。結果は record(code, dict) で台帳へ（呼び出し側が書く）。

値の口径は従来の 斑马 xlsx（jd_bm_item_master.nst_to_bm_row）と同じ:
  SPU = SKU = 型番 / 产品标题 = 中文名称 = アイテム名 / 规格 1 = 型番 / 成本价 = 商品原価（円の数値そのまま。
  2026-11-09 実測: page15 経由で xlsx 取込した既存品は BM 成本价 = NST 原価（900=900）。文書は CNY と書くが従う）
  重量・長宽高 = パッケージ値 / 海关编码 = _hs / 英文名称 = _name_en / ERP 类目・供应商は空。
POST は非冪等 → 再送しない（banma_client が GET 以外を 1 回に固定）。応答が読めなければ Code で照会して判定。
"""
from __future__ import annotations

import math

from shared.item_register import COL_HS, COL_IMAGE, COL_NAME, COL_NAME_EN, PLACEHOLDER_IMAGE_URL, item_key

SKU_PATH = "/v1/product/sku"


def _int_up(v, default: int = 1) -> int:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return max(1, math.ceil(n)) if math.isfinite(n) and n > 0 else default


def product_body(row: dict) -> dict:
    code = item_key(row)
    image = row.get(COL_IMAGE) or PLACEHOLDER_IMAGE_URL
    return {
        "Spu": {"Code": code, "Title": row[COL_NAME], "Image": image, "Source": "手工创建"},
        "Skus": [{
            "Code": code, "Specification": code, "CostPrice": float(row.get("商品原価") or 0),
            "Image": image, "Weight": _int_up(row.get("パッケージ重量(g)")),
            "Length": _int_up(row.get("パッケージ奥行(cm)")), "Width": _int_up(row.get("パッケージ幅(cm)")),
            "Height": _int_up(row.get("パッケージ高さ(cm)")),
            "IsValid": True, "Sort": 0, "Type": "普通", "Options": [{"Name": "1", "Value": code}],
        }],
        "Options": [{"Name": "1", "Sort": 0, "Values": [code]}],
        "Images": [{"Src": image, "Sort": 0}],
    }


def customs_body(row: dict) -> dict | None:
    hs, en = str(row.get(COL_HS) or "").strip(), str(row.get(COL_NAME_EN) or "").strip()
    if not hs or not en:      # NameCn・NameEn・Code は必須（2026-09-19 実測）
        return None
    return {"Code": hs, "NameCn": row[COL_NAME], "NameEn": en,
            "Weight": _int_up(row.get("パッケージ重量(g)"))}


def find_sku(client, code: str) -> dict | None:
    skus = (client.call("GET", SKU_PATH, {"Code": code, "PageNumber": 1, "PageSize": 50}) or {}).get("SKUs") or []
    return next((s for s in skus if s.get("Code") == code), None)


def get_skus(client, codes: list[str]) -> dict[str, dict]:
    """Code（型番 / JAN）→ 斑马 SKU（SKUID・Image・Weight・Length/Width/Height・Type 等。SPUID は無い）。
    50 件ずつ照会（PageSize 上限 50・Code はカンマ区切り）。完全一致だけ採る。"""
    out: dict[str, dict] = {}
    uniq = sorted({c for c in codes if c})
    for i in range(0, len(uniq), 50):
        chunk = uniq[i:i + 50]
        page = 1
        while True:
            skus = (client.call("GET", SKU_PATH, {"Code": ",".join(chunk), "PageNumber": page, "PageSize": 50})
                    or {}).get("SKUs") or []
            out.update({s["Code"]: s for s in skus if s.get("Code") in chunk})
            if len(skus) < 50:
                break
            page += 1
    return out


def find_skus(client, codes: list[str]) -> dict[str, str]:
    """Code（型番）→ 斑马 SKUID。"""
    return {c: str(s.get("SKUID") or "") for c, s in get_skus(client, codes).items()}


def register_one(client, row: dict) -> dict:
    """→ {"status": ok|partial|skipped|fail, "sku_id", "spu_id", "message"}。例外は投げない。"""
    code = item_key(row)
    try:
        hit = find_sku(client, code)
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "message": f"斑马照会に失敗（登録していません）: {type(e).__name__}: {e}"}
    if hit:
        return {"status": "skipped", "sku_id": str(hit.get("SKUID") or ""), "spu_id": str(hit.get("SPUID") or ""),
                "message": "斑马に同じ SKU コードが既にあります（登録していません）"}
    body = product_body(row)
    post_err = ""
    try:
        client.call("POST", "/v1/product/", body=body)
    except Exception as e:  # noqa: BLE001 — 応答が読めなかっただけで作られている可能性 → 照会で判定
        post_err = f"{type(e).__name__}: {e}"
    try:
        hit = find_sku(client, code)
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "message": f"登録後の照会に失敗（斑马で {code} を確認してください）: "
                                             f"{type(e).__name__}: {e}" + (f" / 登録: {post_err}" if post_err else "")}
    if not hit:
        return {"status": "fail", "message": f"斑马に登録されていません: {post_err or '応答は成功・照会で見つからず'}"}
    out = {"status": "ok", "sku_id": str(hit.get("SKUID") or ""), "spu_id": str(hit.get("SPUID") or ""),
           "message": ""}
    notes = []
    sku = body["Skus"][0]
    for k in ("CostPrice", "Weight"):
        if float(hit.get(k) or 0) != float(sku[k]):
            notes.append(f"回読 {k}={hit.get(k)}（送信 {sku[k]}）")
    cb = customs_body(row)
    if cb is None:
        notes.append("報関情報なし（HS か通関英文名が空）")
    else:
        try:
            client.call("POST", f"{SKU_PATH}/{out['sku_id']}/customs", body=cb)
        except Exception as e:  # noqa: BLE001
            notes.append(f"報関情報の書込に失敗: {type(e).__name__}: {e}")
    if notes:
        out.update(status="partial", message=" · ".join(notes))
    return out


def register_all(client, rows: list[dict], record=None, on_progress=None) -> list[tuple[str, dict]]:
    """1 件ずつ登録。record(code, result) は各件の直後に呼ぶ（台帳を逐次書く）。"""
    out = []
    for i, row in enumerate(rows):
        res = register_one(client, row)
        if record:
            try:
                record(item_key(row), res)
            except Exception as e:  # noqa: BLE001 — 台帳失敗は画面に出す。登録結果は失わない
                res["message"] = (res.get("message", "") + f" · 台帳更新失敗: {type(e).__name__}: {e}").strip(" ·")
        out.append((item_key(row), res))
        if on_progress:
            on_progress(i + 1, len(rows), item_key(row))
    return out
