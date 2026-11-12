"""page15「🔎 SKUID 空チェック」（Boss 2026-11-12）.

NST で SKUID（custitem_item_skuid＝斑马 SKUID・在庫同期用）が空の品を出し、
斑马に同じ型番 / JAN の SKU があればその SKUID、無ければ斑马に登録して取った SKUID で
NST 更新 CSV（型番 + SKUID）を作る。CMS は NST に書かない（CSV は人が取り込む）。
条件はすべて画面で選べる。既定 = 部門 輸出 2 つ・取扱中・有効・直近 30 日・全ランク・型番+JAN 照合・斑马 API・
1 SKUID↔複数品は CSV に出さない（Boss「你的方式为默认选项」）。

2026-11-12 実測: 輸出・取扱中・有効で SKUID 空 2,159 件、うち斑马に型番 / JAN で在るのは 7 件。
その 7 件中 2 組は 1 つの斑马 SKU を NST の 2 品が指していた（soel-064 と 4571502021054）→ 重複は人が判断。
"""
from __future__ import annotations

import datetime as dt
import json

from data_warehouse.templates import nst_item_master as TPL
from data_warehouse.templates.item_entry_form import COL_JAN, COL_PKG_DIMS, COL_PKG_WEIGHT
from shared.i18n import t
from shared.item_register import COL_HS, COL_IMAGE, COL_ITEM_CODE, COL_MAKER, COL_NAME, COL_NAME_EN, NAME_MAX

BLANK = "（空）"
DEPTS = ("輸出事業", "輸出（中国）")                       # item.department 4 / 9（pull_items と同じ範囲）
HANDLINGS = ("取扱中", "取扱中止", "メーカー取扱中止", "在庫処分", BLANK)
STATES = ("有効", "非アクティブ")
RANKS = ("NEW", "Aランク", "Bランク", "Cランク", "取扱中止", BLANK)
PERIODS: dict[str, int | None] = {"直近 7 日": 7, "直近 30 日": 30, "直近 90 日": 90, "直近 1 年": 365, "全期間": None}
DEFAULTS = {"period": "直近 30 日", "depts": DEPTS, "handlings": ("取扱中",), "states": ("有効",), "ranks": RANKS,
            "by_jan": True, "source": "api", "allow_dup": False}

_SQL = (
    "SELECT item.id AS id, item.itemid AS code, item.upccode AS jan, item.displayname AS name, "
    "BUILTIN.DF(item.department) AS dept, BUILTIN.DF(item.custitem_item_handling_cd) AS handling, "
    "item.isinactive AS inactive, BUILTIN.DF(item.custitem_fb_item_rank) AS item_rank, item.createddate AS created, "
    "item.custitem_fb_madein AS maker, item.custitem_fb_item_cost AS cost, "
    "item.custitem_fb_package_weight AS pkg_weight, item.custitem_fb_package_height AS pkg_height, "
    "item.custitem_fb_package_width AS pkg_width, item.custitem_fb_package_depth AS pkg_depth, "
    "item.custitem_item_skuid AS sku_id "
    "FROM item WHERE item.department IN (4, 9) AND item.custitem_item_skuid IS {null}{since} ORDER BY item.id")
_CHUNK = 200


def _v(x) -> str:
    return str(x or "").strip()


def blank_items(suiteql_all, *, days: int | None, today: dt.date | None = None, has_skuid: bool = False) -> list[dict]:
    """NST の SKUID 空の品（has_skuid=True なら SKUID がある品＝斑马に在る品）。部門 輸出 2 つ・作成日 days 日以内
    （None = 全期間）。他の条件は pick で絞る。"""
    since = ""
    if days:
        d = (today or dt.date.today()) - dt.timedelta(days=days)
        since = f" AND item.createddate >= TO_DATE('{d:%Y-%m-%d}', 'YYYY-MM-DD')"
    return suiteql_all(_SQL.format(since=since, null="NOT NULL" if has_skuid else "NULL"))


def pick(items: list[dict], *, depts, handlings, states, ranks) -> list[dict]:
    def state(r):
        return "非アクティブ" if _v(r.get("inactive")) == "T" else "有効"
    return [r for r in items
            if (_v(r.get("dept")) or BLANK) in depts and (_v(r.get("handling")) or BLANK) in handlings
            and state(r) in states and (_v(r.get("item_rank")) or BLANK) in ranks]


def lookup_keys(items: list[dict], *, by_jan: bool) -> list[str]:
    """斑马に照会するコード（型番・by_jan なら JAN も）。"""
    keys = {_v(r.get("code")) for r in items} | ({_v(r.get("jan")) for r in items} if by_jan else set())
    return sorted(k for k in keys if k)


def mirror_skus(conn, keys: list[str]) -> dict[str, str]:
    """PG banma.sku_dims（毎日 04:00 の斑马全 SKU 写し）で コード → SKUID。"""
    out: dict[str, str] = {}
    for i in range(0, len(keys), _CHUNK):
        chunk = keys[i:i + _CHUNK]
        rows = conn.execute("SELECT code, sku_id FROM banma.sku_dims WHERE code IN ("
                            + ",".join("?" for _ in chunk) + ")", tuple(chunk)).fetchall()
        out.update({r["code"]: str(r["sku_id"] or "") for r in rows})
    return out


def skuids_in_use(suiteql_all, sku_ids: list[str]) -> dict[str, list[str]]:
    """SKUID → それを既に持つ NST の型番（別の品に同じ SKUID を入れないため）。"""
    out: dict[str, list[str]] = {}
    uniq = sorted({s for s in sku_ids if s.isdigit()})   # 斑马 SKUID は数字だけ（SQL に入れるので数字以外は捨てる）
    for i in range(0, len(uniq), _CHUNK):
        rows = suiteql_all("SELECT item.itemid AS code, item.custitem_item_skuid AS sku_id FROM item "
                           "WHERE item.custitem_item_skuid IN (" + ",".join(f"'{s}'" for s in uniq[i:i + _CHUNK])
                           + ") ORDER BY item.id")
        for r in rows:
            out.setdefault(_v(r.get("sku_id")), []).append(_v(r.get("code")))
    return out


def match(items: list[dict], bm: dict[str, str], *, by_jan: bool,
          in_use: dict[str, list[str]] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """→ (hits, conflicts, misses)。型番一致を先に、無ければ JAN 一致。
    1 つの SKUID に今回の品が 2 件以上、または NST の別の品が既にその SKUID を持つ → conflicts（人が判断）。"""
    found, misses = [], []
    for r in items:
        code, jan = _v(r.get("code")), _v(r.get("jan"))
        if bm.get(code):
            found.append({**r, "sku_id": bm[code], "key": "型番"})
        elif by_jan and jan and bm.get(jan):
            found.append({**r, "sku_id": bm[jan], "key": "JAN"})
        else:
            misses.append(r)
    in_use = in_use or {}
    hits, conflicts = [], []
    for f in found:
        same = [_v(x.get("code")) for x in found if x["sku_id"] == f["sku_id"] and x is not f]
        others = [c for c in in_use.get(f["sku_id"], []) if c != _v(f.get("code"))]
        if same or others:
            conflicts.append({**f, "note": t("同じ斑马 SKUID: {detail}").format(detail=" / ".join(
                x for x in (same and t("今回の対象 {codes}").format(codes="・".join(same)),
                            others and t("NST で設定済み {codes}").format(codes="・".join(others))) if x))})
        else:
            hits.append(f)
    return hits, conflicts, misses


def load_ledger(conn, codes: list[str]) -> dict[str, dict]:
    """page15 台帳 nst.item_register_log の型番ごとの最新行 → {code: {"payload": dict, "bm": dict}}（人が確認済みの値）。"""
    out: dict[str, dict] = {}
    for i in range(0, len(codes), _CHUNK):
        chunk = codes[i:i + _CHUNK]
        rows = conn.execute("SELECT item_code, payload, bm_payload FROM nst.item_register_log WHERE id IN ("
                            "SELECT max(id) FROM nst.item_register_log WHERE item_code IN ("
                            + ",".join("?" for _ in chunk) + ") GROUP BY item_code)", tuple(chunk)).fetchall()
        for r in rows:
            load = (lambda x: x if isinstance(x, dict) else json.loads(x) if x else {})
            out[r["item_code"]] = {"payload": load(r["payload"]), "bm": load(r["bm_payload"])}
    return out


_PKG = dict(zip((*COL_PKG_DIMS, COL_PKG_WEIGHT), ("pkg_height", "pkg_width", "pkg_depth", "pkg_weight")))


def register_rows(misses: list[dict], ledger: dict[str, dict], classify) -> tuple[list[dict], dict[str, dict]]:
    """斑马に無い品 → 斑马登録用の行（page15 と同じキー）と extra。
    台帳に行があれば台帳の値（page15 で人が確認した名前・原価・寸法・HS・英文名・画像）、無ければ NST の値 +
    classify(名前, メーカー, JAN) → Customs で HS・英文名。画像は呼び出し側で fill_images。"""
    rows, extra = [], {}
    for r in misses:
        code = _v(r.get("code"))
        led = ledger.get(code)
        if led and led["payload"]:
            p, bm = led["payload"], led["bm"]
            row = {COL_ITEM_CODE: code, COL_JAN: _v(p.get(COL_JAN)) or _v(r.get("jan")),
                   COL_NAME: _v(p.get(COL_NAME)) or _v(r.get("name"))[:NAME_MAX],
                   COL_MAKER: _v(p.get(COL_MAKER)), "商品原価": _v(p.get("商品原価")) or _v(r.get("cost")),
                   **{c: _v(p.get(c)) or _v(r.get(k)) for c, k in _PKG.items()}}
            ex = {COL_HS: _v(bm.get("海关编码")), COL_NAME_EN: _v(bm.get("英文名称")),
                  COL_IMAGE: _v(bm.get("图片URL")), "source": "台帳"}
        else:
            name = _v(r.get("name"))[:NAME_MAX]
            row = {COL_ITEM_CODE: code, COL_JAN: _v(r.get("jan")), COL_NAME: name, COL_MAKER: _v(r.get("maker")),
                   "商品原価": _v(r.get("cost")), **{c: _v(r.get(k)) for c, k in _PKG.items()}}
            cus = classify(name, row[COL_MAKER], row[COL_JAN]) if name else None
            ex = {COL_HS: _v(getattr(cus, "hs", "")), COL_NAME_EN: _v(getattr(cus, "name_en", "")),
                  COL_IMAGE: "", "source": "NST"}
        rows.append(row)
        extra[code] = ex
    return rows, extra


def update_csv(pairs: list[tuple[str, str]]) -> bytes:
    """[(型番, SKUID)] → NST 更新 CSV（型番で照合・page03/07 と同じ原本テンプレ書式。サポート提供=T は原本の既定列）。"""
    return TPL.build_nst_master_csv([{"型番": c, "SKUID": s} for c, s in pairs if c and s], ["SKUID"],
                                    id_label="型番")
