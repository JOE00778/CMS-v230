# NST API データフロー · 内外データセキュリティ総合レビュー（DX部 審査版）

> バージョン：2026-05-18 v2 · scaffolding 段階 · リポジトリ `database/data_warehouse/nst_api/`
> タスク：[T-NST-001](../../../.tasks/doing/T-NST-001-nst-api-daily-pull.md)
> 対象読者：DX部、情報セキュリティ責任者、輸出事業統括、財務責任者
> スコープ：**NST API が NetSuite からデータを取得し、社内 PG `nst.*` に更新する全プロセス**
> データ範囲：**輸出事業のみ**（CB事業 / 国内事業のデータは取得しない・格納しない・配信しない）
> セキュリティ視点：**外部攻撃面 ＋ 内部統制 を同等比重で網羅**

---

## 目次

| 章 | 内容 | 主視点 |
|---|---|---|
| 1 | 視点宣言とスコープ | — |
| 2 | 適用範囲（輸出事業限定の三層防御） | 内部 |
| 3 | **拉取プロセス全解 — ステップバイステップ** | 内外両方 |
| 4 | 外部攻撃面の制御 | **外部** |
| 5 | データ資産分類（C0-C3） | 内部 |
| 6 | データフローマップ（社内視点） | 内部 |
| 7 | ロールとアクセスマトリックス（RBAC） | 内部 |
| 8 | 下流コンシューマ隔離 | 内部 |
| 9 | データ流出経路と制御 | 内部 |
| 10 | アクセス監査 | 内外両方 |
| 11 | データ保持と廃棄 | 内部 |
| 12 | 物理 / 環境層 | 内外両方 |
| 13 | 退職 / 異動 SOP | 内部 |
| 14 | 内部脅威モデル | 内部 |
| 15 | 外部脅威モデル | **外部** |
| 16 | D-101 ブロックと連動セキュリティ決定 | — |
| 17 | DX部 重点レビュー / 決議事項 | — |

---

## 1. 視点宣言とスコープ

本書は **NST API が NetSuite から輸出事業データを取得し、社内 PG `nst.*` に毎日更新する全プロセス** を対象とする総合的データセキュリティレビュー文書である。

**評価視点（内外部同等比重）**：

- **外部視点**：第三者攻撃者がデータ取得経路・認証情報・通信路を侵害するリスク
- **内部視点**：社内従業員 / ツール / システムによる権限境界違反・意図的非意図的漏洩・退職時残存権限

NST システムは NetSuite の経営データを「手動 Excel ダウンロード」から「自動 PG 落とし」に移行した直後である。データ可達性が一桁上がった結果、攻撃面・内部統制の両面でガバナンスを確立すべき最適なタイミングにある。

**本書がカバーしないもの**：

- NetSuite 自体のテナント側セキュリティ（OCI / NetSuite クラウド側責任範囲）
- PG が同居する Inspiron 5405 上の他システム（CMS Streamlit / N8N 等）の独自セキュリティ — 別書（CMS-SECURITY、N8N-SECURITY）にて
- 一般的なエンドポイントセキュリティ（ウイルス対策、EDR 等）— IT 一般運用に従う

---

## 2. 適用範囲（**輸出事業限定**）

本システムが扱うデータは **すべて輸出事業のみ**。CB事業 / 国内事業のデータは取得しない、PG に格納しない、下流に配信しない。

### 2.1 三層防御

| 層 | 実装場所 | 効果 |
|---|---|---|
| **層1 取得層** | NetSuite Saved Search に `department = 輸出事業` フィルタを必須付与（D-101 ① で決議） | そもそも他事業データを物理的に取得不能 |
| **層2 格納層** | PG `nst.*` の各原テーブルに `CHECK (department_code = '輸出')` 制約を導入 | 仮に層1が漏れても DB レイヤで弾く |
| **層3 消費層** | 下流ロール（CMS / N8N / pgweb）に対し輸出事業ビューのみ GRANT、他事業データを問い合わせる SQL が書けない | アプリレイヤで三重防御 |

### 2.2 異常検知

- **他事業データが万一混入した場合**：`nst._pull_errors` に記録 + Lark webhook で即時通知 + 該当行物理削除 + 根本原因調査
- **検知メカニズム**：層2 の CHECK 制約違反は INSERT 時点で例外を発生させ、daily_pull がエラーとして捕捉

### 2.3 範囲変更の承認

「輸出事業フィルタ」自体の変更（D-101 ① の Saved Search 定義変更）は **経営者 + DX部 + 財務責任者の三者承認** を必須とする。一者承認では不可。

---

## 3. 拉取プロセス全解 — ステップバイステップ

本章が本書の核心。**NetSuite から PG `nst.*` への 1 日 1 回の更新プロセス**を 9 段階に分解し、各段階のセキュリティ関心事を明示する。

### 3.1 全体タイムライン

```
09:00:00  ① Windows Task Scheduler が起動
09:00:01  ② PowerShell スクリプト inspiron-daily-pull.ps1 が動作
09:00:02  ③ Docker exec で database コンテナ内 CLI を起動
09:00:03  ④ CLI が認証情報を環境変数から読込・client 構築
09:00:04  ⑤ OAuth2 トークン取得（JWT assertion 経由）または TBA 署名生成
09:00:05  ⑥ NetSuite REST/SuiteQL に対し HTTPS リクエスト発射
09:00:06  ⑦ NetSuite 側で認証検証・ロール権限チェック・Saved Search 実行
09:00:08  ⑧ レスポンス受信・JSON パース・フィールドマッピング
09:00:09  ⑨ PG `nst.*` にトランザクション内で書込・監査ログ記録
09:00:30  ⑩ 4 ドメイン分完了、`_pull_runs` 終了行更新、CLI exit
09:00:30  ⑪ ps1 が exit code を判定、失敗時のみ Lark webhook 発射
```

実際の所要時間は domain 規模に依存（items 数千行 / sales 万行レベル）、概ね 30 秒〜 5 分。

---

### 3.2 段階①〜② — トリガと起動

**コンポーネント**：Windows Task Scheduler + [inspiron-daily-pull.ps1](inspiron-daily-pull.ps1)

**詳細**：

- Windows Task Scheduler が `Smikie\NST-DailyPull` として登録される
- 実行アカウント：**専用サービスアカウント**を推奨（個人アカウントで実行しない）
- PowerShell スクリプトが `D:\Smikie-Database\logs\nst-daily-pull-YYYY-MM-DD.log` にログ書込

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| スケジューラの改ざん | 第三者が時刻 / コマンドを変更 | Task の編集権限を Administrators に限定、変更履歴を Windows Event Log で監視 |
| サービスアカウントの権限 | 過剰権限で他システムまで動かせる | 当該アカウントは Inspiron ローカルのみ、ドメイン参加不可 |
| ログファイルへのアクセス | 第三者が `D:\Smikie-Database\logs\` を読取可能 | NTFS ACL で `nst_service` + `Administrators` のみ読取可 |
| Bypass 実行ポリシー | `-ExecutionPolicy Bypass` を悪用される | スクリプトの設置パスを ACL で保護、書込権限を限定 |

---

### 3.3 段階③ — Docker exec で CLI 起動

**コマンド**（[ps1:57](inspiron-daily-pull.ps1#L57)）：

```pwsh
docker exec $containerName python -m data_warehouse.nst_api.daily_pull --domains all --since $sinceArg
```

**詳細**：

- `database-app` コンテナ（docker-compose で起動済の常駐コンテナ）に exec
- コンテナ内 python プロセスは **コンテナ起動時に注入された環境変数**で認証情報を受け取る
- コマンドライン引数には認証情報を **含めない**（プロセスリストから漏れないように）

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| Docker socket への接続 | コンテナ脱出 → ホスト権限取得 | Docker socket をマウントするコンテナを最小化、daily_pull コンテナには socket をマウントしない |
| コンテナイメージの改ざん | 悪意のあるイメージで CLI を差し替え | イメージは社内 git からビルド、第三者 registry から pull しない |
| コンテナ間ネットワーク | 他コンテナから daily_pull が見える | docker-compose の network を分離、daily_pull の出口は NetSuite + PG のみ |

---

### 3.4 段階④ — 認証情報の読込と client 構築

**コンポーネント**：[client.py:243-266](client.py#L243-L266) `build_client()`

**詳細**：

- `NST_ACCOUNT_ID` / `NST_AUTH_MODE` / 認証情報グループを環境変数から取得
- `NST_AUTH_MODE=oauth2` の場合：`OAuth2Auth` インスタンス構築（client_id + cert_id + cert_path）
- `NST_AUTH_MODE=tba` の場合：`TBAAuth` インスタンス構築（4 トークン）
- 環境変数欠如時：`RuntimeError("KEY env 未設定")` で即時失敗（[client.py:271](client.py#L271)）

**認証情報の保管場所**：

| 項目 | 場所 | アクセス権 |
|---|---|---|
| `.env` ファイル | `D:\Smikie-Database\.env` | NTFS ACL で `nst_service` + `Administrators` のみ |
| OAuth2 PEM 秘密鍵 | `D:\Smikie-Database\nst-cert.pem` | コンテナへ read-only bind mount |
| `DATABASE_URL` | 同 `.env` | 同上 |

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| .env の git 漏洩 | 平文 secret が git 履歴に残る | `.gitignore` で除外済、リポジトリ全文 grep で確認済（本書執筆時点） |
| PEM 秘密鍵の漏洩 | 攻撃者が NetSuite に全アクセス可能化 | NTFS ACL + BitLocker + 物理アクセス制限（第 12 章） |
| 環境変数のメモリダンプ | プロセスメモリから secret 取得 | Inspiron への物理 / RDP アクセス制御で対応 |
| 環境変数の子プロセス継承 | サブプロセス起動時に secret が漏れる | daily_pull は外部プロセスを起動しない設計 |
| 認証情報ローテーション未実施 | 漏洩時の被害窓口拡大 | **90 日周期のローテーション SOP**（DX部 が実施） |

---

### 3.5 段階⑤ — トークン取得 / 署名生成

#### 3.5.1 OAuth 2.0 モード（推奨）

**コンポーネント**：[client.py:128-175](client.py#L128-L175)

**プロセス**：

1. PEM 秘密鍵をロード（`open(cert_path, "rb")`）
2. JWT assertion を生成：
   ```
   header:  {"alg": "PS256", "kid": <cert_id>}
   payload: {
     iss:   <client_id>,
     scope: "rest_webservices",            ← REST スコープのみ、UI / 書込権限を要求しない
     aud:   https://<account>.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token,
     iat:   now,
     exp:   now + 3600
   }
   ```
3. NetSuite トークンエンドポイントに POST：
   ```
   grant_type=client_credentials
   client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
   client_assertion=<上記 JWT>
   ```
4. アクセストークン取得（NS は 60 分有効を発行）
5. メモリにキャッシュ、有効期限 10 分前から自動更新（[client.py:169](client.py#L169)）

**セキュリティ特性**：

| 特性 | 評価 |
|---|---|
| 短期トークン | ✅ 60 分有効、漏洩窓口が小さい |
| 秘密鍵ベース | ✅ NetSuite に届くのは公開鍵のみ、私鍵は社内に留まる |
| スコープ最小 | ✅ `rest_webservices` のみ、UI ログイン不可 |
| アルゴリズム | ✅ PS256（RSASSA-PSS）、NetSuite 推奨の現代的アルゴリズム |
| トークンの保管 | ⚠️ プロセスメモリのみ、ディスク永続化なし → 漏洩リスク低 |

#### 3.5.2 TBA モード（レガシー）

**コンポーネント**：[client.py:59-108](client.py#L59-L108)

**プロセス**：各リクエストごとに nonce + timestamp を生成、HMAC-SHA256 で署名

**セキュリティ特性**：

| 特性 | 評価 |
|---|---|
| 長期 secret | ❌ 4 トークン全てが長期有効、ローテーションコスト大 |
| アルゴリズム | ✅ HMAC-SHA256、現代基準で十分強度 |
| 漏洩リスク | ❌ 4 secret 全てを保管する必要、漏洩面が広い |

**DX 推奨**：OAuth 2.0 をデフォルト、TBA は NetSuite 側ロールが JWT 非対応時のフォールバックのみ。

---

### 3.6 段階⑥ — NetSuite への HTTPS リクエスト発射

**コンポーネント**：[client.py:195-220](client.py#L195-L220) `_request()`

**通信仕様**：

| 項目 | 値 |
|---|---|
| プロトコル | HTTPS (TLS 1.2+、NetSuite 側強制) |
| ホスト | `<account>.suitetalk.api.netsuite.com` |
| ポート | 443 |
| 認証ヘッダ | `Authorization: Bearer <token>`（OAuth2）or `OAuth ...`（TBA）|
| Content-Type | `application/json` |
| 特殊ヘッダ | `prefer: transient`（NS の SuiteQL transient 結果用） |

**HTTP メソッド使用範囲**：

| メソッド | 用途 | 本システム内 |
|---|---|---|
| GET  | REST レコード取得 | ✅ 使用 |
| POST | SuiteQL クエリ | ✅ 使用 |
| PUT / PATCH / DELETE | レコード変更 / 削除 | ❌ **使用しない（読込専用設計）** |

**ネットワーク経路**：

```
Inspiron 5405（社内 LAN）
   │
   │  HTTPS:443 (TLS 1.2+)
   │  プロキシ経由（社内プロキシ設定がある場合）
   ▼
社内 firewall（egress）
   │
   │  whitelist 必要：*.suitetalk.api.netsuite.com
   ▼
Internet
   │
   ▼
NetSuite クラウド
```

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| TLS 中間者攻撃 | DNS / 証明書を改ざんし通信傍受 | TLS 1.2+ 強制（NS 側）、システム CA 信頼ストア管理 |
| プロキシ経由の傍受 | 社内プロキシが TLS 終端し平文化 | NetSuite ホストを TLS terminating proxy の bypass リストに加える |
| egress 制御 | 不要なドメインへの out-bound | firewall で `*.suitetalk.api.netsuite.com` + `open.feishu.cn` のみ allow、他は deny |
| DNS 改ざん | 偽 NetSuite ドメインへ誘導 | 社内 DNS の信頼性管理、できれば DNSSEC |
| 全リクエストの認可検証 | 認証情報がリクエスト毎に検証されないリスク | NetSuite 側で毎リクエスト検証（NS の責任範囲、本書範囲外） |

---

### 3.7 段階⑦ — NetSuite 側の認証検証・ロール権限チェック・Saved Search 実行

**NetSuite 側責任範囲だが、本システム設計と連動する重要事項**：

| 項目 | 設定 |
|---|---|
| Integration の認証フロー | JWT bearer assertion（OAuth 2.0 client credentials）|
| Integration に紐づけるロール | **`SmikieJP_輸出事業_DataPull_ReadOnly`**（専用ロール、新規作成）|
| ロールの権限 | (a) REST Web Services 有効 (b) 対象 Saved Search 実行可 (c) item / cost / inventory / sales レコード閲覧可 (d) **書込権限すべて無効** |
| Saved Search の department フィルタ | **`department = 輸出事業` を必須付与**（第 2 章 層1） |
| ロールの IP 制限 | NetSuite 側で Inspiron のグローバル IP を許可リスト化（推奨） |
| 監査ログ | NetSuite Login Audit Trail で API ログイン履歴を確認可能 |

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| ロール過剰権限 | API が UI と同じ範囲にアクセス可能化 | DX部 が NetSuite 管理者に **権限スクリーンショット取得を依頼**、本書付録 D に添付 |
| 他事業データの混入 | Saved Search フィルタ漏れ | 第 2 章 層1 + 層2 二重防御 |
| Integration の有効期限 | 期限切れで pull 全停止 | NetSuite Integration 設定の Expiration 確認、無期限化または更新 SOP 設定 |
| ロール変更の追跡 | 知らぬ間にロール権限が拡大 | DX部 が月次で NetSuite ロール定義を export して diff チェック |

---

### 3.8 段階⑧ — レスポンス受信・JSON パース・フィールドマッピング

**コンポーネント**：[client.py:206-211](client.py#L206-L211) + 4 個の `pull_*.py`

**プロセス**：

1. HTTPS レスポンス受信（最大 1000 行 / ページ、SuiteQL）
2. `urllib.request.urlopen` でストリーム読込、JSON パース
3. NS フィールド → PG カラムのマッピング（`pull_*.py` に定義、D-101 ① 後実装）
4. 大きな payload はメモリのみで処理、ディスクに一時保存しない

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| 大量レスポンス → メモリ枯渇 | DoS / ノード OOM | SuiteQL は 1000 行ページング（[client.py:228-240](client.py#L228-L240)）、過大データ取得時は警告 |
| JSON injection | 攻撃者制御のフィールド値が PG SQL を破壊 | psycopg parameterized query 必須（D-101 ① 後実装、SQL組立て禁止） |
| 機密フィールドのログ出力 | コンソール / log に cost 等が漏れる | **DEBUG ログは prod 環境で無効化**、`-v` フラグは検証時のみ |
| レスポンス改ざん | TLS 通過後にローカルで書換 | TLS 完了点 = メモリ内、Python プロセス信頼面内のみ |
| 文字エンコーディング攻撃 | UTF-8 不正シーケンスでパース誤動作 | `json.loads(raw.decode())` で標準 utf-8 デコード、不正は例外で停止 |

---

### 3.9 段階⑨ — PG `nst.*` への書込

**コンポーネント**：4 個の `pull_*.py` 内の書込ロジック（D-101 ① 後実装）+ [sql/000_create_nst_schema.sql](sql/000_create_nst_schema.sql)

**プロセス**（実装予定）：

1. PG に接続（`DATABASE_URL` 環境変数 → psycopg）
2. **単一トランザクション**内で：
   a. `nst._pull_runs` に `started_at` 行 INSERT、`run_id` 取得
   b. 各 domain について：
      - `nst.<domain>_raw` に行 INSERT / UPSERT
      - 失敗行は `nst._pull_errors` に raw_row JSONB で記録
   c. 完了時 `nst._pull_runs.finished_at` + `summary_json` を UPDATE
3. **失敗時**：トランザクション ROLLBACK、`_pull_runs.overall_status='failed'` で別トランザクションに記録

**接続セキュリティ**：

| 項目 | 設定 |
|---|---|
| PG 接続文字列 | `postgresql://nst_writer:<password>@localhost:5432/smikie` |
| 接続方式 | Unix socket または localhost TCP（外部からの接続不可） |
| SSL | 同一ホスト内のため不要（外部 PG なら必須） |
| ロール | `nst_writer` — `nst.*` への INSERT / UPDATE のみ、他 schema 不可、DROP 不可 |

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| SQL injection | フィールド値で SQL 破壊 | psycopg parameterized query、SQL 文字列組立て禁止 |
| ロール越権 | nst_writer が他テーブルを破壊 | PG ロール権限を `nst.*` に限定、`REVOKE ALL ON SCHEMA public` |
| トランザクション漏れ | 中途半端な状態で残る | 全ドメインを単一トランザクションでくくる、または domain 毎独立トランザクション |
| ロックタイムアウト | 書込中に他クエリがブロック | `lock_timeout` 設定、大量更新は時間外帯のみ |
| バックアップ整合性 | dump 中に書込が走る | `pg_dump` は別時間帯で実行、または `--snapshot` 使用 |
| 監査テーブルの改ざん | アプリロールが `_pull_errors` を削除 | `_pull_errors` は `nst_writer` に INSERT のみ、UPDATE / DELETE 権限なし（append-only） |

---

### 3.10 段階⑩〜⑪ — 終了処理・失敗時の通知

**プロセス**：

1. CLI exit code を判定：0 (全 ok) / 1 (一部失敗) / 2 (env 欠如) / 3 (build_client 失敗)
2. exit ≠ 0 の場合：
   a. PowerShell が exit code を捕捉
   b. ログ末尾 20 行を抽出
   c. Lark webhook に POST：
      ```json
      {
        "msg_type": "text",
        "content": {
          "text": "🚨 NST daily_pull · exit=1\n<ログ末尾 20 行>\n時間: 2026-05-18T09:00:30Z"
        }
      }
      ```

**セキュリティ関心事**：

| 項目 | リスク | 対策 |
|---|---|---|
| Lark webhook URL 漏洩 | 第三者が偽通知送信可 | webhook URL は `.env` 内、git 不入、ローテーション SOP |
| webhook payload に機密混入 | ログにフィールド値が出力され漏洩 | コード側で値レベルのログは出さない、SQL パラメータも値マスキング |
| Lark グループメンバー | 外部連絡先が混入 | グループメンバーは経営者 + DX部 + 財務責任者に限定、月次レビュー |
| webhook 障害時の sileent fail | 失敗が伝わらない | ps1 側で webhook 失敗をローカルログに必ず記録（[ps1:43](inspiron-daily-pull.ps1#L43)） |
| Lark 側の保存期間 | 機密が Lark クラウドに長期保存 | Lark のメッセージ保存ポリシーを確認、必要なら定期削除 |

---

### 3.11 拉取プロセス全体のセキュリティチェックポイントまとめ

| # | 段階 | 内部視点チェック | 外部視点チェック |
|---|---|---|---|
| 1 | Task Scheduler | サービスアカウント権限最小化 | スケジューラ改ざん防止 |
| 2 | PS1 起動 | スクリプト書込権限制限 | スクリプト経路保護 |
| 3 | Docker exec | コンテナ間隔離 | Docker socket 保護 |
| 4 | client 構築 | .env / PEM ACL | git 漏洩防止、ローテーション |
| 5 | トークン取得 | scope 最小（rest_webservices）| 短期トークン、PS256 |
| 6 | HTTPS 送信 | egress whitelist | TLS 1.2+ 強制、DNS 信頼 |
| 7 | NS 側認証 | ロール権限最小 + dept filter | NS Login Audit Trail |
| 8 | レスポンス処理 | DEBUG ログ無効化 | JSON 検証、SQL injection 防御 |
| 9 | PG 書込 | nst_writer ロール最小 | localhost のみ、外部接続不可 |
| 10 | 終了通知 | Lark グループ限定 | webhook URL 保護 |

---

## 4. 外部攻撃面の制御

第 3 章で各段階のセキュリティ事項を網羅したため、本章ではトップレベルの **外部攻撃シナリオ** ごとに整理する。

### 4.1 攻撃シナリオ × 防御マップ

| # | 外部攻撃シナリオ | 想定攻撃者 | 第 3 章対応段階 | 防御状況 |
|---|---|---|---|---|
| E1 | NetSuite 認証情報の盗取 | 外部攻撃者 / 内部関係者 | 段階④ | ✅ NTFS ACL + BitLocker + git 除外 + 90 日ローテーション |
| E2 | TLS 通信の傍受・改ざん | 中間ネットワーク経路上の攻撃者 | 段階⑥ | ✅ TLS 1.2+ 強制、NS ホストはプロキシ bypass |
| E3 | NetSuite Integration の乗っ取り | 認証情報入手後の攻撃者 | 段階⑦ | ✅ NS 側 IP allowlist、ロールは読込専用 |
| E4 | DNS 改ざん → 偽 NetSuite | 内部 LAN への侵入者 | 段階⑥ | ⚠️ 社内 DNS 信頼性管理、DNSSEC 推奨 |
| E5 | プロキシ TLS terminating で平文化 | 社内プロキシ管理者 / 侵入者 | 段階⑥ | ✅ NS ホストを bypass リスト化 |
| E6 | Inspiron 物理侵入・盗難 | 物理アクセス可能者 | 段階④ | ⚠️ BitLocker 必須化、施錠区画配置（第 12 章） |
| E7 | Docker socket 経由のコンテナ脱出 | コンテナ内権限取得者 | 段階③ | ✅ daily_pull コンテナに socket 非マウント |
| E8 | サプライチェーン攻撃（PyJWT 等）| 依存ライブラリ改ざん者 | 段階⑤ | ⚠️ uv.lock pin、定期 vulnerability scan |
| E9 | Lark webhook URL 盗取 → 偽通知 | webhook URL 入手者 | 段階⑩ | ✅ .env 内、git 不入、ローテーション |
| E10 | PG 外部接続 | PG ポート露出 | 段階⑨ | ✅ localhost listen のみ、firewall block |
| E11 | バックアップ NAS への侵入 | NAS LAN アクセス者 | 別経路 | ⚠️ NAS 権限縮小 + GPG 暗号化（第 11 章） |

### 4.2 NetSuite 側の依存事項

NetSuite 自体のセキュリティは NS（Oracle）の責任範囲だが、本システム運用上以下を期待する：

- 認証ロール変更の監査ログ提供
- API レート制限 / 異常パターン検知
- セキュリティパッチの自動適用
- 認証情報漏洩通知の SLA

DX部 は NetSuite 管理者と **年次でセキュリティ責任分界点を再確認** することを推奨。

---

## 5. データ資産分類（C0-C3、輸出事業限定）

全項目が輸出事業データに限定される前提で分類：

| 等級 | 意味 | フィールド例 | 閲覧可能者 |
|---|---|---|---|
| **C3 機密** | 漏洩が直接的に競争力 / 財務に影響 | `最安原価`、`利益率`、`仕入価格`、`サプライヤー別cost` | 経営者・輸出事業財務責任者・限定 IT 管理者 |
| **C2 制限** | 社内経営データ、部門内可視 | `売上金額`、`受注先`、`粗利`、`在庫月数` | 経営者・輸出事業営業・輸出事業財務（担当切片）|
| **C1 社内** | 社内公開、影響軽微 | `item master`（品名・JAN・規格）、`在庫数量` | 輸出事業全員 |
| **C0 公開** | 既に対外公開済 | 商品名称（Shopify 上で表示中の部分）| 制限なし |

### 5.1 4 ドメインのフィールド分類予測

| domain | 主分類 | 備考 |
|---|---|---|
| `items` | C1（大半）+ C3（cost フィールドを含む場合）| Saved Search に cost 列を含めるかは D-101 ① で決定、**テーブル全体の分類に直結** |
| `costs` | **テーブル全体 C3** | 最安原価は中核機密、下流消費はすべて独立レビュー必要 |
| `inventory` | C1 主体 + C2（在庫月数 / 欠品予警）| 「欠品 × 販売速度」の組合せから競争戦略を逆推可能 |
| `sales` | **テーブル全体 C2**、一部フィールド C3 | 金額・受注先 は個人情報または法人取引情報を含む |

**運用約束**：D-101 ① で Saved Search 拍板時、各フィールドに C 等級を必ず付記。未付記フィールドはデフォルト **C3 扱い**（fail-secure）。

---

## 6. データフローマップ（社内視点）

```
        NetSuite（外部、経営ソースデータ）
            │
            │  ※輸出事業 department フィルタ必須
            │  HTTPS:443 TLS 1.2+
            ▼
        ╔═══════════ 信頼境界（社内 firewall）═══════════╗
            │
            ▼  daily_pull 09:00（Inspiron 専用サービスアカウント）
    ┌─────────────────────────────┐
    │   PG nst.*（社内 PG）         │  ← 本書の中核「データ落地点」
    │   C1/C2/C3 混在               │
    │   CHECK 制約：輸出事業のみ     │
    └────────┬──────────────────────┘
             │
   ┌─────────┼──────────┬──────────┬──────────┐
   ▼         ▼          ▼          ▼          ▼
CMS Streamlit  N8N    pgweb 管理  Boss laptop  バックアップ NAS
(社内 Web)   (workflow) (DBA UI)  (pgweb 直結) LS210DC
   │            │          │           │           │
   ▼            ▼          ▼           ▼           ▼
従業員       自動化出力  管理者 SQL  個人クエリ   保管媒体
ブラウザ
   │            │          │           │           │
   ▼            ▼          ▼           ▼           ▼
CSV ダウンロード Slack/Lark dump 出力  ローカル    外部？誰が取れる？
スクショ送信   メール添付  pg_dump   Excel
印刷
```

下流 7 出口をそれぞれ権限審定（第 7 節 RBAC + 第 9 節 出口制御）。

---

## 7. ロールとアクセスマトリックス（RBAC）

| ロール | 人員例 | items (C1+C3) | costs (C3) | inventory (C1+C2) | sales (C2+C3) | 備考 |
|---|---|---|---|---|---|---|
| **経営者** | Boss | R | R | R | R | 全フィールド |
| **輸出事業 財務** | （TBD）| R（cost 列除く）| R | — | R | 金額必要、在庫不要 |
| **輸出事業 営業** | （TBD）| R（cost 列除く）| — | R | R（本人/本組 client 行のみ）| RLS で担当者フィルタ |
| **輸出事業 倉庫** | （TBD）| R（cost 列除く）| — | R | — | 在庫のみ、金額不可視 |
| **DX / IT** | （TBD）| R（cost 列除く）| — | — | — | 保守用、**cost と sales はデフォルト不可視** |
| **退職 / 異動** | — | — | — | — | — | HR 起動後 24h 以内に回収 |

**他事業（CB / 国内）所属者**：ロール定義に存在しない、`nst.*` への GRANT を一切付与しない。

### 7.1 実装メカニズム（PG 層）

- `GRANT SELECT (col1, col2, …)` 列レベル授権、機密列はデフォルト未付与
- Row Level Security (`CREATE POLICY`) で `tantousha_code` による sales 行フィルタ
- ビュー層：`nst.items_for_eigyou`、`nst.sales_for_zaimu` 等、下流はビューのみクエリ可
- 原テーブル `nst.item_master_raw` / `nst.cost_daily` 等は `nst_dba` + `nst_writer` のみ

**現在の scaffolding 段階**：上記 4 ロール + ビューは未作成。D-101 ① 拍板後の次 ticket（提案番号 T-NST-002）にて実装。

---

## 8. 下流コンシューマ隔離

| コンシューマ | 接続方式 | PG ロール | 禁止操作 |
|---|---|---|---|
| **CMS Streamlit** | 直結 PG | `cms_reader`（ビュー読込専用） | 原テーブル不可、INSERT/UPDATE 不可、COPY TO 不可 |
| **N8N** | 直結 PG | `n8n_reader`（items/inventory ビューのみ）| cost / sales 不可 |
| **pgweb 管理 UI** | 直結 PG | 強制 `nst_dba` + 二段階確認 | 操作ログ全件 |
| **Boss laptop（個人 pgweb）** | CF Access + Tailscale | `boss_full` | — |
| **daily_pull コンテナ** | docker network local | `nst_writer` | cms.\* 不可、DROP 不可 |
| **バックアップ** | `pg_dump` | `nst_backup_ro` | 必要 schema のみ |
| **臨時分析** | — | 禁止、ビュー経由必須 | — |

**核心原則**：アプリケーション用途の default-allow PG superuser を提供しない。

---

## 9. データ流出経路と制御

データが PG に落ちた後の **真の漏洩リスク経路 8 つ**：

| 経路 | 起動者 | 機密度 | 現状制御 | 追加制御 |
|---|---|---|---|---|
| CMS Streamlit CSV ダウンロード | 従業員 | ページに依る | なし | (a) cost 列を含むページはダウンロード禁止 (b) ダウンロード行動を監査 (c) CSV に watermark |
| CMS Streamlit スクリーンショット | 従業員 | ページに依る | なし | プロセス教育 + 月次レビュー + 画面 watermark |
| Boss スクショを Lark へ送信 | Boss 個人 | C3 可能性 | — | Lark グループメンバー審定 |
| Lark webhook 失敗アラート | 自動 | error message + ログ末尾 | コード側で業務行を送信せず | ps1 のログ出力複査 |
| NAS LS210DC への pg_dump | 日次定時 | DB 全体 C3 | NAS 共有 | (a) GPG 暗号化 (b) NAS 共有を `nst_backup` 限定 (c) 保持期間厳格化 |
| 従業員 SELECT 後のローカル Excel | 任意権限保有者 | 権限に依る | pgaudit 未有効 | pgaudit 有効化 + 月次異常パターンレビュー |
| N8N workflow 出力 | 自動 | workflow に依る | なし | 各 node 出力チャネル（Lark/メール/Shopee API）審定 |
| IT 管理者の臨時 cost 参照 | DX / IT | C3 | なし | cost 列アクセスは二人承認 + 全件記録 |

**実装順序**：

1. **即時**：PG pgaudit 有効化
2. **D-101 拍板後**：RBAC + ビュー
3. **次サイクル**：CSV ダウンロード監査 + watermark
4. **四半期**：バックアップ GPG 暗号化

---

## 10. アクセス監査

| 層 | 現状 | あるべき姿 |
|---|---|---|
| 書込監査（daily_pull → PG）| ✅ `nst._pull_runs` / `nst._pull_errors` | — |
| 読取監査（従業員 SELECT）| ❌ 完全未実装 | pgaudit 有効化、user / time / query / affected rows |
| 管理操作監査（DBA）| ❌ | pgaudit object class、GRANT/REVOKE/CREATE/DROP |
| 下流アプリ監査（CMS / N8N）| 各アプリ層で未整合 | CMS は page visit_user + viewed_columns、N8N は workflow run の PG クエリ |
| 物理 / 流出監査 | なし | CSV / pg_dump / NAS バックアップすべて trail |
| **NetSuite Login Audit Trail（外部）** | NS 側で取得可、未活用 | DX部 が月次 export して異常 API ログイン検知 |

**監査ログ自体のセキュリティ**：

- `audit.*` 独立 schema、全アプリロールに append-only（DELETE / UPDATE 不可）
- 保持期間 ≥ 2 年
- 月次：DX部 が異常パターンレビュー

---

## 11. データ保持と廃棄

| データ | 保持期間 | 廃棄方法 |
|---|---|---|
| `nst.item_master_raw` 等原テーブル | 3 年（財務監査周期）| 月次 partition → 旧月パーティション DROP |
| `nst._pull_runs` | 2 年 | 同上 |
| `nst._pull_errors` | 1 年 | 同上 |
| `audit.*` 監査ログ | 2 年 | — |
| PG 日次バックアップ（本機）| 30 日 | rolling rm |
| NAS LS210DC 月次バックアップ | 12 ヶ月 | rolling NAS 回収 |
| NAS LS210DC 年次バックアップ | 7 年（税法 / 商法）| 物理媒体廃棄記録 |

**重要決定**：「永久保持」はデフォルトで誤り、保持期間未定義 = セキュリティ負債。

---

## 12. 物理 / 環境層

| 項目 | 現状 | 提案 |
|---|---|---|
| Inspiron 5405 物理位置 | （要確認）| 社内ロック区画、共有 desk 不可 |
| Inspiron 全ドライブ暗号化 | （BitLocker 状態要確認）| BitLocker 必須、TPM バインド |
| USB ポート | デフォルト開放 | IT 管理者アカウントのみ、一般従業員無効化 |
| 画面ロック | OS デフォルト | 5 分自動ロック + Windows パスワード強制 |
| 複数ユーザ RDP | （要確認）| 多ユーザ RDP 禁止、daily_pull は専用サービスアカウント |
| NAS LS210DC 物理位置 | （要確認）| 同上 |
| 社内ネットワーク Segmentation | （要確認）| daily_pull ホスト用 VLAN 分離、PG ホストの egress 制御 |

---

## 13. 退職 / 異動 SOP

| イベント | 起動 | 24h 以内アクション | 責任者 |
|---|---|---|---|
| 退職 | HR | PG ロール DROP / CF Access 取消 / Lark グループ除外 / バックアップアクセス回収 | DX |
| 異動（部門変更）| HR | 新部門ビュー再 GRANT / 旧部門 REVOKE / 過去 30 日 SELECT 監査 | DX |
| ロール昇降 | 経営者 | RBAC 更新 + 通知 | DX |
| 長期休職 | HR | PG ロール停止、復職時再 GRANT | DX |
| **輸出事業から他事業への異動** | HR | `nst.*` への一切のアクセスを完全 REVOKE | DX |

**Tooling**：GRANT / REVOKE はすべて git 化された migration SQL、`psql` 直接権限変更不可。

---

## 14. 内部脅威モデル

| # | シナリオ | 機密度 | 阻断可？ | 既存制御 | 追加制御 |
|---|---|---|---|---|---|
| I1 | 営業員が cost CSV を競合に提供 | C3 | ❌ | 信頼のみ | 営業ロールから cost 列禁止 + ダウンロード監査 |
| I2 | 従業員が誤って sales スクショを Lark 外部グループに送信 | C2~C3 | ❌ | プロセス教育のみ | 画面 watermark + グループメンバー審定 |
| I3 | Boss laptop 盗難 | C3 | 部分対応 | BitLocker？要確認 | BitLocker 必須 + Tailscale 失効 |
| I4 | 退職従業員が依然 PG にアクセス | 権限に依る | ❌ | なし | 第 13 節 SOP |
| I5 | IT 管理者の cost 越権参照 | C3 | ❌ | なし | DX/IT に cost 未付与 + 二人承認 |
| I6 | NAS バックアップ持ち出し | C3 | ❌ | なし | GPG 暗号化 + NAS 権限縮小 |
| I7 | CMS Streamlit 越権アクセス | ページに依る | 部分 | CF Access | ページレベル RBAC + マスキング + 監査 |
| I8 | N8N が cost を Shopee API に出力 | C3 | ❌ | なし | n8n_reader に cost 未付与 |
| I9 | 臨時分析 SQL でテーブル全体越権 | フィールドに依る | ❌ | なし | 個人直結禁止、ビュー経由強制 |
| I10 | 従業員が pg_dump で DB 持ち出し | C3 | ❌ | なし | 一般ロールから pg_dump REVOKE |
| I11 | 輸出事業外部門人員が `nst.*` 参照 | 全範囲 | ❌ | なし | 他事業ロールに `nst.*` 一切 GRANT せず + 第 2 章三層防御 |

**現状 11 シナリオ中 9 つに技術的阻断なし、2 つに部分対応**。

---

## 15. 外部脅威モデル

| # | シナリオ | 想定攻撃者 | 阻断可？ | 既存制御 | 追加制御 |
|---|---|---|---|---|---|
| E1 | NetSuite 認証情報の盗取 → API 経由データ漏洩 | 外部 / 内部関係者 | ✅ 部分 | NTFS ACL、git 除外、90 日ローテーション SOP | HSM / Windows DPAPI 検討 |
| E2 | TLS 中間者攻撃 | ネットワーク経路上 | ✅ | TLS 1.2+ 強制、bypass proxy | DNSSEC、Certificate Pinning（推奨）|
| E3 | NetSuite Integration 乗っ取り | 認証情報取得後 | ✅ 部分 | NS 側 IP allowlist、ロール読込専用 | NS 側 IP allowlist の正式設定要求 |
| E4 | DNS 改ざん | 内部 LAN 侵入者 | ⚠️ | 社内 DNS 信頼性 | DNSSEC 検討、HSTS preload |
| E5 | プロキシ TLS terminating で平文化 | 内部プロキシ管理者 | ✅ | NS ホストの bypass | プロキシ管理者 SOP 整備 |
| E6 | Inspiron 物理侵入・盗難 | 物理アクセス可能者 | ⚠️ | — | BitLocker 必須化、施錠区画 |
| E7 | Docker socket 経由のコンテナ脱出 | コンテナ内権限取得 | ✅ | socket 非マウント | 定期 docker security scan |
| E8 | サプライチェーン攻撃（PyJWT 等）| 依存ライブラリ改ざん者 | ⚠️ | uv.lock pin | 定期 `pip-audit` / `safety` scan + lock 更新 SOP |
| E9 | Lark webhook URL 盗取 → 偽通知 | URL 入手者 | ✅ | .env 内、git 不入 | webhook ローテーション SOP |
| E10 | PG ポート露出 → 外部接続 | LAN 侵入者 | ✅ | localhost listen のみ | firewall outbound 監視 |
| E11 | バックアップ NAS への侵入 | NAS LAN アクセス者 | ⚠️ | NAS 共有権限 | NAS 権限縮小 + 暗号化 |
| E12 | API レート超過攻撃（DoS）| 外部攻撃者 | ✅ | NS 側レート制限 | 自社側でも monitoring 設定 |

---

## 16. D-101 ブロックと連動セキュリティ決定

| D-101 決定項 | データセキュリティ連動影響 | DX 同期決定要件 |
|---|---|---|
| ① Saved Search フィールド規定 | 原テーブル分類決定 + **輸出事業 department フィルタ必須付与** | フィールド級 C 等級リストを Saved Search 決議と一括出力 |
| ② OAuth2 vs TBA | 認証情報ローテーション SOP を決定 | 90 日周期 / HSM 検討 |

**scaffolding 段階に真の業務データ未投入** — DX が RBAC + ビュー + 監査 SOP を策定する最もクリーンなタイミング。D-101 拍板 + 真認証情報投入時、**第 7 / 9 / 10 節の制御を同時にローンチすべき**、裸起動して後から追加不可。

---

## 17. DX部 重点レビュー / 決議事項

| # | 議題 | 決議形式 | ブロック条件 | 視点 |
|---|---|---|---|---|
| 1 | 4 ドメインのフィールド級 C 等級リスト | 表 + Boss 承認 | D-101 ① | 内部 |
| 2 | RBAC ロールとビュー設計（第 7 節） | T-NST-002 ticket | D-101 ① 後 | 内部 |
| 3 | pgaudit 有効化 + 監査ログ保持期間 | 決議 + migration SQL | 即時 | 内外 |
| 4 | CSV ダウンロード制御方針 | CMS 側落地方針 | 即時 | 内部 |
| 5 | データ保持期間正式承認 | Boss + 財務 連署 | D-101 ② 前 | 内部 |
| 6 | 退職 / 異動 SOP の正式業務フロー化 | HR + DX 共同 | 即時 | 内部 |
| 7 | バックアップ GPG 暗号化 + NAS 権限縮小 | DX 実装 | 即時 | 内部 |
| 8 | 画面 watermark + Lark グループメンバー審定 | DX + 経営者 | 即時 | 内部 |
| 9 | Inspiron 物理 / BitLocker / USB 方針 | DX 実装 | 即時 | 外部 |
| 10 | 内部脅威シナリオ #I1-#I11 承認または反論 | DX 会議議事録 | 即時 | 内部 |
| 11 | 外部脅威シナリオ #E1-#E12 承認または反論 | DX 会議議事録 | 即時 | 外部 |
| 12 | **輸出事業限定三層防御 正式承認** | 経営者 + DX + 財務 三者承認 | 即時 | 内部 |
| 13 | 認証情報ローテーション 90 日周期承認 | DX 実装 | 即時 | 外部 |
| 14 | NetSuite ロール権限スクリーンショット要求 | DX → NS 管理者 | 即時 | 外部 |
| 15 | サプライチェーン scan SOP（pip-audit）| DX 実装 | 即時 | 外部 |
| 16 | DNS 改ざん対策（DNSSEC 検討）| DX 検討 | 中長期 | 外部 |
| 17 | 社内ネットワーク VLAN segmentation | IT インフラ | 中長期 | 外部 |

**議題 3 / 4 / 6 / 7 / 8 / 9 / 10 / 11 / 12 / 13 / 14 / 15** は D-101 拍板に依存しない、即時推進可能。

---

## 付録 A：関連ファイル

- 認証コア：[client.py](client.py)
- CLI 編成：[daily_pull.py](daily_pull.py)
- 4 ドメイン stub：[pull_items.py](pull_items.py) / [pull_costs.py](pull_costs.py) / [pull_inventory.py](pull_inventory.py) / [pull_sales.py](pull_sales.py)
- PG 監査 schema：[sql/000_create_nst_schema.sql](sql/000_create_nst_schema.sql)
- スケジューラスクリプト：[inspiron-daily-pull.ps1](inspiron-daily-pull.ps1)
- 業務説明：[README.md](README.md)
- タスクコンテキスト：[T-NST-001](../../../.tasks/doing/T-NST-001-nst-api-daily-pull.md)
- 決定依存：[T-003 D-101](../../../.tasks/backlog/T-003-d-101-shouhin-decisions.md)
- 中国語版（原文）：[SECURITY-DX-REVIEW.md](SECURITY-DX-REVIEW.md)

## 付録 B：用語集

| 用語 | 意味 |
|---|---|
| NST | NetSuite（社内略称） |
| daily_pull | 1 日 1 回 NetSuite からデータを取得するプロセス全体 |
| SuiteQL | NetSuite の SQL 風クエリ言語、REST API 経由実行 |
| TBA | Token-Based Authentication、NetSuite レガシー認証 |
| JWT | JSON Web Token、OAuth 2.0 で使用 |
| RBAC | Role-Based Access Control |
| RLS | Row Level Security（PostgreSQL ネイティブ機能）|
| pgaudit | PostgreSQL の監査ログ拡張 |
| C0-C3 | データ機密度分類（C3 が最高機密）|
| 三層防御 | 取得層 + 格納層 + 消費層、輸出事業フィルタ三重実装 |

## 付録 C：DX レビュー Quick Checklist

DX レビュー会議で 30 分以内に判定するための要点：

```
□ 第 2 章 三層防御を承認するか？
□ 第 3 章 拉取プロセス全 10 段階に追加リスクがあるか？
□ 第 7 章 RBAC マトリックスを承認するか？
□ 第 13 章 退職 / 異動 SOP を HR と合意できるか？
□ 第 14 章 内部脅威 11 シナリオに新規追加が必要か？
□ 第 15 章 外部脅威 12 シナリオに新規追加が必要か？
□ 第 17 章 17 議題のうち、即時着手すべきもの優先順位？
□ scaffolding 段階 = 制御整備の最適タイミング、この機会を逃さないことに合意するか？
```
