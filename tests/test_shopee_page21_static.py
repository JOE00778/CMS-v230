"""page21 の静的回帰（UI 経由コードはテストで到達しないので文面で固定する）。

2026-09-07 の教訓：commit メッセージに書いたのに実装していなかった／市場リストが
2026-05-17 の Boss 校正（ID→BR）を 4 ヶ月追従していなかった。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "pages" / "21_🚀_Shopee上架.py"
SRC = PAGE.read_text(encoding="utf-8")


def test_page_parses():
    ast.parse(SRC)


def test_market_list_matches_actual_20_shops():
    """実店は 7 国 = PH/MY/SG/TH/VN/TW/BR（ID なし・BR あり）。"""
    m = re.search(r'st\.selectbox\(\s*"🌏 Shopee 站点",\s*\[([^\]]+)\]', SRC)
    assert m, "市场 selectbox 不见了"
    markets = re.findall(r'"([A-Z]{2})"', m.group(1))
    assert markets == ["PH", "MY", "SG", "TH", "VN", "TW", "BR"]


def test_has_review_tab_wired():
    assert 'tab_auto, tab_review, tab_refs, tab_history = st.tabs(' in SRC
    assert '"✅ 待确认"' in SRC
    assert "with tab_review:" in SRC


def test_review_tab_reads_draft_tables_and_writes_via_write_conn():
    assert 'shopee.listing_draft' in SRC and 'shopee.listing_draft_sku' in SRC
    assert "get_connection" in SRC, "写操作要拿写连接，不能用 get_readonly_connection"
    # 批准/拒绝/保存 三个写路径都走 _draft_write（带 commit/rollback）
    assert SRC.count("_draft_write(") >= 4  # 1 def + 3 calls


def test_mock_draft_cannot_be_approved():
    """<MOCK> 占位文案不能进发布队列。"""
    assert "startswith(\"<MOCK>\")" in SRC
    assert "disabled=is_mock" in SRC


# ---- 状态与文案按 UI 语言显示（Boss 2026-09-09「匹配为中文和日文UI」）----
I18N = (Path(__file__).resolve().parent.parent / "shared" / "i18n.py").read_text(encoding="utf-8")
COLS = (Path(__file__).resolve().parent.parent / "shared" / "i18n_columns.py").read_text(encoding="utf-8")


def test_status_values_never_shown_raw():
    for raw in ('"待确认 draft"', '"已批准 approved"', '"已拒绝 rejected"', '"已发布 published"'):
        assert raw not in SRC, raw
    assert "_STATUS_LABELS" in SRC and "format_func=_st_label" in SRC
    assert '"status": _st_label(d["status"])' in SRC


def test_review_tab_strings_have_japanese():
    for zh, ja in [("草稿待确认", "確認待ち"), ("已批准", "承認済み"), ("已拒绝", "却下"), ("已发布", "公開済み"),
                   ("✅ 待确认", "✅ 確認待ち"), ("状态", "状態"), ("命中 {n} 个 SPU", "該当 {n} SPU")]:
        assert f'"{zh}": "{ja}"' in I18N, zh
    # tab 内不再有裸 t()/裸中文按钮：关键控件都过 tt()
    for s in ('tt("状态")', 'tt("打开一个 SPU")', 'tt("❌ 拒绝")', 'tt("✅ 批准（进发布队列）")'):
        assert s in SRC, s


def test_review_tables_use_localize_df_and_columns_have_ja():
    assert SRC.count("localize_df(") >= 3     # 历史 tab 原有 1 + 待确认 tab 两张表
    for col in ("spu_key", "status", "title", "category_id", "sku_count", "approved_by", "name_jp", "cost_jpy", "weight_g", "image"):
        assert f'"{col}":' in COLS, f"i18n_columns 缺列键 {col}"
    # 表格行字典必须用英文列键（localize_df 按英文键翻 ja/zh/en）
    for k in ('"spu_key": d["spu_key"]', '"status": _st_label(d["status"])', '"title": (d["title"]',
              '"jan": s_["jan"]', '"name_jp": (s_.get("name_jp")', 'row["image"] ='):
        assert k in SRC, k
