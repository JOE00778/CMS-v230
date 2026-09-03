"""ECMS API 疎通確認 · 建単 → 面単 → 追跡 → 取消 を 1 本で通す（元川で手動実行）。

Boss 2026-08-30「先测试 ECMS 推送订单信息和回传面单是否可用」。
page41 で人が押していく代わりに、4 接口を順に叩いて**どこで落ちたかを数で出す**。

安全側の作り:
  · **既定は UAT のみ**。ECMS_ENV=pro のときは実運送状になるので拒否する（--allow-pro で解除）
  · 建てた運単は最後に **自動で取消す**（--keep で残せる。ただし**本番では --keep 禁止**——
    取消されない実運送状が残るため）
  · 落ちたらそこで止めて生レスポンスを出す（推測しない）

用法（元川 · 凭据は .env 済みが前提）:
  docker exec cms_streamlit python tools/ecms_uat_check.py
  docker exec cms_streamlit python tools/ecms_uat_check.py --keep   # 取消さない
  docker exec cms_streamlit python tools/ecms_uat_check.py --label-dir /app/data/files
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import ecms_client as ec          # noqa: E402

# UAT 用のダミー。韓国向けの書式に合わせてあるが中身は明らかにテストと判る値
TEST_RECEIVER = {
    "country": "KR", "name": "TEST RECEIVER", "state": "서울특별시", "city": "서울특별시",
    "address1": "강남구 테헤란로 152 (역삼동)", "postCode": "06236",
    "phone": "010-0000-0000", "email": "test@example.com",
}
TEST_ITEMS = [{
    "name": "TEST ITEM cleansing foam", "description": "TEST ITEM cleansing foam 120g",
    "quantity": 1, "price_amount": 10.00, "price_currency": "USD",
    "weight_kg": 0.2, "origin_country": "JP", "hscode": "330499",
}]


def _step(n: int, title: str) -> None:
    print(f"\n[{n}/4] {title}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="最後に取消さない")
    ap.add_argument("--allow-pro", action="store_true",
                    help="本番環境で流す（**実運送状が立つ**）")
    ap.add_argument("--label-dir", default=".", help="面単 PDF の保存先")
    args = ap.parse_args()

    env = ec.env_name()
    print(f"env={env} base={ec.base_url()}")
    if env == "pro" and not args.allow_pro:
        print("NG: ECMS_ENV=pro。本番で実運送状が立つため中止した。"
              "本当に本番で試すなら --allow-pro", file=sys.stderr)
        return 2
    if env == "pro" and args.keep:
        # 本番で運送状を残したまま終わると、誰も取消さない実運送状が 1 本残る
        print("NG: 本番で --keep は許可しない（取消されない実運送状が残る）", file=sys.stderr)
        return 2
    if env == "pro":
        print("⚠️ **本番環境**。テスト用のダミー宛先で運送状を 1 本立て、最後に取消す。"
              "ECMS 側には『作成→取消』の記録が残る。", flush=True)
    if not ec.is_configured():
        print("NG: ECMS_CLIENT_ID / ECMS_TOKEN 未設定", file=sys.stderr)
        return 2
    shipper = ec.shipper_default()
    if not shipper:
        print("NG: ECMS_SHIPPER_JSON 未設定（発送人が空だと ECMS に弾かれる）", file=sys.stderr)
        return 2

    ref = "UATCHK-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"referenceCode={ref}")

    # ---- 1. 建単 ----
    _step(1, "manifest（建単）")
    payload = ec.build_shipment(
        reference_code=ref, receiver=TEST_RECEIVER, items=TEST_ITEMS,
        weight_kg=0.3, length_cm=25, width_cm=18, height_cm=8, shipper=shipper)
    try:
        data = ec.create_shipment(payload)
    except ec.EcmsError as e:
        print(f"  NG: {e}", file=sys.stderr)
        print(f"  送った内容:\n{json.dumps(payload, ensure_ascii=False, indent=2)}",
              file=sys.stderr)
        return 1
    box = (data.get("boxes") or [{}])[0]
    tracking = box.get("trackingNo") or ""
    shipment_id = data.get("shipmentId") or ""
    print(f"  OK trackingNo={tracking} shipmentId={shipment_id}")
    if not tracking:
        print("  NG: trackingNo が返っていない", file=sys.stderr)
        print(json.dumps(data, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    ok = 1
    # ---- 2. 面単 ----
    _step(2, "printLabel（面単）")
    label_path = None
    try:
        label = ec.get_label(tracking_no=tracking)
    except ec.EcmsError as e:
        print(f"  NG: {e}", file=sys.stderr)
    else:
        ok += 1
        print(f"  OK size={label['size']} type={label['fileType']} "
              f"url={'あり' if label['labelUrl'] else 'なし'} "
              f"base64={'あり' if label['content'] else 'なし'}")
        if label["content"]:
            label_path = Path(args.label_dir) / f"ECMS_{tracking}.{label['fileType']}"
            try:
                label_path.write_bytes(base64.b64decode(label["content"]))
                size = label_path.stat().st_size
                print(f"  面単を保存: {label_path}（{size:,} bytes）")
                if size < 1000:
                    print("  ⚠️ ファイルが小さすぎる。中身を確認すること", file=sys.stderr)
            except Exception as e:                     # 保存に失敗しても続行する
                print(f"  ⚠️ 保存失敗: {e}", file=sys.stderr)
        elif label["labelUrl"]:
            print(f"  URL のみ返却（base64 なし）: {label['labelUrl']}")

    # ---- 3. 追跡 ----
    _step(3, "getTracking（追跡）")
    try:
        events = ec.get_tracking(tracking_no=tracking)
    except ec.EcmsError as e:
        print(f"  NG: {e}", file=sys.stderr)
    else:
        ok += 1
        print(f"  OK events={len(events)}")
        for e in events[:5]:
            print(f"    {e['date']} {e['code']} {e['description']} {e['location']}")
        if not events:
            print("  （建てた直後は 0 件のことがある。S01N100 が付くまで時差あり）")

    # ---- 4. 取消 ----
    _step(4, "cancelShipment（取消）")
    if args.keep:
        print(f"  スキップ（--keep）。この運単は残る: {tracking}")
    else:
        try:
            resp = ec.cancel_shipment(tracking_no=tracking)
        except ec.EcmsError as e:
            print(f"  NG: {e}", file=sys.stderr)
            print(f"  ⚠️ 建てた運単が残っている: {tracking}。ECMS 画面で取消すこと",
                  file=sys.stderr)
        else:
            ok += 1
            print(f"  OK {resp.get('message', '')}")

    total = 3 if args.keep else 4
    print(f"\nok={ok}/{total} env={env} ref={ref} tracking={tracking}"
          + (f" label={label_path}" if label_path else ""))
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
