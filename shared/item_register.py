"""商品登録 v2 のオーケストレーション（画面非依存・テスト可能）。

流れ（page15「🆕 商品登録」）:
  read_upload（人が書いた 12 列）→ existing_jans（NST 既登録は error）→ enrich（本モジュール）
  → data_editor で人が直す → from_frame → NST CSV + 斑马 xlsx → write_register_log（台帳）

enrich が埋めるもの（仕様書 SPEC_item_register_v2 §1・§4・§6）:
  アイテム名 = jancode.xyz 商品名（60 字で切る・切ったら warn。取れなければ error＝NST 必須）
  メーカー名 = resolve_maker（NST 同プレフィックス → jancode 会社名 → 空+warn）
  型番 = JAN / 取扱区分・商品ランク・部門・仕入区分・発注通貨 = FIXED / 税率 = 大分類「食品」なら ★
  購入価格・仕入先1：優先・優先場所・保管棚を使用・売上原価勘定・サポート提供・NE_連携対象・
    納税スケジュール・原価計算法 = NST 原本 BJ〜BS 列の数式の値（FIXED_DERIVED / _derived）
  _hs・_name_en = classify_customs（判定できなければ空のまま・推測で埋めない）
  want_weight のときだけ パッケージ寸法・重量を NETSEA → スーパーデリバリーで補う。
    **人が書いた値は上書きしない（空欄だけ埋める）**。埋めた列と source/url を記録。

fetch_meta / fetch_weight / classify_customs は引数で受ける（テストでモックを差すため）。
"""
from __future__ import annotations

import json
import re

from data_warehouse.templates.item_entry_form import (
    COL_JAN, COL_LARGE, COL_OWNER, COL_PKG_DIMS, COL_PKG_WEIGHT, INPUT_COLUMNS, Issue,
)
from shared.jan_web import INTERVAL, fetch_many
from shared.nst_choices import FIXED, resolve_maker

NAME_MAX = 60
COL_NAME = "アイテム名"
COL_MAKER = "メーカー名"
COL_ITEM_CODE = "型番"
COL_TAX = "税率"
COL_NAME_EN = "_name_en"
COL_HS = "_hs"

# WeightHit の属性 → NST 列
_WEIGHT_FIELDS = {
    COL_PKG_DIMS[0]: "height_cm", COL_PKG_DIMS[1]: "width_cm",
    COL_PKG_DIMS[2]: "depth_cm", COL_PKG_WEIGHT: "weight_g",
}
# NST 原本 row3「特殊文字禁止 / 全角カッコ、カンマ禁止 / ローマ数字禁止」の機械判定できる分
_BANNED_RE = re.compile(r"[（）,，Ⅰ-ⅿ]")

# NST 原本 BF〜BS 列の数式（row4）を 仕入区分=輸出専用・部門=輸出事業（FIXED）で評価した値。
# 原本「登録手順」2 で値貼付けされて CSV に入る列。輸出専用では 適正在庫水準の日数・購買リード・タイム・
# 安全在庫日数・リードタイムを自動計算・輸入諸掛をトラッキング は空。FIXED を変えたらここも見直す。
FIXED_DERIVED: dict[str, str] = {
    "仕入先1：優先": "T", "優先場所": "弁天倉庫", "保管棚を使用": "T",
    "売上原価勘定": "501121 仕入 : 仕入 : 仕入高 : 国内仕入-輸出",
    "サポート提供 ": "T",            # 列名は原本どおり末尾半角空白
    "NE_連携対象": "F",              # 部門が「EC (BtoC）」以外は F
    "原価計算法": "平均",            # 原本 row3「インポート後に変更不可能」
}
_FOOD_CATS = {"食品", "菓子", "飲料", "サプリメント"}   # 大分類=食品 で受け入れる HS 品類


def _derived(row: dict) -> dict:
    """BJ 購入価格 = 商品原価 / BR 納税スケジュール = 税率 ★ なら 仕入8%＆販売免税、他は 仕入10%＆販売免税。"""
    return {**FIXED_DERIVED, "購入価格": row.get("商品原価", ""),
            "納税スケジュール": "仕入8%＆販売免税" if row.get(COL_TAX) == "★" else "仕入10%＆販売免税"}


# 画面の表（data_editor）: 表示名 → 行 dict のキー
FRAME_COLUMNS: dict[str, str] = {
    "JANコード": COL_JAN, "アイテム名": COL_NAME, "メーカー名": COL_MAKER,
    "通関英文名": COL_NAME_EN, "HS": COL_HS,
    "大分類": "大分類", "中分類": "中分類", "商品原価": "商品原価",
    "仕入先1：仕入先": "仕入先1：仕入先", "商品担当者": COL_OWNER,
    **{c: c for c in (*COL_PKG_DIMS, COL_PKG_WEIGHT)},
    "カートン入数": "カートン入数", "発注ロット": "発注ロット",
}
EDITABLE = ["アイテム名", "メーカー名", "通関英文名", "HS", *COL_PKG_DIMS, COL_PKG_WEIGHT]
COL_AUTO = "🤖 自動入力"
COL_URL = "網調べ URL"


def _fmt(v) -> str:
    """195.0 → "195" / 13.5 → "13.5"。"""
    return f"{v:g}" if isinstance(v, float) else str(v)


def _banned_warn(col: str, val: str) -> str | None:
    bad = sorted(set(_BANNED_RE.findall(val)))
    if bad:
        return f"{col} に NST 原本の禁止文字（全角カッコ・カンマ・ローマ数字）: {' '.join(bad)}"
    return None


def enrich(rows, *, choices, prefix_makers, fetch_meta, fetch_weight, classify_customs,
           want_weight: bool, on_progress=None, interval: float = INTERVAL):
    """read_upload の正常行 → (nst_rows, bm_extra, issues, stats)。

    nst_rows : NST 行 dict（キー = NST テンプレ列名原文・空の列は持たない）。error 行は入らない
    bm_extra : {jan: {"_hs", "_name_en", "hs_category", "hs_basis", "maker_source",
                      "auto": [埋めた列], "weight_source", "weight_url", "weight_note"}}
    issues   : Issue（row=0・jan で特定。read_upload で JAN はファイル内一意）
    stats    : ok/ng/warn と jancode/メーカー/重量/HS の取得・失敗件数
    on_progress(done, total, msg)。fetch は fetch_many 経由で interval 秒間隔。
    """
    # choices: 選択肢の検証は read_upload 済み。インタフェース互換のため受けるだけ
    rows = list(rows)
    jans = [r[COL_JAN] for r in rows]
    total = len(jans) * (2 if want_weight else 1)
    stats = {k: 0 for k in (
        "ok", "ng", "warn", "jancode_ok", "jancode_not_found", "jancode_error",
        "name_cut", "maker_nst", "maker_jancode", "maker_none", "hs_ok", "hs_ng",
        "weight_hit", "weight_miss", "weight_error", "weight_skipped")}

    def _prog(offset, label):
        if on_progress is None:
            return None
        return lambda i, n, jan: on_progress(offset + i, total, f"{label} {jan}")

    metas = fetch_many(jans, fetch_meta, interval=interval, on_progress=_prog(0, "jancode"))

    # 重量は 4 項目とも人が書いた行は調べない（時間の無駄）
    need_w = [r[COL_JAN] for r in rows
              if want_weight and any(not r.get(c) for c in _WEIGHT_FIELDS)]
    hits = fetch_many(need_w, fetch_weight, interval=interval,
                      on_progress=_prog(len(jans), "重量")) if need_w else {}
    if want_weight and on_progress:
        on_progress(total, total, "完了")

    nst_rows: list[dict] = []
    bm_extra: dict[str, dict] = {}
    issues: list[Issue] = []
    # ponytail: NST 既存メーカー = プレフィックスで 1 種類に決まった maker の集合。全 maker 一覧が要るなら loader を足す
    nst_makers = set(prefix_makers.values())
    for r in rows:
        jan = r[COL_JAN]
        errs: list[str] = []
        warns: list[str] = []
        meta = metas.get(jan)
        if isinstance(meta, dict) or meta is None:          # fetch_many が握った例外
            stats["jancode_error"] += 1
            errs.append(f"jancode.xyz 取得失敗（再処理してください）: {(meta or {}).get('error', '')}")
        elif meta.status == "error":
            stats["jancode_error"] += 1
            errs.append(f"jancode.xyz 取得失敗（再処理してください）: {meta.error}")
        elif meta.status != "ok" or not (meta.name or "").strip():
            stats["jancode_not_found"] += 1
            errs.append("jancode.xyz に商品名がありません（NST はアイテム名必須）")
        else:
            stats["jancode_ok"] += 1
        if errs:
            stats["ng"] += 1
            issues.extend(Issue(0, jan, "error", m) for m in errs)
            continue

        name = meta.name.strip()
        if len(name) > NAME_MAX:
            stats["name_cut"] += 1
            warns.append(f"アイテム名が {len(name)} 字 → {NAME_MAX} 字で切りました")
            name = name[:NAME_MAX]
        maker, maker_src = resolve_maker(jan, meta.maker, prefix_makers)
        stats[{"nst-prefix": "maker_nst", "jancode": "maker_jancode"}.get(maker_src, "maker_none")] += 1
        if maker_src == "none":
            warns.append("メーカー名が決まりません（NST 同プレフィックス・jancode 会社名とも無し）")
        elif maker_src == "jancode" and maker not in nst_makers:
            warns.append(f"メーカー名「{maker}」は jancode 会社名で NST 既存メーカーに無い表記です（確認してください）")
        auto = ["アイテム名(jancode)"] + ([f"メーカー名({maker_src})"] if maker else [])

        row = {COL_ITEM_CODE: jan, COL_NAME: name, **{c: r.get(c, "") for c in INPUT_COLUMNS},
               COL_MAKER: maker, **FIXED,
               COL_TAX: "★" if r.get(COL_LARGE) == "食品" else ""}
        row.update(_derived(row))

        extra = {"maker_source": maker_src, "auto": auto,
                 "weight_source": None, "weight_url": "", "weight_note": ""}
        if jan in hits:
            hit = hits[jan]
            if isinstance(hit, dict):
                stats["weight_error"] += 1
                warns.append(f"重量・寸法の網調べ失敗: {hit.get('error', '')}")
            elif hit is None:
                stats["weight_miss"] += 1
                warns.append("重量・寸法: NETSEA・スーパーデリバリーとも個装の記載なし")
            else:
                filled = [c for c, a in _WEIGHT_FIELDS.items()
                          if not row.get(c) and getattr(hit, a) is not None]
                for c in filled:
                    row[c] = _fmt(getattr(hit, _WEIGHT_FIELDS[c]))
                if filled:
                    stats["weight_hit"] += 1
                    auto.append(f"{'/'.join(filled)}({hit.source})")
                    extra.update(weight_source=hit.source, weight_url=hit.url,
                                 weight_note=hit.note)
                else:  # 取れた値は手入力済みの列だけ（上書きしない）
                    stats["weight_miss"] += 1
        elif want_weight:
            stats["weight_skipped"] += 1

        cus = classify_customs(name, maker, jan, prefix_map=prefix_makers)
        hs, name_en, cat, basis = cus.hs, cus.name_en, cus.category, cus.basis
        # 判定規則は食品を想定していない（ポテトチップス→化粧小物、ミルクチョコ→乳液）。推測で埋めない
        if hs and r.get(COL_LARGE) == "食品" and cat not in _FOOD_CATS:
            warns.append(f"HS: 大分類=食品 なのに品類「{cat}」と判定 → 空にしました（手で記入可）")
            hs = name_en = cat = None
            basis = "food-veto"
        elif not hs:
            warns.append("HS・通関英文名を判定できません（空のまま・手で記入可）")
        stats["hs_ok" if hs else "hs_ng"] += 1
        extra.update({COL_HS: hs or "", COL_NAME_EN: name_en or "",
                      "hs_category": cat or "", "hs_basis": basis})

        for col in (COL_NAME, COL_MAKER):
            w = _banned_warn(col, row[col])
            if w:
                warns.append(w)

        nst_rows.append({k: v for k, v in row.items() if str(v).strip() != ""})
        bm_extra[jan] = extra
        if warns:
            stats["warn"] += 1
            issues.extend(Issue(0, jan, "warn", m) for m in warns)
        stats["ok"] += 1
    return nst_rows, bm_extra, issues, stats


def to_frame(nst_rows: list[dict], bm_extra: dict[str, dict]) -> list[dict]:
    """data_editor 用のレコード（表示名キー）。自動入力の列と網調べ URL を添える。"""
    out = []
    for r in nst_rows:
        ex = bm_extra.get(r[COL_JAN], {})
        rec = {disp: str({**r, **ex}.get(key, "") or "") for disp, key in FRAME_COLUMNS.items()}
        rec[COL_AUTO] = " · ".join(ex.get("auto", []))
        rec[COL_URL] = ex.get("weight_url", "")
        out.append(rec)
    return out


def from_frame(records: list[dict], nst_rows: list[dict], bm_extra: dict[str, dict]):
    """人が直した表 → (nst_rows, bm_rows, weight_sources, issues)。

    bm_rows = NST 行 + "_hs"/"_name_en"（build_bm_xlsx へそのまま渡す形）。
    アイテム名が空になった行は error で外す。60 字超は切って warn。数値でない寸法・重量は空にして warn。
    """
    base = {r[COL_JAN]: r for r in nst_rows}
    out_nst, out_bm, sources, issues = [], [], {}, []
    for rec in records:
        jan = str(rec.get("JANコード") or "").strip()
        if jan not in base:
            continue
        row = dict(base[jan])
        ex = bm_extra.get(jan, {})
        edited = {FRAME_COLUMNS[c]: str(rec.get(c) if rec.get(c) is not None else "").strip()
                  for c in EDITABLE}
        for c in (*COL_PKG_DIMS, COL_PKG_WEIGHT):
            v = edited[c]
            try:
                ok = float(v) > 0 if v else True
            except ValueError:
                ok = False
            if not ok:
                issues.append(Issue(0, jan, "warn", f"{c} が正の数ではないので空にしました（{v}）"))
                edited[c] = ""
        if not edited[COL_NAME]:
            issues.append(Issue(0, jan, "error", "アイテム名が空です（NST 必須）"))
            continue
        for c in (COL_NAME, COL_MAKER):
            if len(edited[c]) > NAME_MAX:
                issues.append(Issue(0, jan, "warn", f"{c} を {NAME_MAX} 字で切りました"))
                edited[c] = edited[c][:NAME_MAX]
        hs, name_en = edited.pop(COL_HS), edited.pop(COL_NAME_EN)
        for c, v in edited.items():
            if v:
                row[c] = v
            else:
                row.pop(c, None)
        out_nst.append(row)
        out_bm.append({**row, COL_HS: hs, COL_NAME_EN: name_en})
        if ex.get("weight_source"):
            sources[jan] = ex["weight_source"]
        elif any(row.get(c) for c in _WEIGHT_FIELDS):
            sources[jan] = "manual"
        else:
            sources[jan] = None
    return out_nst, out_bm, sources, issues


def csv_field_columns(nst_rows: list[dict]) -> list[str]:
    """NST CSV に出す列 = 有データ列のみ（原本「登録手順」3: 入力列以外は削除）。型番は id 列で出す。"""
    from data_warehouse.templates.nst_item_master import NST_MASTER_COLUMNS
    have = {k for r in nst_rows for k, v in r.items() if str(v).strip() != ""}
    return [n for _, n in NST_MASTER_COLUMNS if n in have and n != COL_ITEM_CODE]


_INSERT = ("INSERT INTO nst.item_register_log "
           "(jan, payload, bm_payload, weight_source, registered_by) "
           "VALUES (?, ?::jsonb, ?::jsonb, ?, ?)")


def write_register_log(conn, nst_rows: list[dict], bm_rows: list[list] | None,
                       weight_sources: dict[str, str | None] | None = None) -> tuple[int, str]:
    """台帳 nst.item_register_log へ追記。戻り (件数, エラー文)。例外は投げない（CSV 生成を止めない）。

    bm_rows は nst_to_bm_row の 46 列 list（nst_rows と同順）→ BM_HEADER をキーに JSON 化。
    """
    from data_warehouse.templates.jd_bm_item_master import BM_HEADER
    weight_sources = weight_sources or {}
    params = []
    for i, r in enumerate(nst_rows):
        jan = str(r.get(COL_JAN, "")).strip()
        bm = dict(zip(BM_HEADER, bm_rows[i])) if bm_rows else None
        params.append((jan, json.dumps(r, ensure_ascii=False),
                       json.dumps(bm, ensure_ascii=False) if bm is not None else None,
                       weight_sources.get(jan), r.get(COL_OWNER) or None))
    if not params:
        return 0, ""
    try:
        conn.executemany(_INSERT, params)
        conn.commit()
    except Exception as e:  # noqa: BLE001 — 表がまだ本番に無い等。呼び出し側が画面に出す
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0, f"{type(e).__name__}: {e}"
    return len(params), ""
