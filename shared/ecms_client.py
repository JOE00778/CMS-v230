"""ECMS STANDARD EXPRESS API 客户端（规格书 v1.7 · 2024-10）。

只实现**核心发货链 4 接口**（Boss 2026-08-28 拍板）：
    create_shipment  POST /api/manifest        建运单（非幂等！重复调用报重单）
    get_label        POST /api/printLabel      取面单 PDF
    get_tracking     POST /api/getTracking     查追踪事件
    cancel_shipment  POST /api/cancelShipment  取消运单

未实现（同次拍板明确不做）：getRate（运价用三金合同表 shopify/ecms_rates.py，
API 报价不是合同价）、getPickupAvailability / requestPickup（集荷预约）、
CN B2C 清关补录。

配置（元川 `.env` / streamlit secrets，**凭证不进仓库不进 chat**）：
    ECMS_ENV          uat | pro     默认 uat
    ECMS_CLIENT_ID    ECMS 分配的 clientId
    ECMS_TOKEN        ECMS 分配的 Bearer token（UAT 与 PRO 是两套）
    ECMS_ACCOUNT      可选，主账号下的子账号
    ECMS_SERVICE_TYPE Warehouse | Dropoff | Pickup（默认 Warehouse）
    ECMS_SHIPPER_JSON 发件人固定信息 JSON（我方）

⚠️ 与 ECMS 协议相关、文档未定死的两处，默认值待与 ECMS/三金确认：
  - serviceType：文档说"按与 ECMS 的协议固定"，这里默认 Warehouse（交货到 ECMS 东京 GW 仓）
  - reasonForExport：电商销售取 commercial，文档 valid value 原文写作 "commercial (Reseller)"
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import requests
import streamlit as st

UAT_BASE = "http://uat.ecmsglobal.cn:17886/eso/api"
PRO_BASE = "https://ese.ecmsglobal.com/api"
TIMEOUT = 60


class EcmsError(RuntimeError):
    """ECMS 返回非 200（201 参数错 / 500 系统错）或传输失败。"""

    def __init__(self, message: str, status: int | None = None, errors: Any = None):
        super().__init__(message)
        self.status = status
        self.errors = errors


class EcmsNotConfigured(EcmsError):
    """clientId / token 没配。凭证到位前所有接口都会先撞这个。"""


def _secret(name: str, default: str = "") -> str:
    """优先 streamlit secrets，fallback env var。与 shared.n8n_client 一致。"""
    try:
        v = st.secrets.get(name, None)
        if v:
            return str(v)
    except (FileNotFoundError, KeyError):
        pass
    return os.environ.get(name, "") or default


def env_name() -> str:
    return (_secret("ECMS_ENV", "uat") or "uat").strip().lower()


def base_url() -> str:
    return PRO_BASE if env_name() == "pro" else UAT_BASE


def is_configured() -> bool:
    return bool(_secret("ECMS_CLIENT_ID") and _secret("ECMS_TOKEN"))


def shipper_default() -> dict:
    """我方发件人信息（元川 .env 里配一次）。没配就返回空 dict，页面上手填。"""
    raw = _secret("ECMS_SHIPPER_JSON", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def request_time() -> str:
    """ECMS 要求 yyyy-MM-dd'T'HH:mm:ssZ，如 2020-01-19T14:03:11+0800（offset 不带冒号）。"""
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _post(path: str, body: dict) -> dict:
    if not is_configured():
        raise EcmsNotConfigured("ECMS_CLIENT_ID / ECMS_TOKEN 未配置（元川 .env）")

    payload = {
        "requestTime": request_time(),
        "clientId": _secret("ECMS_CLIENT_ID"),
        **body,
    }
    account = _secret("ECMS_ACCOUNT")
    if account:
        payload["account"] = account

    try:
        resp = requests.post(
            f"{base_url()}/{path}",
            json=payload,
            headers={
                "accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_secret('ECMS_TOKEN')}",
            },
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise EcmsError(f"ECMS 请求失败（{path}）: {e}") from e

    try:
        data = resp.json()
    except ValueError as e:
        raise EcmsError(f"ECMS 返回非 JSON（HTTP {resp.status_code}）: {resp.text[:200]}") from e

    status = data.get("status")
    if status != 200:
        raise EcmsError(
            f"ECMS {path} 返回 {status}: {data.get('message', '')}",
            status=status,
            errors=data.get("errors"),
        )
    return data


def _id_body(tracking_no: str = "", shipment_id: str = "") -> dict:
    """三个查询类接口共用：trackingNo / shipmentId 至少给一个。"""
    if not tracking_no and not shipment_id:
        raise EcmsError("trackingNo 与 shipmentId 至少要给一个")
    body: dict[str, Any] = {}
    if tracking_no:
        body["trackingNo"] = tracking_no
    if shipment_id:
        body["shipmentId"] = shipment_id
    return body


# ------------------------------------------------------------------
# 4 个接口
# ------------------------------------------------------------------
def create_shipment(shipment: dict) -> dict:
    """建运单。⚠️ 非幂等：同一 referenceCode 第二次调用 ECMS 会报重单。

    返回 data 对象：{"shipmentId": ..., "boxes": [{"trackingNo": ...}]}
    """
    return _post("manifest", {"shipment": shipment})["data"]


def get_label(tracking_no: str = "", shipment_id: str = "") -> dict:
    """取面单。返回第一个 label 的 {trackingNo, labelUrl, content(base64), fileType}。"""
    data = _post("printLabel", _id_body(tracking_no, shipment_id))["data"]
    labels = data.get("labels") or []
    if not labels:
        raise EcmsError("printLabel 返回空 labels")
    label = labels[0]
    if str(label.get("code")) != "0":
        raise EcmsError(f"面单未就绪（code={label.get('code')}）: {label.get('message', '')}")
    f = label.get("file") or {}
    return {
        "trackingNo": label.get("trackingNo", ""),
        "labelUrl": f.get("labelUrl") or "",
        "content": f.get("content") or "",
        "fileType": f.get("fileType") or "pdf",
        "size": f.get("size") or "",
    }


def get_tracking(tracking_no: str = "", shipment_id: str = "", lang: str = "en") -> list[dict]:
    """查追踪事件，返回扁平事件列表（新到旧由 ECMS 决定，不重排）。

    ⚠️ 规格书响应示例里事件描述字段拼作 `desciption`（文档笔误或 API 真这么拼），
    两种拼法都收。
    """
    body = _id_body(tracking_no, shipment_id)
    body["lang"] = lang
    data = _post("getTracking", body)["data"]
    out: list[dict] = []
    for track in data.get("tracks") or []:
        tno = track.get("trackingNo", "") or tracking_no
        for ev in track.get("events") or []:
            loc = ev.get("activityLocation") or {}
            out.append({
                "trackingNo": tno,
                "code": ev.get("code", ""),
                "reasonCode": ev.get("reasonCode", ""),
                "date": ev.get("date", ""),
                "description": ev.get("description") or ev.get("desciption") or "",
                "remark": ev.get("remark", ""),
                "location": ", ".join(
                    x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x
                ),
            })
    return out


def cancel_shipment(tracking_no: str = "", shipment_id: str = "") -> dict:
    """取消运单（连带取消集荷）。响应只有 status/message，没有 data。"""
    return _post("cancelShipment", _id_body(tracking_no, shipment_id))


# ------------------------------------------------------------------
# 请求体组装
# ------------------------------------------------------------------
def build_shipment(
    *,
    reference_code: str,
    receiver: dict,
    items: list[dict],
    weight_kg: float,
    length_cm: float,
    width_cm: float,
    height_cm: float,
    shipper: dict | None = None,
    service_type: str = "",
    reason_for_export: str = "commercial",
    duty_paid_by: str = "recipient",
    incoterm: str = "DDP",
    ship_date: str = "",
    immediate_label: bool = True,
) -> dict:
    """把页面表单拼成 manifest 的 shipment 对象（单箱，numberOfPieces 固定 1）。

    items 每行：name / description / quantity / price_amount / price_currency /
    weight_kg / origin_country / hscode(可空) / brand(可空)
    """
    box_items = []
    for i, it in enumerate(items, start=1):
        box_items.append({
            "sequenceNumber": i,
            "name": it["name"],
            "description": it.get("description") or it["name"],
            "quantity": int(it["quantity"]),
            "originCountry": it.get("origin_country") or "JP",
            "hscode": it.get("hscode") or "",
            "brand": it.get("brand") or "",
            "price": {
                "amount": round(float(it["price_amount"]), 2),
                "currency": it.get("price_currency") or "JPY",
            },
            "weight": {"value": round(float(it["weight_kg"]), 2), "unit": "KG"},
        })

    shipment = {
        "referenceCode": reference_code,
        "serviceType": service_type or _secret("ECMS_SERVICE_TYPE", "Warehouse"),
        "numberOfPieces": 1,
        "immediateLabel": "true" if immediate_label else "false",
        "boxes": [{
            "sequenceNumber": 1,
            "referenceNumber": reference_code,
            "weight": {"value": round(float(weight_kg), 2), "unit": "KG"},
            "dimension": {
                "length": round(float(length_cm), 2),
                "width": round(float(width_cm), 2),
                "height": round(float(height_cm), 2),
                "unit": "CM",
            },
            "items": box_items,
        }],
        "shipper": shipper if shipper is not None else shipper_default(),
        "receiver": receiver,
        "customs": {
            "incoterm": incoterm,
            "reasonForExport": reason_for_export,
            "dutyBilling": {"paidBy": duty_paid_by},
        },
    }
    if ship_date:
        shipment["shipDate"] = ship_date
    return shipment
