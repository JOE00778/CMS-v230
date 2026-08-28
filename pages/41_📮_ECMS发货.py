"""模块 #41 ECMS 发货 · 建运单 / 面单 / 追踪 / 取消（Boss 2026-08-28 拍板）.

对接 ECMS STANDARD EXPRESS API v1.7 的核心发货链 4 接口，客户端在
shared/ecms_client.py。**只在元川跑**——凭证只配在元川 .env，本机无凭证时
页面正常打开、按钮点了报"未配置"。

留痕：ecms_shipment（一单一行，reference_code 幂等键）/ ecms_tracking_event。
⚠️ ECMS 建单接口非幂等：同一 referenceCode 建两次会被判重单，故建单前先查本地表。

不做（同次拍板）：运价查询（用三金合同表 shopify/ecms_rates.py）、集荷预约、
Shopify 订单自动流入与 tracking 回写。发件人信息配在元川 ECMS_SHIPPER_JSON。
"""
from __future__ import annotations

from base64 import b64decode

import pandas as pd
import streamlit as st

from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("ECMS 发货"), page_icon="📮", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
from shared import ecms_client as ec
from shared import ecms_store as store

require_password()
inject_theme()
lang_selector()

st.title(t("📮 ECMS 发货"))

_ENV = ec.env_name()
if _ENV == "pro":
    st.error(t("⚠️ 当前连的是 ECMS **生产环境**——建单与取消都是真实运单，操作前确认单号。"))
else:
    st.caption(t("环境：UAT 测试环境（建单不产生真实运单）"))
if not ec.is_configured():
    st.warning(t("ECMS_CLIENT_ID / ECMS_TOKEN 未配置——本页可浏览，调接口会报错。凭证配在元川 .env。"))


def _user() -> str:
    u = st.session_state.get("__lark_user") or {}
    return u.get("email") or u.get("name") or ""


tab_create, tab_label, tab_track, tab_cancel, tab_log = st.tabs(
    [t("① 建运单"), t("② 面单"), t("③ 追踪"), t("④ 取消"), t("📋 记录")]
)

# ============================================================
# ① 建运单
# ============================================================
with tab_create:
    shipper = ec.shipper_default()
    if not shipper:
        st.warning(t("发件人未配置（元川 .env 的 ECMS_SHIPPER_JSON）——建单会被 ECMS 拒。"))
    else:
        with st.expander(t("发件人（来自 ECMS_SHIPPER_JSON）"), expanded=False):
            st.json(shipper)

    ref = st.text_input(t("我方单号 referenceCode"), key="c_ref",
                        help=t("用 Shopify 订单号。ECMS 建单非幂等，同号只能建一次。"))

    st.subheader(t("收件人"))
    c1, c2, c3 = st.columns(3)
    r_name = c1.text_input(t("姓名"), key="c_rname")
    r_phone = c2.text_input(t("电话"), key="c_rphone")
    r_email = c3.text_input(t("邮箱"), key="c_remail")
    c4, c5, c6 = st.columns(3)
    r_country = c4.text_input(t("国家（2位ISO）"), value="PH", key="c_rcountry")
    r_state = c5.text_input(t("州/省"), key="c_rstate")
    r_city = c6.text_input(t("城市"), key="c_rcity")
    c7, c8 = st.columns([3, 1])
    r_addr1 = c7.text_input(t("地址1"), key="c_raddr1")
    r_post = c8.text_input(t("邮编"), key="c_rpost")
    r_addr2 = st.text_input(t("地址2（可空）"), key="c_raddr2")

    st.subheader(t("包装箱"))
    b1, b2, b3, b4 = st.columns(4)
    w_kg = b1.number_input(t("实重 kg"), min_value=0.01, value=0.5, step=0.1, key="c_w")
    l_cm = b2.number_input(t("长 cm"), min_value=1.0, value=25.0, step=1.0, key="c_l")
    wd_cm = b3.number_input(t("宽 cm"), min_value=1.0, value=18.0, step=1.0, key="c_wd")
    h_cm = b4.number_input(t("高 cm"), min_value=1.0, value=8.0, step=1.0, key="c_h")
    st.caption(t("体积重 = 长×宽×高/6000，计费重取实重与体积重的大者") +
               f" · {t('体积重')} {l_cm * wd_cm * h_cm / 6000:.2f} kg")

    st.subheader(t("申报明细"))
    items_df = st.data_editor(
        pd.DataFrame([{"name": "", "description": "", "quantity": 1, "price_amount": 0.0,
                       "price_currency": "JPY", "weight_kg": 0.0, "origin_country": "JP",
                       "hscode": "", "brand": ""}]),
        num_rows="dynamic", use_container_width=True, key="c_items",
    )

    s1, s2, s3 = st.columns(3)
    service_type = s1.selectbox(t("交货方式 serviceType"), ["Warehouse", "Dropoff", "Pickup"],
                                key="c_stype", help=t("按与 ECMS 的协议固定，未确认前用 Warehouse"))
    reason = s2.selectbox(t("出口理由"), ["commercial", "gift", "sample", "personal,not for resale"],
                          key="c_reason")
    duty_by = s3.selectbox(t("关税由谁付"), ["recipient", "shipper", "thirdParty"], key="c_duty")

    def _receiver() -> dict:
        d = {"country": r_country.strip().upper(), "name": r_name.strip(), "city": r_city.strip(),
             "address1": r_addr1.strip(), "postCode": r_post.strip(), "phone": r_phone.strip(),
             "email": r_email.strip()}
        if r_state.strip():
            d["state"] = r_state.strip()
        if r_addr2.strip():
            d["address2"] = r_addr2.strip()
        return d

    def _items() -> list[dict]:
        return [r for r in items_df.to_dict("records") if str(r.get("name", "")).strip()]

    missing = [k for k, v in {
        t("我方单号"): ref.strip(), t("收件人姓名"): r_name.strip(), t("城市"): r_city.strip(),
        t("地址1"): r_addr1.strip(), t("邮编"): r_post.strip(), t("电话"): r_phone.strip(),
        t("邮箱"): r_email.strip(),
    }.items() if not v]
    if not _items():
        missing.append(t("申报明细"))

    if missing:
        st.info(t("待填：") + "、".join(missing))
    else:
        payload = ec.build_shipment(
            reference_code=ref.strip(), receiver=_receiver(), items=_items(),
            weight_kg=w_kg, length_cm=l_cm, width_cm=wd_cm, height_cm=h_cm,
            shipper=shipper, service_type=service_type, reason_for_export=reason,
            duty_paid_by=duty_by,
        )
        with st.expander(t("发出去的 JSON（发前核一遍）"), expanded=False):
            st.json(payload)

        existing = store.fetch_shipment(ref.strip())
        if existing and existing["status"] == "created":
            st.error(t("这个单号已建过运单：") + f" trackingNo={existing['tracking_no']}"
                     + t("。ECMS 不接受重复建单，要重建先在 ④ 取消。"))
        else:
            ok = st.checkbox(t("已核对收件人与申报明细，确认建单（不可撤销，只能事后取消）"),
                             key="c_confirm")
            if st.button(t("建运单"), type="primary", disabled=not ok, key="c_submit"):
                base = dict(reference_code=ref.strip(), receiver_name=r_name.strip(),
                            receiver_country=r_country.strip().upper(),
                            request_json=payload, created_by=_user())
                try:
                    data = ec.create_shipment(payload)
                except ec.EcmsError as e:
                    store.save_shipment(**base, status="failed",
                                        response_json={"error": str(e), "errors": e.errors})
                    st.error(t("建单失败：") + str(e))
                else:
                    box = (data.get("boxes") or [{}])[0]
                    store.save_shipment(**base, status="created",
                                        shipment_id=data.get("shipmentId") or "",
                                        tracking_no=box.get("trackingNo") or "",
                                        label_url=(box.get("file") or {}).get("labelUrl") or "",
                                        response_json=data)
                    st.success(t("建单成功") + f" · trackingNo `{box.get('trackingNo')}`"
                               f" · shipmentId `{data.get('shipmentId')}`")
                    st.json(data)

# ============================================================
# ② 面单
# ============================================================
with tab_label:
    lt = st.text_input(t("trackingNo"), key="l_tno")
    ls = st.text_input(t("或 shipmentId"), key="l_sid")
    if st.button(t("取面单"), key="l_get", disabled=not (lt.strip() or ls.strip())):
        try:
            label = ec.get_label(tracking_no=lt.strip(), shipment_id=ls.strip())
        except ec.EcmsError as e:
            st.error(str(e))
        else:
            st.write(f"trackingNo `{label['trackingNo']}` · {label['size']} · {label['fileType']}")
            if label["content"]:
                try:
                    st.download_button(
                        t("下载面单"), b64decode(label["content"]),
                        file_name=f"ECMS_{label['trackingNo']}.{label['fileType']}",
                        mime="application/pdf", key="l_dl")
                except Exception as e:  # base64 坏了不该整页崩
                    st.error(t("面单内容解码失败：") + str(e))
            if label["labelUrl"]:
                st.link_button(t("ECMS 面单下载链接（带签名，会过期）"), label["labelUrl"])

# ============================================================
# ③ 追踪
# ============================================================
with tab_track:
    tt = st.text_input(t("trackingNo"), key="t_tno")
    ts = st.text_input(t("或 shipmentId"), key="t_sid")
    if st.button(t("查追踪"), key="t_get", disabled=not (tt.strip() or ts.strip())):
        try:
            events = ec.get_tracking(tracking_no=tt.strip(), shipment_id=ts.strip())
        except ec.EcmsError as e:
            st.error(str(e))
        else:
            if not events:
                st.info(t("暂无事件（ECMS 收到电子舱单后才有第一条 S01N100）"))
            else:
                n = store.save_events(events)
                st.caption(t("已落库") + f" {n} " + t("条（重复事件自动跳过）"))
                st.dataframe(
                    pd.DataFrame(events)[["date", "code", "description", "location", "remark"]],
                    use_container_width=True, hide_index=True)

    hist_tno = (tt or ts).strip()
    if hist_tno:
        rows = store.local_events(hist_tno)
        if rows:
            with st.expander(t("本地已存事件") + f"（{len(rows)}）", expanded=False):
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ============================================================
# ④ 取消
# ============================================================
with tab_cancel:
    st.caption(t("取消运单会连带取消集荷。已实际交货的单取消不了，以 ECMS 返回为准。"))
    ct = st.text_input(t("trackingNo"), key="x_tno")
    cs = st.text_input(t("或 shipmentId"), key="x_sid")
    cref = st.text_input(t("我方单号（可空，填了才会同步更新本地状态）"), key="x_ref")
    ok_x = st.checkbox(t("确认取消这一单"), key="x_confirm")
    if st.button(t("取消运单"), type="primary", key="x_submit",
                 disabled=not (ok_x and (ct.strip() or cs.strip()))):
        try:
            resp = ec.cancel_shipment(tracking_no=ct.strip(), shipment_id=cs.strip())
        except ec.EcmsError as e:
            st.error(t("取消失败：") + str(e))
        else:
            if cref.strip():
                store.update_status(cref.strip(), "cancelled")
            st.success(t("已取消") + f" · {resp.get('message', '')}")

# ============================================================
# 📋 记录
# ============================================================
with tab_log:
    rows = store.recent_shipments()
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info(t("还没有建单记录"))
