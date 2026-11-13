"""page15「🩺 斑马登録内容チェック」（Boss 2026-11-12）: 斑马 SKU の 三辺・重量・画像・報関（HS・英文名）の空を出し、
CMS で直した値を斑马へ書き戻す。

書き方は 2 系統:
- 報関（HS・英文名）: PUT /v1/product/sku/{SKUID}/customs（現値の全項目を回送して対象だけ差し替え・無ければ POST で作る）。
  database 仓 banma_api/write_customs.py（2026-09-19 一括 HS で実証）と同じ形。
- 三辺・重量・画像: 斑马に SKU 単位の更新 API は無い → PUT /v1/product/{SPUID} で SPU 丸ごと（write_sku_dims.py と同じ形）。
  ⚠️ この PUT は描述（Descriptions）を消し、戻す API も無い（2026-10-07 実測）。包材（PackMaterials）は PUT の body に無い。
  → 描述か包材がある SPU には書かない（三辺・重量は「批量修改SKU信息」Excel、画像は斑马の画面で）。
  画像が 1 つも無い SPU の PUT は 400 で拒まれる（2026-11-13）→ 新規登録と同じ「待上図」画像を入れて書く。
  2026-11-12 抽出: 直近 90 日の NST 品の SPU 30 件中 描述あり 1・画像なし 26。
どちらも: 書く前に GET → 台帳 banma.cms_write_log に before を commit（できなければ書かない）→ 書く（非冪等・再送しない）
→ GET し直し、対象以外が 1 つも変わっていないことを確認。違えば mismatch（呼び出し側は以降の SPU 書込を止める）。
空の判定は page46（shared/banma_dims）と同じ仮値の口径（Boss 2026-10-07〜09 拍板）。
"""
from __future__ import annotations

import copy
import io
import json
import re

from shared.banma_dims import D_PH, W_PH
from shared.i18n import t
from shared.item_register import PLACEHOLDER_IMAGE_URL

SKU_PATH = "/v1/product/sku"
PRODUCT_PATH = "/v1/product"
CHECKS = ("三辺", "重量", "画像", "英文名", "HS")
DIMS = ("Length", "Width", "Height")
PRODUCT_FIELDS = ("Weight", *DIMS, "Image")          # SPU 丸ごと PUT で書く SKU 項目
SPU_KEYS = ("SPUID", "Code", "Title", "LeiMuID", "Image", "Source", "DeveloperID",
            "DefaultSupplierID", "Remark", "IsExemptQuality")
SKU_KEYS = ("SKUID", "Code", "Specification", "CostPrice", "Image", "Weight",
            *DIMS, "IsValid", "Remark", "Sort", "Type", "Options", "CombineData")
_HS = re.compile(r"\d{6}(\d{2}){0,2}")


# ───────────── 検査 ─────────────

def problems(sku: dict, customs: dict | None, *, checks, placeholder: bool) -> list[str]:
    """空（placeholder=True なら仮値も空）の項目名。customs None = 報関情報のレコード自体が無い。"""
    out = []
    w = sku.get("Weight")
    if "重量" in checks and (not w or (placeholder and w in W_PH)):
        out.append("重量")
    d = tuple(sku.get(k) for k in DIMS)
    if "三辺" in checks and (any(not x for x in d) or (placeholder and tuple(sorted(d, reverse=True)) in D_PH)):
        out.append("三辺")
    img = str(sku.get("Image") or "").strip()
    if "画像" in checks and (not img or (placeholder and img == PLACEHOLDER_IMAGE_URL)):
        out.append("画像")
    c = customs or {}
    if "英文名" in checks and str(c.get("NameEn") or "").strip() in ("", "-"):
        out.append("英文名")
    if "HS" in checks and not _HS.fullmatch(str(c.get("Code") or "").strip()):
        out.append("HS")
    return out


def _is_404(e: Exception) -> bool:
    return "404" in str(e)


def get_customs(client, sku_id: str) -> dict | None:
    """斑马の報関情報。レコード自体が無い（404）→ None。"""
    try:
        return (client.call("GET", f"{SKU_PATH}/{sku_id}/customs") or {}).get("Customs") or None
    except Exception as e:  # noqa: BLE001
        if _is_404(e):
            return None
        raise


def mirror_customs(conn, sku_ids: list[str], ranked: set[str] | None = None) -> dict[str, dict | None]:
    """PG banma.sku_customs（pull_sku_health 毎日 04:00）→ {SKUID: {Code, NameEn, NameCn} | None}。
    写しの範囲 = 斑马 Export 類目の有効 SKU で NST 等級 A/B/C/NEW（ranked = その SKUID）。範囲内で行が無い = 報関なし（None）。
    範囲外で行が無い SKU は返さない（「無い」とは限らない → 呼び出し側が API で見る）。"""
    out: dict[str, dict | None] = {}
    for i in range(0, len(sku_ids), 200):
        chunk = sku_ids[i:i + 200]
        ph = ",".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT sku_id, name_cn, name_en, hs_code FROM banma.sku_customs WHERE sku_id IN ({ph})",
                            tuple(chunk)).fetchall()
        out.update({str(r["sku_id"]): {"Code": r["hs_code"], "NameEn": r["name_en"], "NameCn": r["name_cn"]}
                    for r in rows})
        if ranked:
            for r in conn.execute(f"SELECT sku_id FROM banma.sku_dims WHERE sku_id IN ({ph}) AND is_valid "
                                  "AND category_top = 'Export'", tuple(chunk)).fetchall():
                if str(r["sku_id"]) in ranked:
                    out.setdefault(str(r["sku_id"]), None)
    return out


def _same(a, b) -> bool:
    return ("" if a is None else str(a).strip()) == ("" if b is None else str(b).strip())


def spu_ids(conn, sku_ids: list[str]) -> dict[str, str]:
    """SKUID → SPUID（斑马の SKU 照会には SPUID が無い）。PG banma.sku_dims（毎日 04:00）→ 無ければ page15 台帳。"""
    out: dict[str, str] = {}
    for i in range(0, len(sku_ids), 200):
        chunk = sku_ids[i:i + 200]
        ph = ",".join("?" for _ in chunk)
        for sql in (f"SELECT sku_id, spu_id FROM banma.sku_dims WHERE sku_id IN ({ph})",
                    f"SELECT banma_sku_id AS sku_id, banma_spu_id AS spu_id FROM nst.item_register_log "
                    f"WHERE banma_sku_id IN ({ph}) AND banma_spu_id IS NOT NULL"):
            for r in conn.execute(sql, tuple(chunk)).fetchall():
                if r["spu_id"]:
                    out.setdefault(str(r["sku_id"]), str(r["spu_id"]))
    return out


# ───────────── 台帳 ─────────────

def _j(x) -> str | None:
    return None if x is None else json.dumps(x, ensure_ascii=False, default=str)


def _log_start(conn, kind: str, target: str, codes: str, changes: dict, before) -> int:
    row = conn.execute("INSERT INTO banma.cms_write_log (kind, target_id, sku_codes, changes, before) "
                       "VALUES (?, ?, ?, ?::jsonb, ?::jsonb) RETURNING id",
                       (kind, target, codes, _j(changes), _j(before))).fetchone()
    conn.commit()
    return row["id"]


def _log_done(conn, log_id: int, status: str, after, message: str) -> None:
    try:
        conn.execute("UPDATE banma.cms_write_log SET status = ?, after = ?::jsonb, message = ? WHERE id = ?",
                     (status, _j(after), message or None, log_id))
        conn.commit()
    except Exception:  # noqa: BLE001 — 書込結果は画面に出す。台帳の後半だけの失敗で結果を失わない
        pass


# ───────────── 報関（HS・英文名）─────────────

_CUSTOMS_KEEP = ("NameCn", "Material", "Purpose", "Weight", "Price", "ImportPrice", "ExportPrice")


def customs_body(cur: dict, sku_id: str) -> dict:
    """現値を丸ごと載せる（write_customs.build_body と同じ・部分更新の意味が文書に無いため）。"""
    return {"SKUID": str(sku_id), "NameCn": cur.get("NameCn") or "", "NameEn": cur.get("NameEn") or "",
            "Code": cur.get("Code") or "", "Price": cur.get("Price", 0), "ImportPrice": cur.get("ImportPrice", 0),
            "ExportPrice": cur.get("ExportPrice", 0), "Weight": cur.get("Weight", 0),
            "SpecialProperty": cur.get("SpecialProperty") or [], "Material": cur.get("Material") or "",
            "Purpose": cur.get("Purpose") or ""}


def write_customs(client, conn, sku_id: str, code: str, new: dict, *, name_cn: str = "", weight=0,
                  expect: dict | None = None) -> dict:
    """new = {"Code": HS, "NameEn": 英文名} の変える分だけ。→ {"status": ok/created/skipped/fail/mismatch, "message"}。
    expect = チェック時に見た値。書く直前の斑马の値が違えば書かない（写しが古い・別の人が直した → 上書きしない）。"""
    new = {k: str(v).strip() for k, v in new.items() if k in ("Code", "NameEn") and str(v or "").strip()}
    if not new:
        return {"status": "skipped", "message": t("変更なし")}
    try:
        cur = get_customs(client, sku_id)
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "message": t("報関の照会に失敗（書いていません）: {err}").format(err=f"{type(e).__name__}: {e}")}
    if expect is not None:
        moved = [t("{k}: 今 {now} / チェック時 {was}").format(k=k, now=(cur or {}).get(k) or t("空"),
                                                              was=expect.get(k) or t("空")) for k in new
                 if not _same((cur or {}).get(k), expect.get(k))]
        if moved:
            return {"status": "skipped", "message": t("斑马の値がチェック時と違うため書いていません（{detail}）").format(
                detail=" · ".join(moved))}
    if cur is None:
        if not (new.get("Code") and new.get("NameEn")):
            return {"status": "skipped", "message": t("斑马に報関情報が無い SKU は HS と英文名の両方が要ります（書いていません）")}
        method, body = "POST", {"SKUID": str(sku_id), "NameCn": name_cn or code, "NameEn": new["NameEn"],
                                "Code": new["Code"], "Weight": int(weight or 0)}
    else:
        method, body = "PUT", {**customs_body(cur, sku_id), **new}
    try:
        log_id = _log_start(conn, "customs", str(sku_id), code, new, cur)
    except Exception as e:  # noqa: BLE001 — 戻す材料が残せないなら書かない
        return {"status": "fail", "message": t("台帳に記録できないため書いていません: {err}").format(err=f"{type(e).__name__}: {e}")}
    err = ""
    try:
        client.call(method, f"{SKU_PATH}/{sku_id}/customs", body=body)
    except Exception as e:  # noqa: BLE001 — 応答が読めないだけで書けている可能性 → 照会で判定
        err = f"{type(e).__name__}: {e}"
    try:
        after = get_customs(client, sku_id)
    except Exception as e:  # noqa: BLE001
        _log_done(conn, log_id, "fail", None, f"書込後の照会に失敗: {e} {err}")
        return {"status": "fail", "message": t("書込後の照会に失敗（斑马で確認してください）: {err}").format(err=e)
                + (t(" / 書込: {err}").format(err=err) if err else "")}
    a = after or {}
    bad = [k for k, v in new.items() if str(a.get(k) or "").strip() != v]
    if cur is not None:
        bad += [k for k in _CUSTOMS_KEEP if k in cur and a.get(k) != cur.get(k)]
        bad += [k for k in ("Code", "NameEn") if k not in new and (a.get(k) or "") != (cur.get(k) or "")]
    if not after or bad:
        st, msg = ("fail", t("書けていません: {why}").format(why=err)) if not after or a == (cur or {}) else (
            "mismatch", t("回読が一致しません: {fields}").format(fields=", ".join(bad))
            + (t(" / 書込: {err}").format(err=err) if err else ""))
    else:
        st, msg = ("created" if cur is None else "ok"), ""
    _log_done(conn, log_id, st, after, msg)
    return {"status": st, "message": msg}


# ───────────── 三辺・重量・画像（SPU 丸ごと）─────────────

def _norm(p: dict) -> dict:
    """実 API は SPU / SKUs（文書は Spu / Skus）→ 文書の綴りへ。"""
    p = dict(p)
    for real, doc in (("SPU", "Spu"), ("SKUs", "Skus")):
        if real in p and doc not in p:
            p[doc] = p.pop(real)
    return p


def get_product(client, spu_id: str) -> dict:
    return _norm((client.call("GET", f"{PRODUCT_PATH}/{spu_id}") or {})["Product"])


def blocked(p: dict) -> str:
    """API で書かない理由（描述 / 包材がある）。空文字 = 書いてよい。"""
    d = p.get("Descriptions") or {}
    if any(str(v or "").strip() for v in (d.values() if isinstance(d, dict) else [d])):
        return t("描述あり（API で書くと描述が消える）")
    if p.get("PackMaterials"):
        return t("包材あり（API の body に包材が無く消える恐れ）")
    return ""


def apply(p: dict, changes: dict[str, dict]) -> dict:
    """changes = {SKUID: {Weight/Length/Width/Height/Image: 値}}。画像を入れた SKU があり SPU 画像・画像一覧が空ならそれも入れる。"""
    p = copy.deepcopy(p)
    for s in p.get("Skus") or []:
        ch = changes.get(str(s.get("SKUID"))) or {}
        s.update({k: v for k, v in ch.items() if k in PRODUCT_FIELDS})
        img = ch.get("Image")
        if img:
            if not (p.get("Spu") or {}).get("Image"):
                p.setdefault("Spu", {})["Image"] = img
            if not p.get("Images"):
                p["Images"] = [{"Src": img, "Sort": 0}]
    return p


def build_body(p: dict) -> dict:
    """PUT body（write_sku_dims.build_body と同じ項目・同 SPU の他 SKU も全部回送）。p は apply 済みの現値。"""
    return {
        "Spu": {k: p["Spu"].get(k) for k in SPU_KEYS if k in p["Spu"]},
        "Descriptions": p.get("Descriptions"),
        "Seo": p.get("Seo"),
        "Skus": [{k: s.get(k) for k in SKU_KEYS if k in s} for s in p.get("Skus") or []],
        "Options": p.get("Options") or [],
        "Images": p.get("Images") or [],
        "Requirements": p.get("Requirements") or [],
        "Sources": p.get("Sources") or [],
        "Tags": [{"Name": t.get("Name")} for t in p.get("Tags") or []],
        "Suppliers": [{"ID": s.get("ID"), "Remark": s.get("Remark"), "Sort": s.get("Sort")}
                      for s in p.get("Suppliers") or []],
    }


def _canon(x):
    """比較用: UpdateTime を落とし、None と空文字・空の描述を同一視、画像一覧は URL の並びに。"""
    if isinstance(x, dict):
        out = {k: _canon(v) for k, v in x.items() if k != "UpdateTime"}
        if "Images" in out and isinstance(x.get("Images"), list):
            out["Images"] = [i.get("Src") if isinstance(i, dict) else i for i in x["Images"]]
        if isinstance(x.get("Descriptions"), dict) and not any(str(v or "").strip() for v in x["Descriptions"].values()):
            out["Descriptions"] = ""
        return out
    if isinstance(x, list):
        return [_canon(v) for v in x]
    return "" if x is None else x


def _diff(a, b, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b), key=str) for d in _diff(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in _diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [path.lstrip(".") or "(root)"]


def verify_product(before: dict, after: dict, changes: dict[str, dict]) -> str:
    """'' = OK（対象が新値・それ以外は書く前と完全一致）。画像は斑马側で URL が変わり得るので「空でない」ことだけ見る。"""
    exp = apply(before, changes)
    if any("Image" in ch for ch in changes.values()):
        asku = {str(s.get("SKUID")): s for s in after.get("Skus") or []}
        for s in exp.get("Skus") or []:
            ch = changes.get(str(s.get("SKUID"))) or {}
            if ch.get("Image"):
                got = (asku.get(str(s.get("SKUID"))) or {}).get("Image")
                if not got:
                    return t("画像が入っていません（SKU {sku}）").format(sku=s.get("SKUID"))
                s["Image"] = got
        if not (before.get("Spu") or {}).get("Image") and (after.get("Spu") or {}).get("Image"):
            exp["Spu"]["Image"] = after["Spu"]["Image"]
        if not before.get("Images") and after.get("Images"):
            exp["Images"] = after["Images"]
    d = _diff(_canon(exp), _canon(after))
    return "" if not d else t("回読が一致しません: {fields}").format(
        fields=", ".join(d[:8]) + (" " + t("ほか {n}").format(n=len(d) - 8) if len(d) > 8 else ""))


def write_product(client, conn, spu_id: str, changes: dict[str, dict], codes: str,
                  expect: dict[str, dict] | None = None) -> dict:
    """1 SPU を書く。→ {"status": ok/skipped/fail/mismatch, "message"}。mismatch なら呼び出し側は以降を止める。
    expect = {SKUID: チェック時の値}。書く直前の値が違えば書かない。"""
    try:
        before = get_product(client, spu_id)
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "message": t("SPU の照会に失敗（書いていません）: {err}").format(err=f"{type(e).__name__}: {e}")}
    why = blocked(before)
    if why:
        return {"status": "skipped", "message": t("{why} → 三辺・重量は Excel、画像は斑马の画面で").format(why=why)}
    want = {str(s.get("SKUID")) for s in before.get("Skus") or []}
    if expect:
        now = {str(s.get("SKUID")): s for s in before.get("Skus") or []}
        moved = [f"{sid} " + t("{k}: 今 {now} / チェック時 {was}").format(k=k, now=(now.get(sid) or {}).get(k),
                                                                         was=expect.get(sid, {}).get(k))
                 for sid, ch in changes.items() for k in ch if sid in expect and not _same((now.get(sid) or {}).get(k),
                                                                                          expect[sid].get(k))]
        if moved:
            return {"status": "skipped", "message": t("斑马の値がチェック時と違うため書いていません（{detail}）").format(
                detail=" · ".join(moved[:4]))}
    if not set(changes) <= want:
        return {"status": "fail", "message": t("SPU {spu} に無い SKU: {skus}（書いていません）").format(
            spu=spu_id, skus=sorted(set(changes) - want))}
    # 斑马は画像が 1 つも無い商品の PUT を 400 BadRequest で拒む（2026-11-13 重量のみ 70/70・画像を足した 1 件は ok）
    # → 新規登録（page15）と同じ「待上図」を入れる（Boss 2026-11-13「执行」）
    no_img = not ((before.get("Spu") or {}).get("Image") or before.get("Images")
                  or any(s.get("Image") for s in before.get("Skus") or []) or any(ch.get("Image") for ch in changes.values()))
    if no_img:
        changes = {sid: {**changes.get(sid, {}), "Image": PLACEHOLDER_IMAGE_URL} for sid in want}
    try:
        log_id = _log_start(conn, "product", str(spu_id), codes, changes, before)
    except Exception as e:  # noqa: BLE001
        return {"status": "fail", "message": t("台帳に記録できないため書いていません: {err}").format(err=f"{type(e).__name__}: {e}")}
    err = ""
    try:
        client.call("PUT", f"{PRODUCT_PATH}/{spu_id}", body=build_body(apply(before, changes)))
    except Exception as e:  # noqa: BLE001 — 応答が読めないだけの可能性 → 照会で判定
        err = f"{type(e).__name__}: {e}"
    try:
        after = get_product(client, spu_id)
    except Exception as e:  # noqa: BLE001
        _log_done(conn, log_id, "fail", None, f"書込後の照会に失敗: {e} {err}")
        return {"status": "mismatch", "message": t("書込後の照会に失敗（斑马で確認してください）: {err}").format(err=e)}
    msg = verify_product(before, after, changes)
    if msg and not _diff(_canon(before), _canon(after)):
        st, msg = "fail", t("書けていません: {why}").format(why=err or msg)
    else:
        st = "mismatch" if msg else "ok"
    _log_done(conn, log_id, st, after, msg + (f" / 書込: {err}" if err and st == "mismatch" else ""))
    if st == "ok" and no_img:
        msg = t("画像が無いため「待上図」画像も入れました（斑马は画像なしの書込を受けない）")
    return {"status": st, "message": msg}


def dims_xlsx(rows: list[dict]) -> bytes:
    """斑马「产品 → 批量修改SKU信息」の取込 Excel（SKU编码・重量・三辺。成本价は空 = 変えない・≤1000 行）。"""
    from openpyxl import Workbook
    from shared.banma_dims import TEMPLATE_HEAD
    wb = Workbook()
    ws = wb.active
    ws.append(TEMPLATE_HEAD)
    for r in rows[:1000]:
        ws.append([r["SKU编码"], None, r.get("重量(g)"), r.get("长(cm)"), r.get("宽(cm)"), r.get("高(cm)")])
        ws.cell(ws.max_row, 1).number_format = "@"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
