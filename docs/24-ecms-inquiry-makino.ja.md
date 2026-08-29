# ECMS 牧野さま宛て 問い合わせ文

> 2026-08-30 · 送信は Boss 本人。背景は `24-ecms-shipping-spec.md`。
> ①が最優先——決まらないと韓国通関が通らない。

---

牧野さま

お世話になっております。三金商事の○○です。

ECMS の API（仕様書 v1.7）連携を進めております。韓国向け（Coupang 受注）から始めますので、
下記ご教示ください。

**① PCCC（개인통관고유부호）をどのフィールドで送りますか**
Excel テンプレートには `Consignee ID Type` / `Consignee IDNo` 列がありますが、
API 側で証件番号を持てる `IOR` は仕様書上「Reserved」表記です。
`IOR.idNumber` でしょうか、それとも別のフィールドでしょうか（`idType` の指定値も）。
一回限りの PCCC（one-time PCCC）も同じ扱いで良いかも併せてお願いします。

**② 接続情報**（すべて必須項目）
`clientId`（UAT・本番） / Bearer token（UAT・本番） / `Warehouse Code` /
`Shipper Code` / `Platform Id`

**③ 取り決め値**
`serviceType`（Warehouse / Dropoff / Pickup のいずれか） / `reasonForExport`（EC 販売の場合） /
韓国向けの `incoterm` と `dutyBilling.paidBy`

**④ 数値の精度**
API 仕様では重量・寸法・単価が `Double(8,2)`。Excel アップロード側も同じで良いでしょうか。

**⑤ 申告通貨**
免税枠が USD 150 のため USD 申告を想定しています。KRW 推奨であればお知らせください。

よろしくお願いいたします。

---

## 回答が来たら

| 回答 | こちら |
|---|---|
| ① | `ecms_client.build_shipment()` に追加 + page41 の必須チェック |
| ② | 元川 `.env` 設定 → UAT 疎通 |
| ③ | 既定値（Warehouse / commercial）差し替え |
| ④ | 相違あれば丸め修正 |
| ⑤ | KRW なら `Double(8,2)` 上限 999,999.99 を再検討 |
