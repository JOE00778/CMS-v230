# ECMS 牧野さま宛て 問い合わせ文（そのまま送れる形）

> 2026-08-30 · Boss が自分で送る前提で用意（送信は Boss、こちらからは連絡しない）。
> 背景は `24-ecms-shipping-spec.md`。①が最優先——ここが決まらないと韓国通関が通らない。

---

牧野さま

お世話になっております。三金商事の○○です。

ECMS STANDARD EXPRESS の API（仕様書 v1.7）でシステム連携を進めております。
韓国向け（Coupang からの受注）を最初の対象としており、下記についてご確認いただけますでしょうか。

**① 韓国向け「個人通関固有符号（PCCC / 개인통관고유부호）」の連携方法**

韓国の個人向け輸入通関では受取人ごとの PCCC（`P` + 12 桁）が必須と認識しております。

- Excel の一括アップロード用テンプレートには `Consignee ID Type`（受取人証件類型）と
  `Consignee IDNo`（受取人証件番号）の列があり、こちらに入れる運用と理解しています。
- 一方 **API（`POST /api/manifest`）側は、仕様書 v1.7 で証件番号を持てるのが `IOR` オブジェクトの
  `idNumber` / `idType` のみで、その `IOR` には「Reserved」と記載**されています。

つきましては、

1. API で PCCC を送る場合、**どのフィールドに載せればよいでしょうか**
   （`IOR.idNumber` / `item.additionalInfo.taxNumber` / その他）。
   `idType` に指定すべき値も併せてご教示ください。
2. Coupang からは通常の PCCC のほかに **一回限りの PCCC（one-time PCCC）** が
   返ってくる場合があります。こちらも同じ扱いでよろしいでしょうか。
3. PCCC が取得できなかった注文について、ECMS 側で受け付けていただける運用はありますか。

**② 接続情報（すべて必須項目のため、揃わないと送信できません）**

- `clientId`（UAT・本番）
- Bearer token（UAT・本番、それぞれ）
- `Warehouse Code`（仕様書の `originWarehouseCode` に相当）
- `Shipper Code`（発送人コード）
- `Platform Id`（電子商取引プラットフォームコード。Coupang 向けの値があればご指定ください）

**③ 弊社アカウントでの取り決め値**

- `serviceType`：`Warehouse` / `Dropoff` / `Pickup` のいずれでしょうか
  （仕様書には「ECMS との取り決めに従って固定」とあります。現状 `Warehouse` を既定にしています）
- `reasonForExport`：EC 販売の場合の指定値（仕様書の表記は `commercial (Reseller)`）
- `incoterm` と `dutyBilling.paidBy`：韓国向けの標準的な組み合わせ

**④ 数値項目の精度**

仕様書 API 側では重量・寸法・単価がいずれも `Double(8,2)`（小数点以下 2 桁）と理解しております。
**Excel アップロード側も同じ精度で問題ないでしょうか**。
桁数や小数点以下の指定が別途ある場合はご教示ください。

**⑤ 申告通貨**

韓国の個人通関免税枠が USD 150 のため、`Item_Currency` は **USD** で申告する想定です。
KRW でのご申告を推奨される場合はお知らせください。

お手数をおかけしますが、よろしくお願いいたします。

---

## 送った後のこちら側の動き

| 回答 | こちらの対応 |
|---|---|
| ① PCCC のフィールド | `shared/ecms_client.py` の `build_shipment()` に追加、page41 の必須チェックに組み込み |
| ② 接続情報 | 元川の `.env` に設定（`ECMS_CLIENT_ID` / `ECMS_TOKEN` ほか）→ UAT 疎通 |
| ③ 取り決め値 | 既定値（`Warehouse` / `commercial`）を差し替え |
| ④ 精度 | 相違があれば丸め処理を修正 |
| ⑤ 通貨 | KRW 指定なら `Double(8,2)` の上限 999,999.99 に当たらないか要再検討 |
