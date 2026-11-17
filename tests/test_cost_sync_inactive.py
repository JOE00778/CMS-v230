"""定義原価更新: 取扱中止・メーカー取扱中止・NST 停用は SKIP_INACTIVE（監査 M14 2026-11-17：「廃番」しか見ておらず実データで当たらなかった）。"""
from modules.cost_sync.rules import _is_inactive


def test_inactive_tokens_and_flag():
    assert _is_inactive({}, {"handling_status": "取扱中止"})
    assert _is_inactive({}, {"handling_status": "取扱中", "item_rank": "メーカー取扱中止"})
    assert _is_inactive({"inactive_flag": True}, {"handling_status": "取扱中"})
    assert not _is_inactive({"inactive_flag": False}, {"handling_status": "取扱中", "item_rank": "Aランク"})
