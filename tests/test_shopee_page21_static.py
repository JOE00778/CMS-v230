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
