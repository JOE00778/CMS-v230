"""韩国地址切分。テスト住所は行政区部分のみ実データ由来で、番地・号室は架空（PII を持ち込まない）。

ここが静かに壊れると ECMS の Province/City が入れ替わったまま出荷され、
通関で止まる（返送費はこちら持ち）。壊れ方が「例外」ではなく「間違った三段」なので、
一段ずつ値で固定する。
"""
from __future__ import annotations

import pytest

from shared.kr_address import ALIAS, SIDO, SIGUNGU, split, to_ecms


def test_17_시도_全部揃っている():
    """세종특별자치시 はコード上 시군구 の形（3611000000）。コード桁で判定すると丸ごと落ちる。"""
    assert len(SIDO) == 17, SIDO
    assert "세종특별자치시" in SIDO


def test_도_아래의_시と구は市と詳細に割れる():
    r = to_ecms("경기도 성남시 분당구 판교로 100 101동 202호")
    assert r["province"] == "경기도"
    assert r["city"] == "경기도 성남시"          # 市は시まで
    assert r["address"].startswith("분당구 ")    # 구は詳細へ
    assert r["ok"]


def test_광역시の구は市に入れず詳細へ():
    r = to_ecms("부산광역시 서구 대신공원로 10 302호")
    assert (r["province"], r["city"]) == ("부산광역시", "부산광역시")
    assert r["address"].startswith("서구 ")


def test_도_아래の시で구が無い場合():
    r = to_ecms("충청남도 공주시 관골2길 24-29 503호")
    assert (r["province"], r["city"]) == ("충청남도", "충청남도 공주시")
    assert r["address"].startswith("관골2길")


def test_세종は単層制なので市に시도自身が入る():
    r = to_ecms("세종특별자치시 고운동 2111 101동 106호")
    assert (r["province"], r["city"]) == ("세종특별자치시", "세종특별자치시")
    assert r["address"].startswith("고운동")
    assert r["ok"]


def test_略称は正式名に正規化される():
    r = to_ecms("대구 달서구 대곡동 46 102동 2305호")
    assert r["province"] == "대구광역시"
    assert r["how"] == "alias:대구"
    assert r["address"].startswith("달서구 ")


def test_切れなければ埋めずに_ok_False():
    """推測で埋めない——page 側で赤表示して人が直す前提。"""
    r = to_ecms("주소가 아닌 문자열")
    assert r == {"province": None, "city": None, "address": "주소가 아닌 문자열",
                 "how": "no_sido", "ok": False}
    assert to_ecms("")["ok"] is False
    assert to_ecms(None)["ok"] is False


def test_括弧の補足は詳細地址に残す():
    """실측 311 件中 149 件（48%）に「( 법정동, 건물명 )」が付く。配達に効くので落とさない。"""
    r = to_ecms("경기도 성남시 분당구 돌마로486번길 7 210동 1202호 ( 서현동, 효자촌 )")
    assert "( 서현동, 효자촌 )" in r["address"]


def test_長い시군구が短いものより先に当たる():
    """'성남시' が先に当たると '분당구' が詳細に残らず City が壊れる。"""
    assert SIGUNGU["경기도"].index("성남시 분당구") < SIGUNGU["경기도"].index("성남시")


@pytest.mark.parametrize("addr,province", [
    ("서울특별시 강북구 번동 462-125 1층", "서울특별시"),
    ("인천광역시 연수구 송도동 23-4 202동", "인천광역시"),
    ("제주특별자치도 제주시 첨단로 242", "제주특별자치도"),
    ("강원특별자치도 춘천시 중앙로 1", "강원특별자치도"),
    ("전북특별자치도 전주시 완산구 효자로 225", "전북특별자치도"),
])
def test_改称された시도も現行名で通る(addr, province):
    """강원도→강원특별자치도・전라북도→전북특별자치도 の改称後の名前で来る。"""
    assert to_ecms(addr)["province"] == province


def test_旧名の略称も受ける():
    assert ALIAS["강원도"] == "강원특별자치도"
    assert to_ecms("강원도 춘천시 중앙로 1")["province"] == "강원특별자치도"
