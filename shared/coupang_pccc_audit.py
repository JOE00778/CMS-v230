"""斑马の Coupang 待审核注文の PCCC を一括で関税庁照合する（page45）。Boss 2026-10-28。

斑马 GET /v1/order/（Status=待审核 · Platform=Coupang）は一覧に住所を含まない（2026-10-28 実測 33/33 件空）→
注文ごとに GET /v1/order/{ID} を引き、收货地址 の Name / TaxNo(=PCCC) / Phone / PostalCode を使う。
照合そのものは shared.pccc_check（GSI Express 経由・Boss 2026-09-04 承認）を**そのまま**使う。
⚠️ 오류 ≠ 番号が無効（pccc_check の docstring 参照）。理由を出して人が判断する。
⚠️ 氏名・PCCC・電話は保存しない（session_state のみ・画面はマスク・CSV は注文番号と理由だけ）。
"""
from __future__ import annotations

import re

from shared import pccc_check
from shared.coupang_to_ecms_xlsx import zip5

VERDICT_OK, VERDICT_NG, VERDICT_UNCHECKED = "正常", "有误", "未能检查"
_PCCC_RE = re.compile(r"P\d{12}")

# 관세청 メッセージ → 中文の要点（原文も併記する）
_REASONS = (
    ("존재하지 않", "PCCC 不存在"),
    ("납세의무자명", "姓名与 PCCC 登记不一致"),
    ("성명", "姓名与 PCCC 登记不一致"),
    ("전화번호", "电话与 PCCC 登记不一致"),
    ("우편번호", "邮编与 PCCC 登记不一致"),
    ("13자리", "PCCC 位数不对"),
    ("P로 시작", "PCCC 不是 P 开头"),
    ("사용정지", "PCCC 已停用"),
)


def reason_zh(message: str) -> str:
    for kw, zh in _REASONS:
        if kw in (message or ""):
            return zh
    return ""


def mask_name(n: str) -> str:
    n = (n or "").strip()
    if len(n) <= 1:
        return "*" * len(n)
    if len(n) == 2:
        return n[0] + "*"
    return n[0] + "*" * (len(n) - 2) + n[-1]


def mask_pccc(p: str) -> str:
    p = (p or "").strip()
    return p[:3] + "*" * (len(p) - 6) + p[-3:] if len(p) > 6 else "*" * len(p)


def target_from_order(order: dict) -> dict | None:
    """斑马 注文詳細 → {order_id, display_id, name, pccc, phone, zip}。住所が無ければ None。"""
    m = order.get("Master") or {}
    addrs = order.get("Addresses") or []
    a = next((x for x in addrs if str(x.get("AddrType")) == "收货地址"), addrs[0] if addrs else None)
    if not a:
        return None
    return {"order_id": str(m.get("ID") or ""),
            "display_id": str(m.get("DisplayOrderID") or m.get("OriginalOrderID") or m.get("ID") or ""),
            "name": (a.get("Name") or "").strip(),
            "pccc": (a.get("TaxNo") or "").strip().upper(),
            "phone": (a.get("Phone") or "").strip(),     # 書式はいじらない（pccc_check 9/08 実測時と同じ扱い）
            "zip": zip5(a.get("PostalCode"))}


def fetch_pending(client, on_progress=None) -> tuple[list[dict], list[str]]:
    """Coupang 待审核 を全件 → (対象, 住所が取れなかった斑马注文 ID)。"""
    ids, page = [], 1
    while True:
        d = client.call("GET", "/v1/order/", {"Status": "待审核", "Platform": "Coupang",
                                               "PageNumber": page, "PageSize": 50}) or {}
        ids += [str((o.get("Master") or {}).get("ID")) for o in (d.get("Orders") or [])]
        if not (d.get("Page") or {}).get("HasMore"):
            break
        page += 1
    targets, missing = [], []
    for n, oid in enumerate(ids, 1):
        dd = client.call("GET", f"/v1/order/{oid}") or {}
        t = target_from_order(dd.get("Order") or dd)
        targets.append(t) if t else missing.append(oid)
        if on_progress:
            on_progress(n, len(ids))
    return targets, missing


def audit(targets: list[dict], check=None) -> tuple[list[dict], str]:
    """→ (注文ごとの判定, 照合ツールのエラー文字列)。形式不正は送らずに 有误。장애 は 1 回だけ再照会。"""
    check = check or pccc_check.check
    send = [t for t in targets if _PCCC_RE.fullmatch(t["pccc"])]
    res, err = check(send) if send else ([], "")
    smap = pccc_check.status_map(res)
    retry = [t for t in send if (smap.get(t["pccc"]) or {}).get("status") != pccc_check.STATUS_OK
             and (smap.get(t["pccc"]) or {}).get("status") != pccc_check.STATUS_NG]
    if retry:
        res2, err2 = check(retry)
        smap.update({k: v for k, v in pccc_check.status_map(res2).items()
                     if v.get("status") != pccc_check.STATUS_FAULT})
        err = err2 or err
    rows = []
    for t in targets:
        base = {"display_id": t["display_id"], "order_id": t["order_id"],
                "name_masked": mask_name(t["name"]), "pccc_masked": mask_pccc(t["pccc"])}
        if not t["pccc"]:
            rows.append({**base, "verdict": VERDICT_NG, "reason": "PCCC 为空", "message": ""})
            continue
        if not _PCCC_RE.fullmatch(t["pccc"]):
            rows.append({**base, "verdict": VERDICT_NG,
                         "reason": "PCCC 格式错误（应为 P + 12 位数字）", "message": ""})
            continue
        hit = smap.get(t["pccc"]) or {}
        st = hit.get("status")
        msg = hit.get("message", "")
        if st == pccc_check.STATUS_OK:
            rows.append({**base, "verdict": VERDICT_OK, "reason": "", "message": msg})
        elif st == pccc_check.STATUS_NG:
            rows.append({**base, "verdict": VERDICT_NG, "reason": reason_zh(msg) or "关税厅返回错误", "message": msg})
        else:
            rows.append({**base, "verdict": VERDICT_UNCHECKED,
                         "reason": "关税厅系统故障或无返回（可稍后重试）", "message": msg})
    return rows, err
