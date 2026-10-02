"""商品登録 v2 の選択肢（仕入先 / 大分類・中分類 / 商品担当者 / 固定値）と NST 照合。

正の源は NST（Boss 2026-10-03「すべて NST が核心」）。NST に無いものだけ PG で補う。
- 仕入先: PG `nst.vendor_master`（NST Vendor の毎日 06:50 全件鏡像）· page10 と共有
- 大分類・中分類: SuiteQL で item の実在組合せ。失敗時は 2026-10-02 実測の定数
- 既存 JAN: SuiteQL item.upccode（NST 全体）。失敗時は PG `nst.item_master_raw`（輸出部門のみ）
- メーカー名: JAN 先頭 7 桁が NST 既存品で 1 種類に決まればそれ → jancode 会社名 → 空

DB/Streamlit 非依存（conn と suiteql は引数で受ける）。キャッシュは呼び出し側（page）で。
"""
from __future__ import annotations

from dataclasses import dataclass

OWNERS: tuple[str, ...] = ("037 米澤和敏", "043 徐越", "005 川崎里子", "079 隋艶偉")
FIXED: dict[str, str] = {"取扱区分": "取扱中", "商品ランク": "NEW", "部門": "輸出事業",
                         "仕入区分": "輸出専用", "発注通貨": "日本円"}
MAKER_MAX = 60
_JAN_CHUNK = 200

# 2026-10-02 SuiteQL 実測の (大分類, 中分類) 組合せ（中分類は件数の多い順）。
# 61 組・中分類 32（2026-10-03 元川で再実測し全件一致を確認）。
# SuiteQL 不通時のフォールバック。NST 側で分類を増やしたらここも直す。
FALLBACK_MIDDLE_BY_LARGE: dict[str, tuple[str, ...]] = {
    "ASEAN": ("日用消耗品", "日用品雑貨", "輸出専用", "パール", "加工食品", "衛生日用品",
              "ホビー", "その他（生活雑貨）"),
    "USA": ("日用品雑貨", "日用消耗品", "DIY・工具", "ホビー", "アウトドア用品", "スポーツ・健康",
            "季節家電"),
    "アウトドア・防災": ("アウトドア用品", "スポーツ・健康", "防災", "その他（アウトドア・防災）",
                         "保安整備"),
    "カー・バイク用品": ("その他（カー・バイク用品）", "アクセサリー", "カーエレクトロニクス",
                         "保安整備", "その他", "生活家電", "洗車用品", "自転車用品"),
    "パール": ("パール",),
    "中国": ("日用消耗品", "日用品雑貨", "パール", "加工食品", "アクセサリー", "アウトドア用品",
             "その他（生活雑貨）", "輸出専用", "ホビー", "衛生日用品"),
    "生活雑貨": ("DIY・工具", "日用品雑貨", "日用消耗品", "スポーツ・健康", "ホビー",
                 "ガーデン・エクステリア", "その他（生活雑貨）", "衛生日用品", "生活家電", "季節家電",
                 "季節雑貨", "加工食品", "雑貨", "日用品", "工具", "その他", "洗濯機・洗濯乾燥機"),
    "食品": ("加工食品", "健康食品", "菓子", "嗜好品（茶・コーヒー）", "調味料"),
}


@dataclass(frozen=True)
class Choices:
    suppliers: tuple[str, ...]                      # "0001 ベストアンサ株式会社"
    middle_by_large: dict[str, tuple[str, ...]]     # 大分類 -> 中分類（実在組合せ）
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


def load_categories(*, suiteql=None) -> tuple[dict[str, tuple[str, ...]], list[str]]:
    """NST item の (大分類, 中分類) 実在組合せ（部門で絞らない＝NST 全体）。

    戻り: ({大分類: (中分類, ...)}, 警告)。大分類・中分類とも件数の多い順。
    SuiteQL 失敗・0 件なら (FALLBACK_MIDDLE_BY_LARGE, [警告])。
    """
    q = suiteql or _default_suiteql()
    try:
        rows = q(
            "SELECT BUILTIN.DF(custitem_fb_large_category) AS large, "
            "BUILTIN.DF(custitem_fb_middle_category) AS middle, COUNT(*) AS n "
            "FROM item "
            "WHERE custitem_fb_large_category IS NOT NULL "
            "AND custitem_fb_middle_category IS NOT NULL "
            "GROUP BY BUILTIN.DF(custitem_fb_large_category), "
            "BUILTIN.DF(custitem_fb_middle_category)",
            limit=1000)
    except Exception as e:  # noqa: BLE001 — テンプレ DL を止めない
        return (dict(FALLBACK_MIDDLE_BY_LARGE),
                [f"NST 分類の取得に失敗したため 2026-10-02 時点の定数を使用: {e}"])
    pairs = [((r.get("large") or "").strip(), (r.get("middle") or "").strip(),
              int(r.get("n") or 0)) for r in rows]
    pairs = [p for p in pairs if p[0] and p[1]]
    if not pairs:
        return (dict(FALLBACK_MIDDLE_BY_LARGE),
                ["NST 分類が 0 件だったため 2026-10-02 時点の定数を使用"])
    total: dict[str, int] = {}
    for lg, _, n in pairs:
        total[lg] = total.get(lg, 0) + n
    out: dict[str, tuple[str, ...]] = {}
    for lg in sorted(total, key=lambda k: -total[k]):
        mids = sorted((p for p in pairs if p[0] == lg), key=lambda p: -p[2])
        out[lg] = tuple(dict.fromkeys(p[1] for p in mids))
    return out, []


def load_choices(conn, *, suiteql=None) -> Choices:
    suppliers = load_suppliers(conn)
    middle_by_large, warns = load_categories(suiteql=suiteql)
    if not suppliers:
        warns.append("仕入先マスタが空です。page 27「📥 NST 取得データ」→ "
                     "「🏭 仕入先マスタ」で更新してください。")
    return Choices(suppliers=tuple(suppliers), middle_by_large=middle_by_large,
                   warnings=tuple(warns))


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def existing_jans(jans: list[str], *, conn=None, suiteql=None) -> tuple[set[str], list[str]]:
    """NST に既にある JAN（SuiteQL item.upccode · NST 全体）。

    SuiteQL が 1 チャンクでも失敗したら PG `nst.item_master_raw.jan` で全件照合し直し、
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
            rows = q("SELECT upccode FROM item WHERE upccode IN ("
                     + ",".join(_quote(j) for j in chunk) + ")", limit=1000)
            found.update(str(r.get("upccode") or "").strip() for r in rows)
        return found & set(uniq), []
    except Exception as e:  # noqa: BLE001
        sq_err = str(e)
    if conn is None:
        return set(), [f"NST 既存 JAN 照合に失敗（SuiteQL: {sq_err}）。PG 代替も無いため"
                       f"重複チェック未実施 {len(uniq)} 件"]
    try:
        rows = conn.execute(
            "SELECT DISTINCT jan FROM nst.item_master_raw WHERE jan IN ("
            + ",".join("?" for _ in uniq) + ")", tuple(uniq)).fetchall()
    except Exception as e:  # noqa: BLE001
        return set(), [f"NST 既存 JAN 照合に失敗（SuiteQL: {sq_err} / PG: {e}）。"
                       f"重複チェック未実施 {len(uniq)} 件"]
    found = {str(r["jan"]).strip() for r in rows}
    return found & set(uniq), [f"SuiteQL 失敗のため PG nst.item_master_raw で照合"
                               f"（NST 全体ではなく輸出部門のみの照合）: {sq_err}"]


def load_prefix_makers(conn) -> dict[str, str]:
    """13 桁 JAN の先頭 7 桁 → NST 既存 maker（そのプレフィックスで maker が 1 種類のものだけ）。"""
    rows = conn.execute(
        "SELECT SUBSTR(jan, 1, 7) AS prefix, MIN(TRIM(maker)) AS maker "
        "FROM nst.item_master_raw "
        "WHERE LENGTH(jan) = 13 AND maker IS NOT NULL AND TRIM(maker) <> '' "
        "GROUP BY SUBSTR(jan, 1, 7) "
        "HAVING COUNT(DISTINCT TRIM(maker)) = 1"
    ).fetchall()
    return {r["prefix"]: r["maker"] for r in rows}


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
