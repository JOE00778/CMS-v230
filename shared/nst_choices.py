"""商品登録 v2 の選択肢（仕入先 / 大分類・中分類 / 商品担当者 / 固定値）と NST 照合。

正の源は NST（Boss 2026-10-03「すべて NST が核心」）。NST に無いものだけ PG で補う。
- 仕入先: PG `nst.vendor_master`（NST Vendor の毎日 06:50 全件鏡像）· page10 と共有
- 商品担当者・大分類・中分類: NST 原本テンプレ V260326EX のデータベース シートの写し（定数）
- 既存 JAN: SuiteQL item.upccode（NST 全体）。失敗時は PG `nst.item_master_raw`（輸出部門のみ）
- メーカー名: JAN 先頭 7 桁が NST 既存品で 1 社に決まればその NST 表記 → jancode 会社名 → 空

DB/Streamlit 非依存（conn と suiteql は引数で受ける）。キャッシュは呼び出し側（page）で。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 商品担当者・大分類・中分類 = NST 原本テンプレ「20260000_NetSuite【アイテム】マスタ登録-V260326EX」
# データベース シート（C2:C12 / R2:W2 / 各列の中分類 = INDIRECT 用の定義名）の写し（Boss 2026-11-09「按照模板」）。
# 原本の版が変わったらここを差し替える。ASEAN / USA の定義名は原本で #REF!（大分類の選択肢に無い）ので入れない。
OWNERS: tuple[str, ...] = ("000李キン", "003 石川徳道", "007 安部龍也", "010 祁王傑", "020 中村陽太",
                           "031 斎藤裕史", "037 米澤和敏", "043 徐越", "060 中村貴明", "067 星野麻衣",
                           "005 川崎里子")
FIXED: dict[str, str] = {"取扱区分": "取扱中", "商品ランク": "NEW", "部門": "輸出事業",
                         "仕入区分": "輸出専用", "発注通貨": "日本円"}
MAKER_MAX = 60
_JAN_CHUNK = 200

_LIFE = ("生活家電", "季節家電", "ガーデン・エクステリア", "DIY・工具", "日用品雑貨", "季節雑貨", "ホビー",
         "スポーツ・健康", "衛生日用品", "日用消耗品", "その他（生活雑貨）")
_OUTDOOR = ("アウトドア用品", "防災", "その他（アウトドア・防災）")
_CAR = ("カーエレクトロニクス", "アクセサリー", "保安整備", "その他（カー・バイク用品）")
MIDDLE_BY_LARGE: dict[str, tuple[str, ...]] = {
    "カー・バイク用品": _CAR,
    "アウトドア・防災": _OUTDOOR,
    "生活雑貨": _LIFE,
    "食品": ("加工食品", "菓子", "健康食品", "嗜好品（茶・コーヒー）", "調味料"),
    "中国": ("パール", *_LIFE, *_OUTDOOR, *_CAR, "加工食品"),
    "パール": ("パール",),
}


@dataclass(frozen=True)
class Choices:
    suppliers: tuple[str, ...]                      # "0001 ベストアンサ株式会社"
    middle_by_large: dict[str, tuple[str, ...]]     # 大分類 -> 中分類（NST 原本テンプレ）
    owners: tuple[str, ...] = OWNERS
    warnings: tuple[str, ...] = ()                  # フォールバック使用などの通知

    @property
    def large(self) -> tuple[str, ...]:
        return tuple(self.middle_by_large)


def _default_suiteql():
    from shared.nst_suiteql import suiteql
    return suiteql


def load_suppliers(conn) -> list[str]:
    """仕入先ドロップダウン = NetSuite Vendor マスタ（`nst.vendor_master`）が唯一の源。

    以前は 38 件をソースにベタ書きしていた（2026 年のある時点のスナップショット）。
    NetSuite 側で改名・新規追加があってもコードを直さない限り反映されず、古い名前の
    まま出した CSV が取込エラーになっていた（隋艶偉さん 2026-07-21 指摘）。
    `nst.vendor_master` は vendor_daily ジョブが毎日 06:50 JST に全件 UPSERT する
    ので、ここを見れば改名も追加も自動で追従する。表記は `vendor_code + 空白 +
    company_name`＝NetSuite の表示名そのもの（旧ベタ書きと同一書式なので
    page10 `_guess_supplier()` の 4 桁プレフィックス照合はそのまま動く）。
    company_name 空欄（経理用ダミー等 17 件）は CSV に出しても無意味なので除外。
    page10（発注書作成）と商品登録 v2 で共有。
    """
    rows = conn.execute(
        "SELECT vendor_code, company_name FROM nst.vendor_master "
        "WHERE company_name IS NOT NULL AND company_name <> '' "
        "  AND vendor_code IS NOT NULL AND vendor_code <> '' "
        "ORDER BY vendor_code"
    ).fetchall()
    return [f"{r['vendor_code']} {r['company_name']}" for r in rows]


def load_choices(conn, *, suiteql=None) -> Choices:  # noqa: ARG001 — suiteql は旧呼び出し互換
    suppliers = load_suppliers(conn)
    warns: list[str] = []
    if not suppliers:
        warns.append("仕入先マスタが空です。page 27「📥 NST 取得データ」→ "
                     "「🏭 仕入先マスタ」で更新してください。")
    return Choices(suppliers=tuple(suppliers), middle_by_large=dict(MIDDLE_BY_LARGE),
                   warnings=tuple(warns))


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def existing_jans(jans: list[str], *, conn=None, suiteql=None) -> tuple[set[str], list[str]]:
    """JAN を NST 全体に照合。失敗時は輸出部門の PG 鏡像へ。"""
    return _existing_values(jans, field="upccode", pg_field="jan", conn=conn, suiteql=suiteql)


def existing_item_codes(codes: list[str], *, conn=None, suiteql=None) -> tuple[set[str], list[str]]:
    """型番を NST 全体に照合。JAN の無い商品も重複登録を防ぐ。"""
    return _existing_values(codes, field="itemid", pg_field="item_code", conn=conn, suiteql=suiteql)


def _existing_values(jans: list[str], *, field: str, pg_field: str, conn=None, suiteql=None) -> tuple[set[str], list[str]]:
    """JAN または型番を NST 全体に照合（field / pg_field は内部の固定値だけ）。

    SuiteQL が 1 チャンクでも失敗したら PG 鏡像で全件照合し直し、
    「輸出部門のみの照合」と警告する。PG も無い/失敗なら空集合＋警告（黙って新規扱いにしない）。
    """
    uniq = sorted({str(j).strip() for j in jans if str(j or "").strip()})
    if not uniq:
        return set(), []
    q = suiteql or _default_suiteql()
    try:
        found: set[str] = set()
        for i in range(0, len(uniq), _JAN_CHUNK):
            chunk = uniq[i:i + _JAN_CHUNK]
            rows = q(f"SELECT {field} FROM item WHERE {field} IN ("
                     + ",".join(_quote(j) for j in chunk) + ")", limit=1000)
            found.update(str(r.get(field) or "").strip() for r in rows)
        return found & set(uniq), []
    except Exception as e:  # noqa: BLE001
        sq_err = str(e)
    if conn is None:
        return set(), [f"NST 既存識別子 照合に失敗（SuiteQL: {sq_err}）。PG 代替も無いため"
                       f"重複チェック未実施 {len(uniq)} 件"]
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {pg_field} FROM nst.item_master_raw WHERE {pg_field} IN ("
            + ",".join("?" for _ in uniq) + ")", tuple(uniq)).fetchall()
    except Exception as e:  # noqa: BLE001
        return set(), [f"NST 既存識別子 照合に失敗（SuiteQL: {sq_err} / PG: {e}）。"
                       f"重複チェック未実施 {len(uniq)} 件"]
    found = {str(r[pg_field]).strip() for r in rows}
    return found & set(uniq), [f"SuiteQL 失敗のため PG nst.item_master_raw で照合"
                               f"（NST 全体ではなく輸出部門のみの照合）: {sq_err}"]


_MAKER_NOISE = re.compile(r"株式会社|有限会社|合同会社|\(株\)|（株）|㈱|[\s・.,]")


def load_prefix_makers(conn) -> dict[str, str]:
    """13 桁 JAN の先頭 7 桁 → NST 既存 maker（そのプレフィックスで maker が 1 社に決まるものだけ）。

    大小文字・空白・法人格だけの違い（TAKARA TOMY 272 件 / TAKARATOMY 7 件）は同じ 1 社とみなし、件数最多の NST 表記を採る
    （Boss 2026-11-12「用 NST 的写法」）。表記そのものが違う（白元アース㈱ / Hakugen Earth（白元アース））は採らない＝確認表で人が埋める。"""
    rows = conn.execute(
        "SELECT SUBSTR(jan, 1, 7) AS prefix, TRIM(maker) AS maker, COUNT(*) AS n "
        "FROM nst.item_master_raw "
        "WHERE LENGTH(jan) = 13 AND maker IS NOT NULL AND TRIM(maker) <> '' "
        "GROUP BY SUBSTR(jan, 1, 7), TRIM(maker) ORDER BY 1, 3 DESC, 2"
    ).fetchall()
    by: dict[str, list[str]] = {}
    for r in rows:   # プレフィックスごとに件数の多い順
        by.setdefault(r["prefix"], []).append(r["maker"])
    return {p: ms[0] for p, ms in by.items() if len({_MAKER_NOISE.sub("", m).upper() for m in ms}) == 1}


def resolve_maker(jan: str, jancode_company: str,
                  prefix_makers: dict[str, str]) -> tuple[str, str]:
    """メーカー名（仕様書 §4）: NST 同プレフィックス → jancode 会社名 → 空。60 字で切る。

    戻り (maker, source)。source は "nst-prefix" / "jancode" / "none"（none は呼び出し側で警告）。
    """
    j = (jan or "").strip()
    if len(j) == 13 and j.isdigit() and prefix_makers.get(j[:7]):
        return prefix_makers[j[:7]][:MAKER_MAX], "nst-prefix"
    company = (jancode_company or "").strip()
    if company:
        return company[:MAKER_MAX], "jancode"
    return "", "none"
