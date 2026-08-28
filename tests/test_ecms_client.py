"""ECMS 客户端离线测试（零外网：requests.post 全部打桩）。

覆盖会**静默出错**的地方——格式错/字段错不会崩，只是 ECMS 那边收到垃圾：
  - requestTime 的 offset 必须无冒号（+0900 不是 +09:00）
  - 追踪事件描述字段规格书拼作 desciption，两种拼法都得收
  - 非 200 状态必须抛错，不能当成功往下走
  - build_shipment 的单位/结构（重量 KG、尺寸 CM、numberOfPieces=1）
"""
import json
import re

import pytest
import requests

from shared import ecms_client as ec


class _FakeResp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("ECMS_CLIENT_ID", "CID001")
    monkeypatch.setenv("ECMS_TOKEN", "tok-abc")
    monkeypatch.setenv("ECMS_ENV", "uat")
    monkeypatch.delenv("ECMS_ACCOUNT", raising=False)
    monkeypatch.delenv("ECMS_SHIPPER_JSON", raising=False)


@pytest.fixture
def captured(monkeypatch):
    """打桩 requests.post，记录出去的请求，返回可设定的响应。"""
    box = {"calls": [], "resp": _FakeResp({"status": 200, "message": "SUCCESS", "data": {}})}

    def fake_post(url, json=None, headers=None, timeout=None):
        box["calls"].append({"url": url, "body": json, "headers": headers})
        return box["resp"]

    monkeypatch.setattr(requests, "post", fake_post)
    return box


# ---------- 配置与鉴权 ----------
def test_未配置凭证直接抛(monkeypatch):
    monkeypatch.delenv("ECMS_CLIENT_ID", raising=False)
    monkeypatch.delenv("ECMS_TOKEN", raising=False)
    assert ec.is_configured() is False
    with pytest.raises(ec.EcmsNotConfigured):
        ec.get_tracking(tracking_no="X")


def test_env_切换_base_url(monkeypatch):
    monkeypatch.setenv("ECMS_ENV", "pro")
    assert ec.base_url() == ec.PRO_BASE
    monkeypatch.setenv("ECMS_ENV", "uat")
    assert ec.base_url() == ec.UAT_BASE


def test_requestTime_offset_不带冒号():
    t = ec.request_time()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}", t), t


def test_公共字段与Bearer头(creds, captured):
    ec.cancel_shipment(tracking_no="EC001")
    call = captured["calls"][0]
    assert call["url"].endswith("/cancelShipment")
    assert call["body"]["clientId"] == "CID001"
    assert "requestTime" in call["body"]
    assert call["headers"]["Authorization"] == "Bearer tok-abc"
    assert "account" not in call["body"]


def test_有子账号才带account(creds, captured, monkeypatch):
    monkeypatch.setenv("ECMS_ACCOUNT", "SUB9")
    ec.cancel_shipment(tracking_no="EC001")
    assert captured["calls"][0]["body"]["account"] == "SUB9"


# ---------- 错误路径 ----------
def test_非200状态抛错(creds, captured):
    captured["resp"] = _FakeResp({"status": 201, "message": "Param error", "errors": [{"code": "E1"}]})
    with pytest.raises(ec.EcmsError) as e:
        ec.cancel_shipment(tracking_no="EC001")
    assert e.value.status == 201
    assert e.value.errors == [{"code": "E1"}]


def test_非JSON响应抛错(creds, captured):
    captured["resp"] = _FakeResp(None, status_code=502, text="<html>bad gateway</html>")
    with pytest.raises(ec.EcmsError, match="非 JSON"):
        ec.cancel_shipment(tracking_no="EC001")


def test_传输异常抛EcmsError(creds, monkeypatch):
    def boom(*a, **kw):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(ec.EcmsError, match="请求失败"):
        ec.cancel_shipment(tracking_no="EC001")


def test_两个ID都不给要拒(creds, captured):
    with pytest.raises(ec.EcmsError, match="至少要给一个"):
        ec.get_tracking()
    assert captured["calls"] == []  # 没发出去


# ---------- 建单 ----------
def test_create_shipment_返回data(creds, captured):
    captured["resp"] = _FakeResp({
        "status": 200, "message": "SUCCESS",
        "data": {"shipmentId": "ESE001", "boxes": [{"sequenceNumber": "1", "trackingNo": "ECESE9"}]},
    })
    data = ec.create_shipment({"referenceCode": "SJ-1001"})
    assert data["shipmentId"] == "ESE001"
    assert data["boxes"][0]["trackingNo"] == "ECESE9"
    assert captured["calls"][0]["body"]["shipment"]["referenceCode"] == "SJ-1001"


# ---------- 面单 ----------
def test_get_label_成功(creds, captured):
    captured["resp"] = _FakeResp({
        "status": 200, "message": "SUCCESS",
        "data": {"labels": [{
            "trackingNo": "ECESE9", "code": "0", "message": "SUCCESS",
            "file": {"size": "10cm*15cm", "type": "url", "fileType": "pdf",
                     "labelUrl": "http://x/label.pdf", "content": None},
        }]},
    })
    label = ec.get_label(tracking_no="ECESE9")
    assert label["labelUrl"] == "http://x/label.pdf"
    assert label["fileType"] == "pdf"
    assert label["content"] == ""  # None 归一成空串，页面不用判 None


def test_get_label_未就绪抛错(creds, captured):
    captured["resp"] = _FakeResp({
        "status": 200, "message": "SUCCESS",
        "data": {"labels": [{"trackingNo": "ECESE9", "code": "1", "message": "creating"}]},
    })
    with pytest.raises(ec.EcmsError, match="面单未就绪"):
        ec.get_label(tracking_no="ECESE9")


# ---------- 追踪 ----------
def test_get_tracking_兼容desciption拼写(creds, captured):
    captured["resp"] = _FakeResp({
        "status": 200, "message": "SUCCESS",
        "data": {"shipmentId": "ESE001", "lang": "en", "tracks": [{
            "trackingNo": "ECESE9",
            "events": [
                {"code": "S06A608", "date": "2020-07-21T23:54:43.000+0800",
                 "desciption": "Parcel return in process",
                 "activityLocation": {"city": "Los Angeles", "country": "United States"}},
                {"code": "S07N706", "date": "2020-07-22T10:00:00.000+0800",
                 "description": "Parcel delivered to consignee",
                 "activityLocation": {"country": "Philippines"}},
            ],
        }]},
    })
    events = ec.get_tracking(tracking_no="ECESE9")
    assert [e["description"] for e in events] == [
        "Parcel return in process", "Parcel delivered to consignee"]
    assert events[0]["location"] == "Los Angeles, United States"
    assert events[1]["location"] == "Philippines"
    assert all(e["trackingNo"] == "ECESE9" for e in events)
    assert captured["calls"][0]["body"]["lang"] == "en"


def test_get_tracking_空事件不炸(creds, captured):
    captured["resp"] = _FakeResp({"status": 200, "message": "SUCCESS", "data": {"tracks": []}})
    assert ec.get_tracking(tracking_no="ECESE9") == []


# ---------- 请求体组装 ----------
def _shipment():
    return ec.build_shipment(
        reference_code="SJ-1001",
        receiver={"country": "PH", "name": "Juan", "city": "Manila",
                  "address1": "1 Ayala Ave", "postCode": "1226", "phone": "09171234567",
                  "email": "juan@example.com"},
        items=[
            {"name": "Face Wash", "description": "cleansing foam 120g", "quantity": 2,
             "price_amount": 980, "price_currency": "JPY", "weight_kg": 0.15,
             "origin_country": "JP", "hscode": "3401.30"},
            {"name": "Lip Balm", "quantity": 1, "price_amount": 500,
             "price_currency": "JPY", "weight_kg": 0.02},
        ],
        weight_kg=0.5, length_cm=25, width_cm=18, height_cm=8,
        shipper={"country": "JP", "name": "Smikie", "city": "Oita",
                 "address1": "x", "postCode": "870-0000", "phone": "0000",
                 "email": "a@b.c"},
    )


def test_build_shipment_结构与单位():
    s = _shipment()
    assert s["numberOfPieces"] == 1
    assert len(s["boxes"]) == 1
    box = s["boxes"][0]
    assert box["referenceNumber"] == "SJ-1001"
    assert box["weight"] == {"value": 0.5, "unit": "KG"}
    assert box["dimension"]["unit"] == "CM"
    assert s["customs"]["dutyBilling"]["paidBy"] == "recipient"
    assert s["customs"]["reasonForExport"] == "commercial"
    assert s["serviceType"] == "Warehouse"


def test_build_shipment_item序号从1且补默认值():
    items = _shipment()["boxes"][0]["items"]
    assert [i["sequenceNumber"] for i in items] == [1, 2]
    # description 缺省回落到 name；originCountry 缺省 JP
    assert items[1]["description"] == "Lip Balm"
    assert items[1]["originCountry"] == "JP"
    assert items[0]["price"] == {"amount": 980.0, "currency": "JPY"}
    assert items[0]["weight"] == {"value": 0.15, "unit": "KG"}


def test_shipper_default_坏JSON不炸(monkeypatch):
    monkeypatch.setenv("ECMS_SHIPPER_JSON", "{not json")
    assert ec.shipper_default() == {}
    monkeypatch.setenv("ECMS_SHIPPER_JSON", '{"country": "JP"}')
    assert ec.shipper_default() == {"country": "JP"}
