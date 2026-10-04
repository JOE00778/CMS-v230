"""shared.hs_ai（AI の HS 判定）と item_register.cross_check_hs。外部呼出はモック。"""
import json
import urllib.error

from shared import hs_ai
from shared import item_register as R


def _reply(rows):
    return lambda body: "```json\n" + json.dumps(rows, ensure_ascii=False) + "\n```"


def test_valid_codes_are_hs2022_from_2026_schedule():
    assert {"340250", "330530", "330590", "960330", "960810"} <= hs_ai.VALID_HS6
    assert "340220" not in hs_ai.VALID_HS6                      # HS2022 で廃止
    from shared.hs_classify import CAT_TO_HS
    assert {hs for hs, _ in CAT_TO_HS.values()} <= hs_ai.VALID_HS6   # 規則側のコードも全部実在


def test_judge_maps_ids_back_and_rejects_codes_not_in_schedule():
    items = [("4900000000001", "A 化粧水", "X"), ("4900000000002", "B 洗剤", ""), ("4900000000003", "C", "")]
    post = _reply([{"id": 0, "hs6": "3304.99", "en": "Skin lotion", "why": "化粧水", "conf": 0.9},
                   {"id": 1, "hs6": "340220", "en": "Detergent", "why": "旧コード"}])
    out = hs_ai.judge(items, post=post, sleep=lambda s: None)
    assert out["4900000000001"].hs == "330499" and out["4900000000001"].en == "Skin lotion"
    assert "品目表に無い" in out["4900000000002"]
    assert "この商品がない" in out["4900000000003"]


def test_judge_retries_then_reports_failure_per_item():
    calls = []

    def post(body):
        calls.append(1)
        raise urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    out = hs_ai.judge([("4900000000001", "A", "")], post=post, sleep=lambda s: None)
    assert len(calls) == 3 and "503" in out["4900000000001"]


def test_judge_batches_of_25():
    seen = []

    def post(body):
        n = body["messages"][1]["content"].count("id=")
        seen.append(n)
        return json.dumps([{"id": i, "hs6": "330499", "en": "x", "why": "y"} for i in range(n)])
    items = [(str(i), "x", "") for i in range(60)]
    assert len(hs_ai.judge(items, post=post, sleep=lambda s: None)) == 60
    assert seen == [25, 25, 10]


def test_cross_check_agree_change_and_error():
    rows = [{"JANコード": "1", "アイテム名": "化粧水 200ml", "メーカー名": "X"},
            {"JANコード": "2", "アイテム名": "流し前髪キープコーム 9mL"},
            {"JANコード": "3", "アイテム名": "謎の品"}]
    extra = {"1": {"_hs": "330499", "_name_en": "Lotion"}, "2": {"_hs": "", "_name_en": ""},
             "3": {"_hs": "961511", "_name_en": "Comb"}}
    ai = {"1": hs_ai.AiHs("330499", "Skin lotion", "化粧水", 0.9),
          "2": hs_ai.AiHs("330590", "Hair styling preparation", "整髪料", 0.8),
          "3": "HTTPError: 503"}
    stats = {}
    issues = R.cross_check_hs(rows, extra, stats, lambda items: ai)
    assert stats == {"ai_agree": 1, "ai_changed": 1, "ai_error": 1}
    assert extra["1"]["hs_check"] == "一致" and extra["1"]["_name_en"] == "Lotion"      # 一致は触らない
    assert extra["2"]["_hs"] == "330590" and extra["2"]["hs_rule"] == ""
    assert extra["2"]["_name_en"] == "Hair styling preparation, 9ml"
    assert extra["3"]["_hs"] == "961511" and extra["3"]["hs_check"] == "AI 失敗"         # 失敗は規則のまま
    msgs = {i.jan: i.message for i in issues}
    assert set(msgs) == {"2", "3"} and "AI=330590" in msgs["2"] and "整髪料" in msgs["2"]
    frame = R.to_frame(rows, extra)
    assert frame[1]["HS"] == "330590" and frame[1]["HS 判定"] == "AI（規則=なし）"
