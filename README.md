# CMS · SmikieJapan 商品情報管理システム

> Streamlit 経営看板 · 25 ページ · **元川さん（Inspiron 5405 Windows）本番稼働**
> NetSuite API 自動 pull + Postgres 16 + Cloudflare Tunnel + CF Access
> 最終更新：2026-05-20

---

## 一行サマリ

NetSuite から毎日自動で業務データを抽出し、元川さん上の Postgres に集約 → 運営が `https://smikie-cms.cc` で経営看板を操作するシステム。

---

## 全体アーキテクチャ（最新）

```
┌─────────────────────────────────────────────────────────────────┐
│ [NetSuite Cloud]                                                │
│   ・item master / 在庫 / 売上 / 原価（輸出事業のみ）            │
│   ・TBA OAuth1 認証                                             │
└────────────────────────┬────────────────────────────────────────┘
                         │  毎日定時 pull
                         │  REST + SuiteQL
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ [元川さん（Inspiron 5405 / Windows / Ryzen 5 4500U / 16GB）]    │
│ ─────────────────────────────────────────────────────────────── │
│                                                                 │
│  Windows タスクスケジューラ                                      │
│       ↓ 毎日 6:00 起動                                          │
│  Docker stack（docker-compose）                                 │
│   ├ database/nst_api/daily_pull.py（NST → PG）                 │
│   ├ postgres:16 ⭐ 業務データ単一事実源                          │
│   │    └ nst schema：item_master / cost_history /              │
│   │                   sales_raw / inventory_raw                 │
│   ├ streamlit（cms.py）                                         │
│   ├ cloudflared（smikie-cms.cc トンネル終端）                   │
│   └ pgweb（DB GUI · 内部限定）                                  │
│                                                                 │
└────────────────────────┬────────────────────────────────────────┘
                         │  Cloudflare Tunnel
                         │  + CF Access（@mitsukin.info 限定）
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ [運営ブラウザ] https://smikie-cms.cc                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## データソース（2 系統）

### 自動系統（最新の中核）⭐

NetSuite API → 元川さん上 daily_pull → Postgres `nst` schema

| ドメイン | 実装 | テーブル |
|---|---|---|
| 商品マスタ + 原価 | `database/data_warehouse/nst_api/pull_items.py` | `nst.item_master_raw` + `nst.cost_history` |
| 売上 | `pull_sales.py` | `nst.sales_raw` |
| 在庫 | `pull_inventory.py`（JD-物流-千葉のみ · 弁天倉庫除外）| `nst.inventory_raw` |
| 原価 | `pull_costs.py` → リダイレクト → `pull_items` | 同上 |

実体は **`~/CC/database/data_warehouse/nst_api/`**（database nested repo）。詳細は [`docs/15-nst-api-field-spec.md`](docs/15-nst-api-field-spec.md)。

### 手動系統（移行中・近日廃止予定）

運営が Excel/XML を Streamlit の `99_⚙️_数据导入与设置` ページからアップロード → ingester が PG に書き込み。NST API 全ドメイン稼働後に廃止。

---

## 機能ページ一覧（25 ページ）

| Page | 用途 |
|---|---|
| 02_🔍_商品情报检索 | 多次元フィルタ + 全文検索 + CSV エクスポート |
| 03_💰_定義原価編集 | 原価統一管理（自動判定 + Boss 手動上書き）|
| 04_📊_销售数据查询 | 時系列 + 次元クロス |
| 05_🏪_店铺别毛利 | 月度切替 + Plotly |
| **06_📦_库存健康监控** | 在庫月数 4 段階健康度（🟢🟡🟠🔴）|
| **07_🏷️_商品等级判定** | A/B/C/停売 4 段階 + 趨勢 + 運営提案 |
| 09_📜_発注履歴 | 過去発注一覧 |
| 10_📦_発注書作成 | 発注書出力 |
| **11_💡_运营调整建议** | 粗利 × 回転 5 段階マトリクス |
| 12_🚫_入荷困難商品 | 仕入困難管理 |
| **13_⚠️_改廃確認** | Boss 三按鈕（取扱中止/継続/代替品調査）|
| **14_💱_財務** | 拨款 + 注文級照合 |
| 15_📝_商品登录 | 商品登録 |
| 16_📈_等级历史趋势 | Sankey 図 |
| 17_💰_价格改善 | 価格改善候補 |
| 18_📦_订货依据 | 発注ロジック説明 |
| 19_🧊_保质期管理 | 賞味期限管理 |
| 20_📈_定义原価波動图 | 原価変動可視化 |
| **21_🚀_Shopee上架** | Shopee 自動上架 |
| 22_🗄️_数据库管理 | DB 管理 |
| 23_🏷️_Tag管理 | Tag 管理 |
| 24_♻️_不良品処分CSV | 廃棄処理 |
| **25_📦_発注AI_v2** | AI 発注（多仕入先決定版）|
| 26_🌐_图片翻译 | 商品画像多言語化 |
| 99_⚙️_数据导入与设置 | データ手動取り込み + システム設定 |

---

## ディレクトリ構造

```
CMS/
├── cms.py                  Streamlit エントリ
├── pages/                  25 ページ
├── modules/                業務ロジック（cost_sync / inventory_health / rank_classifier / operation_advice / image_translate）
├── shared/                 横断ユーティリティ（db / auth / lark_* / forex / purchase_engine 等）
├── data_warehouse/         旧 ingester（ローカル開発用 SQLite · 本番は PG）
├── tools/                  補助スクリプト
├── shopee_listing/         Shopee 自動上架サブシステム
├── tests/                  pytest（unit + integration）
├── deploy/
│   ├── push.sh             Mac → GitHub → 元川さん デプロイ
│   ├── windows/            元川さん デプロイ用 PS1 + compose
│   └── n8n/                N8N 連携
├── docs/                   設計ドキュメント（最新版は本 README とリンク先）
└── .streamlit/             config.toml + secrets.toml.example
```

⚠️ NetSuite からのデータ取得実装は **`~/CC/database/data_warehouse/nst_api/`**（別 repo）。

---

## 開発フロー（Mac → 元川さん）

```
[Mac] Boss が編集
   ↓ cmsdeploy（= git push + ssh 元川さん git pull · 一括）
[GitHub: JOE00778/CMS-v230]（コードのみ · データ・凭据は除外）
   ↓ ssh git pull（bind mount · 即時反映 · build 不要）
[元川さん] streamlit コンテナ自動 reload
   ↓
[smikie-cms.cc] 運営が新バージョン使用
```

### Mac → 元川さん SSH（2026-05-20 配通）

| 接続 | コマンド | 用途 |
|---|---|---|
| 局域网 | `ssh smiki@192.168.68.96` | 同 WiFi/网线 · 直連速い |
| 异地 | `ssh cms-ssh.smikie-cms.cc` | CF Access（JO@mitsukin.info）+ Tunnel · どこからでも |
| 一键部署（局域网）| `cmsdeploy` | git push + git pull 一括 |
| 一键部署（异地）| `cmsdeploy-r` | 同上 · CF 隧道経由 |

- 日常（コードのみ）: `git pull` で bind mount 即時反映、**build 不要**
- 依存/.env/Dockerfile 変更時のみ: `redeploy.bat`（build 込み）
- 詳細手順: [`deploy/windows/SSH-ACCESS.md`](deploy/windows/)（本地参考 · gitignore · 内网配置含む）

**Mac は編集専用**。稼働コアは全て元川さんにあります（[mac-editor-only-policy](../.claude/memory/feedback_mac_editor_only_policy.md) 参照）。

---

## 起動

### Mac（ローカル開発）

```bash
cd ~/CC/CMS
uv venv && uv sync
uv run streamlit run cms.py
```

→ `http://localhost:8501` で動作確認（本番データには接続せず、ローカル SQLite を使用）。

### 元川さん（本番）

```powershell
# 日常（コードのみ変更 · bind mount 即時反映 · build 不要）
cd C:\Users\smiki\CMS-v230
git pull origin main
# または update-cms.bat ダブルクリック

# 依存/.env/Dockerfile 変更時のみ（build 込み）
docker compose -f deploy\windows\docker-compose.yml up -d --build streamlit
# または redeploy.bat
```

Mac からは `cmsdeploy`（局域网）/ `cmsdeploy-r`（异地）で push + pull 一括。詳細は [`deploy/windows/README.md`](deploy/windows/README.md) + [`deploy/windows/SSH-ACCESS.md`](deploy/windows/)。

---

## 公開・認証

| 項目 | 内容 |
|---|---|
| 公開 URL | https://smikie-cms.cc |
| ドメイン管理 | Cloudflare |
| トンネル | Cloudflare Tunnel（cloudflared コンテナ）|
| アクセス制御 | Cloudflare Access · `@mitsukin.info` ドメイン限定 |
| 一次パスワード | CMS ログイン（統一）|
| 二次パスワード | `99_⚙️_数据导入与设置` ページ専用 |

---

## バックアップ

| 対象 | 場所 | 方式 |
|---|---|---|
| コード | GitHub `JOE00778/CMS-v230`（private） | git push |
| 業務 PG データ | NAS は 2026-05-18 廃止決定 → 現状単点（Inspiron 内のみ）| ⚠️ 離機バックアップなし |
| シークレット | 元川さんの compose env / Mac の `~/.smikie-shopify-token` | ⚠️ 中央化未実施 |
| 商品画像（6,304 枚）| Mac `~/CC/CMS/jancode_images_hd/`（gitignore）| ⚠️ Mac のみ・移転待ち（T-MIG-001）|

---

## ステータス・進行中タスク

| タスク | 状態 |
|---|---|
| **T-NST-001** NetSuite API 自動 pull 本番稼働 | scaffolding 完了（2026-05-18）· D-101 解除済 · credentials 投入 + run() 実装中 |
| **T-MIG-001** Mac → 元川さん アセット移転 | 起票済（2026-05-19）· **Mac→元川さん SSH 配通（2026-05-20 · 局域网+异地）** · アセット移転実施待ち |
| **Supabase 廃止** | T-NST-001 完了後・ASEAN 7 ストリーム PG 統合後 |
| **Phase 4 DB 整理** | ✅ 完了（2026-05-09 · `commits 7b6bf61 + 2dbcf67`）|

---

## 関連ドキュメント

### 最新の中核ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/15-nst-api-field-spec.md`](docs/15-nst-api-field-spec.md) | NetSuite REST API フィールド仕様 |
| [`docs/16-purchase-decision-spec.md`](docs/16-purchase-decision-spec.md) | 自動発注（多仕入先決定版）設計 |
| [`docs/17-订货算法选型.md`](docs/17-订货算法选型.md) | 発注アルゴリズム選定（最新）|
| [`docs/11-architecture-final.md`](docs/11-architecture-final.md) | Phase 4 DB 最終アーキテクチャ |
| [`docs/10-database-tables-reference.md`](docs/10-database-tables-reference.md) | DB テーブル完全リファレンス |
| [`docs/08-data-model-v2.md`](docs/08-data-model-v2.md) | データモデル v2（JAN 中心）|
| [`docs/03-metrics-v3-2026-05-05.md`](docs/03-metrics-v3-2026-05-05.md) | metrics v3 計算式 |
| [`docs/04-automation-architecture.md`](docs/04-automation-architecture.md) | 自動化アーキテクチャ |
| [`docs/07-lark-integration.md`](docs/07-lark-integration.md) | 飞书連携 |
| [`订货逻辑.md`](订货逻辑.md) | Boss 公式（発注ロジック単一事実源）|
| [`NetSuite-API/`](NetSuite-API/) | NetSuite API フィールド仕様 + 字段清单（中日双语 · 2026-05-20）|
| [`deploy/windows/SSH-ACCESS.md`](deploy/windows/) | Mac → 元川さん SSH 接入手順（局域网+异地 · **本地 gitignore · 内网配置含む**）|

### アーカイブ済（参考のみ · 最新版に統合された旧文書）

`docs/01`（v1 増強方案）/ `docs/02`（v2 増強方案）/ `docs/05`（ComfyUI 調研）/ `docs/06`（N8N 画像生成）/ `docs/09`（明日デプロイ清单 · 既に完了）/ `docs/12-13`（NST Saved Search 検討 · NST API に置換済）

---

## 重要な用語

| 用語 | 意味 | 対応ページ |
|---|---|---|
| **改廃** | 品牌方/メーカー 製品迭代（外部信号）| page 13 改廃確認 |
| **降级** | 内部データ駆動の渐变下調（A→B→C→停売）| page 11 運営調整建議 |
| **定義原価** | Boss 確定の原価（NS の `cost` + `averagecost` + `lastpurchaseprice` + `costestimate` から算出）| page 03 定義原価編集 |
| **元川さん** | Inspiron 5405 Windows の愛称・CMS 本番ホスト | — |

---

## ライセンス

Private · SmikieJapan internal use only.
