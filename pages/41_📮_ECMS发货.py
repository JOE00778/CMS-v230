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

import io
import tempfile
from base64 import b64decode
from pathlib import Path

import pandas as pd
import streamlit as st

from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("ECMS 发货"), page_icon="📮", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
from shared import ecms_client as ec
from shared import ecms_store as ecms_store
from shared import coupang_client as cp
from shared import coupang_ecms as ce
from shared import coupang_store as store_cp
from shared import coupang_to_ecms_xlsx as X

require_password()
inject_theme()
lang_selector()

st.title(t("📮 ECMS 发货"))


def _col_letter(i: int) -> str:
    """0 → A, 25 → Z, 26 → AA。Coupang の書き出しを列位置で読むため。"""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out

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


tab_cp, tab_sf, tab_create, tab_label, tab_track, tab_cancel, tab_log = st.tabs(
    [t("🇰🇷 Coupang"), t("🛒 Shopify"), t("✍️ 手工建单"), t("🏷️ 面单"),
     t("🚚 追踪"), t("🚫 取消"), t("📋 记录")]
)

# ============================================================
# ① Coupang（KR）· 拉取 → 核对 → 发 ECMS
# ============================================================
with tab_cp:
    mode = st.radio(
        t("模式"), [t("📄 Excel 转换（现在用）"), t("🔌 API 直连（凭证到位后）")],
        horizontal=True, key="cp_mode",
        help=t("ECMS 的 API 环境还没好，先用 Excel：上传 Coupang 下载的订单，出 ECMS 上传文件"))
    excel_mode = mode.startswith("📄")

    if excel_mode:
        st.caption(t("Coupang 后台下载的订单 Excel → ECMS 上传用 Excel。"
                     "规则照运营 2026-09-02 的实际文件核对过（37 行逐格一致）。"))
        up_o = st.file_uploader(t("Coupang 订单 Excel"), type=["xlsx"], key="cp_orders")
        e1, e2 = st.columns(2)
        seq = e1.number_input(t("头程运单号起始序号"), min_value=1, max_value=99999, value=1,
                              key="cp_seq", help=t("ECLBF + 日期 + 5位序号。接着上次的号往下"))
        ship_day = e2.date_input(t("运单号日期"), key="cp_day")

        if up_o is not None:
            try:
                df_o = pd.read_excel(up_o, dtype=str).fillna("")
            except Exception as e:
                st.error(t("读取失败：") + str(e))
            else:
                # 列名ではなく**位置**で取る（Coupang の書き出しは列名が英/韓で揺れる）
                orders = []
                for _, raw in df_o.iterrows():
                    vals = list(raw.values)
                    orders.append({_col_letter(i): str(v).strip()
                                   for i, v in enumerate(vals)})
                orders = [o for o in orders if o.get(X.C_ORDER_NO)]
                st.write(t("读到订单") + f" {len(orders)}")

                pm = store_cp.product_map()
                jans = sorted({X.split_sku(o.get(X.C_SKU, ""))[0] for o in orders})
                nm = store_cp.nst_master_map(jans)
                rows = X.convert(orders, pm, nm, start_seq=int(seq), on=ship_day)

                view = []
                for r in rows:
                    miss = X.missing(r)
                    view.append({"缺": "、".join(miss) if miss else "",
                                 **{f"{c} {X.HEADERS[X.COLUMNS.index(c)].split(chr(10))[0]}":
                                    r.get(c, "")
                                    for c in ("B", "C", "R", "S", "V", "W", "X", "Y", "AA",
                                              "AE", "AG", "AH", "AJ", "AO", "AP")}})
                bad = sum(1 for v in view if v["缺"])
                if bad:
                    st.warning(t("有缺项的行") + f"：{bad} / {len(rows)}　"
                               + t("（商品主档没登记的 SKU 会缺英文品名/HScode）"))
                st.dataframe(pd.DataFrame(view), use_container_width=True, hide_index=True)

                buf = io.BytesIO()
                tmp = Path(tempfile.gettempdir()) / f"ecms_{ship_day:%Y%m%d}.xlsx"
                X.to_xlsx(rows, tmp)
                buf.write(tmp.read_bytes())
                st.download_button(
                    t("下载 ECMS 上传文件"), buf.getvalue(),
                    file_name=f"ecms上传-{ship_day:%m%d}.xlsx", type="primary",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="cp_dl")

        # ---- 商品主档 ----
        pm_now = store_cp.product_map()
        with st.expander(t("商品主档") + f"（{len(pm_now)}）", expanded=not pm_now):
            st.caption(t("按 SKU（4901616011007_3 这种）登记。同一 JAN 不同规格是不同行。"
                         "英文品名 / HScode / Product ID / 옵션 ID 来自这里；"
                         "重量和品牌从 NST 主档自动查，品牌要覆盖时填 brand 列。"))
            up_p = st.file_uploader(t("商品信息 Excel"), type=["xlsx"], key="cp_prod")
            if up_p is not None and st.button(t("导入"), key="cp_prod_go"):
                try:
                    df_p = pd.read_excel(up_p, dtype=str).fillna("")
                except Exception as e:
                    st.error(t("读取失败：") + str(e))
                else:
                    def _c(row, *names):
                        for k, v in row.items():
                            if any(n.lower() in str(k).strip().lower() for n in names):
                                s = str(v).strip()
                                if s and s.lower() != "nan":
                                    return s
                        return ""

                    recs = []
                    for _, raw in df_p.iterrows():
                        row = raw.to_dict()
                        sku = _c(row, "JAN", "SKU", "vendor")
                        if not sku:
                            continue
                        jan, pack = X.split_sku(sku)
                        recs.append({"sku": sku, "jan": jan, "pack": pack,
                                     "name_en": _c(row, "英文名称", "英文", "name_en"),
                                     "brand": _c(row, "brand", "品牌"),
                                     "hscode": _c(row, "HScode", "HS"),
                                     "product_id": _c(row, "Product ID"),
                                     "option_id": _c(row, "옵션", "option")})
                    n = store_cp.upsert_products(recs)
                    st.success(t("导入") + f" {n} / {len(df_p)} " + t("行"))
                    st.rerun()
            if pm_now:
                st.dataframe(pd.DataFrame(list(pm_now.values())[:200]),
                             use_container_width=True, hide_index=True)


    if not excel_mode and not cp.is_configured():
        st.warning(t("Coupang 未配置（元川 .env 的 COUPANG_ACCESS_KEY / SECRET_KEY / VENDOR_ID）"))

    if not excel_mode:
        pm = store_cp.product_map()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(t("商品主档"), f"{len(pm)}")
        q_all = store_cp.list_queue()
        c2.metric(t("待处理"), sum(1 for r in q_all if r["ecms_status"] == "pending"))
        c3.metric(t("已发送"), sum(1 for r in q_all if r["ecms_status"] == "sent"))
        c4.metric(t("换算汇率"), f"{ce.fx_rate():.5f}", help=t("KRW→USD。运营 Excel 的固定系数"))

        # ---- 拉取 ----
        p1, p2 = st.columns([1, 3])
        days = p1.number_input(t("拉取最近几天"), min_value=1, max_value=30, value=3, key="cp_days")
        if p2.button(t("从 Coupang 拉取待发货订单"), type="primary", key="cp_pull",
                     disabled=not cp.is_configured()):
            try:
                boxes = cp.fetch_shippable(days=int(days))
            except cp.CoupangError as e:
                st.error(str(e))
            else:
                now = store_cp._now()
                jans = sorted({ce.split_sku(it.get("externalVendorSkuCode") or "")[0]
                               for b in boxes for it in (b.get("orderItems") or [])})
                rows = [ce.to_queue_row(b, pm, now, store_cp.nst_master_map(jans))
                        for b in boxes]
                n, skipped = store_cp.upsert_queue(rows)
                purged = store_cp.purge_old()
                st.success(t("拉取完成") +
                           f" · {t('取得')} {len(boxes)} · {t('入库')} {n} · "
                           f"{t('已发送跳过')} {skipped} · {t('过期清理')} {purged}")
                st.rerun()

        st.caption(t("个人信息（姓名/电话/地址/PCCC）保存 7 天后自动删除，每次拉取时清理。"))

        # ---- 核对 ----
        pending = [r for r in q_all if r["ecms_status"] in ("pending", "failed")]
        if not pending:
            st.info(t("没有待处理的订单"))
        else:
            st.subheader(t("核对") + f"（{len(pending)}）")
            st.caption(t("红色 = 缺必填项，发不出去。省/市/地址/PCCC/电话/重量 可以直接改。"))
            view = []
            for r in pending:
                miss = ce.missing_fields(r)
                view.append({
                    "发送": not miss,
                    "缺": "、".join(miss) if miss else "",
                    "order_id": r["order_id"], "box_id": r["shipment_box_id"],
                    "姓名": r["receiver_name"], "电话": r["receiver_phone"],
                    "邮编": r["receiver_postcode"], "省/州": r["addr_sido"],
                    "城市": r["addr_sigungu"], "详细地址": r["addr_detail"],
                    "PCCC": r["pccc"], "重量kg": r["weight_kg"],
                    "USD": r["total_usd"], "通关": ce.clearance_type(r["total_usd"] or 0),
                    "品目": " / ".join(f"{i.get('name_en') or i.get('jan')}×{i['qty']}"
                                       for i in r["items"]),
                })
            edited = st.data_editor(
                pd.DataFrame(view), use_container_width=True, hide_index=True, key="cp_edit",
                disabled=["缺", "order_id", "box_id", "USD", "通关", "品目"],
                column_config={"发送": st.column_config.CheckboxColumn(t("发送"))},
            )

            if st.button(t("保存修改"), key="cp_save"):
                n = 0
                for _, row in edited.iterrows():
                    store_cp.update_row(
                        str(row["order_id"]), str(row["box_id"]),
                        receiver_name=row["姓名"], receiver_phone=row["电话"],
                        receiver_postcode=str(row["邮编"]), addr_sido=row["省/州"],
                        addr_sigungu=row["城市"], addr_detail=row["详细地址"],
                        pccc=row["PCCC"],
                        weight_kg=float(row["重量kg"]) if row["重量kg"] else None)
                    n += 1
                st.success(t("已保存") + f" {n}")
                st.rerun()

            # ---- 发送 ----
            chosen = [r for r in edited.to_dict("records") if r["发送"] and not r["缺"]]
            st.divider()
            shipper = ec.shipper_default()
            blocked = []
            if not ec.is_configured():
                blocked.append(t("ECMS 凭证未配置"))
            if not shipper:
                blocked.append(t("发件人未配置（ECMS_SHIPPER_JSON）"))
            if blocked:
                st.warning("、".join(blocked))

            ok_send = st.checkbox(
                t("已核对，确认发送（ECMS 建单不可撤销，只能事后取消）"), key="cp_confirm")
            if st.button(t("发送到 ECMS") + f"（{len(chosen)}）", type="primary", key="cp_send",
                         disabled=not (chosen and ok_send and not blocked)):
                by_key = {(r["order_id"], r["shipment_box_id"]): r for r in pending}
                ok = fail = 0
                log = []
                for c in chosen:
                    r = by_key.get((str(c["order_id"]), str(c["box_id"])))
                    if not r:
                        continue
                    ref = f"CP-{r['order_id']}-{r['shipment_box_id']}"
                    if ecms_store.fetch_shipment(ref):
                        log.append(f"⏭️ {ref} " + t("已建过，跳过"))
                        continue
                    receiver = {
                        "country": "KR", "name": r["receiver_name"], "state": r["addr_sido"],
                        "city": r["addr_sigungu"], "address1": r["addr_detail"],
                        "postCode": r["receiver_postcode"], "phone": r["receiver_phone"],
                        "email": "",
                    }
                    items = [{"name": i["name_en"], "description": i["name_en"],
                              "quantity": i["qty"], "price_amount": i["price_usd"],
                              "price_currency": "USD",
                              "weight_kg": (i["weight_kg"] or 0) / max(1, i["qty"]),
                              "origin_country": "JP", "hscode": i.get("hscode") or "",
                              "url": i.get("url") or ""} for i in r["items"]]
                    # TODO(PCCC): 牧野さん回答待ち。API のどのフィールドに載せるか未確定のため
                    # いまは備考にだけ入れておく（回答が来たら build_shipment に正式に渡す）
                    payload = ec.build_shipment(
                        reference_code=ref, receiver=receiver, items=items,
                        weight_kg=r["weight_kg"], length_cm=25, width_cm=18, height_cm=8,
                        shipper=shipper)
                    payload["customs"]["importReference"] = r["pccc"]
                    base = dict(reference_code=ref, receiver_name=r["receiver_name"],
                                receiver_country="KR", request_json=payload, created_by=_user())
                    try:
                        data = ec.create_shipment(payload)
                    except ec.EcmsError as e:
                        fail += 1
                        ecms_store.save_shipment(**base, status="failed",
                                                 response_json={"error": str(e)})
                        store_cp.update_row(r["order_id"], r["shipment_box_id"],
                                            ecms_status="failed", note=str(e)[:400])
                        log.append(f"❌ {ref}: {e}")
                    else:
                        ok += 1
                        box = (data.get("boxes") or [{}])[0]
                        ecms_store.save_shipment(**base, status="created",
                                                 shipment_id=data.get("shipmentId") or "",
                                                 tracking_no=box.get("trackingNo") or "",
                                                 label_url=(box.get("file") or {}).get("labelUrl") or "",
                                                 response_json=data)
                        store_cp.update_row(r["order_id"], r["shipment_box_id"],
                                            ecms_status="sent", ecms_reference=ref, note="")
                        log.append(f"✅ {ref} → {box.get('trackingNo')}")
                st.success(f"ok={ok} fail={fail} " + t("（详情见下）"))
                st.code("\n".join(log) or "-")

        # ---- 面单 ----
        sent = [r for r in q_all if r["ecms_status"] == "sent" and r["ecms_reference"]]
        if sent:
            with st.expander(t("已发送的面单") + f"（{len(sent)}）", expanded=False):
                for r in sent:
                    s = ecms_store.fetch_shipment(r["ecms_reference"])
                    if not s:
                        continue
                    cols = st.columns([2, 2, 1])
                    cols[0].write(f"`{r['ecms_reference']}` {r['receiver_name']}")
                    cols[1].write(s.get("tracking_no") or "-")
                    if s.get("label_url"):
                        cols[2].link_button(t("面单"), s["label_url"])

# ============================================================
# ② Shopify · 後回し（Boss 2026-08-30「coupang做完后再做shopify的」）
# ============================================================
with tab_sf:
    st.info(t("Shopify 侧待做——先把 Coupang 这条跑通。手工建单可用「✍️ 手工建单」tab。"))

# ============================================================
# 手工建单
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

        existing = ecms_store.fetch_shipment(ref.strip())
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
                    ecms_store.save_shipment(**base, status="failed",
                                        response_json={"error": str(e), "errors": e.errors})
                    st.error(t("建单失败：") + str(e))
                else:
                    box = (data.get("boxes") or [{}])[0]
                    ecms_store.save_shipment(**base, status="created",
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
                n = ecms_store.save_events(events)
                st.caption(t("已落库") + f" {n} " + t("条（重复事件自动跳过）"))
                st.dataframe(
                    pd.DataFrame(events)[["date", "code", "description", "location", "remark"]],
                    use_container_width=True, hide_index=True)

    hist_tno = (tt or ts).strip()
    if hist_tno:
        rows = ecms_store.local_events(hist_tno)
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
                ecms_store.update_status(cref.strip(), "cancelled")
            st.success(t("已取消") + f" · {resp.get('message', '')}")

# ============================================================
# 📋 记录
# ============================================================
with tab_log:
    rows = ecms_store.recent_shipments()
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info(t("还没有建单记录"))
