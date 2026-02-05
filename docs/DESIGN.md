<!--
Role: 仕様の"正"として、要件・データ形式・実行形態・プライバシー方針・（将来の）Notion方針をまとめる。
How: 実装より先に意思決定を固定し、変更が出たらここに追記してから実装に反映する。
Key sections: 出力、スキーマ、スケジューリング、プライバシー、Notion、確定方針。
Collaboration: 実装詳細は ARCHITECTURE.md と各ソースファイル先頭コメントに寄せる。
-->
# everlog 設計仕様 v0.2

## 1. 背景 / 目的
スクリーンショット + ローカルOCRから、作業ログを自動生成する。

- 一日を振り返る「日報」ではなく、**作業ログ**（何を何分/何時間やったか、いつ何をしていたか）を作る
- OCRはmacOSローカル（Vision）で実行し、外部OCR課金を避ける
- 収集データはまずローカルにJSONLで蓄積し、日次でMarkdownに整形する
- 将来的にNotionへも同期できる構造にする（ただし最初はMarkdownが正）

## 2. ゴール / 非ゴール
### ゴール
- 5分おき（可変）に「その時点の作業コンテキスト」をサンプリングしてJSONLに追記
- 1日分のJSONLから、以下をMarkdownで出力
  - その日の記録期間（最初〜最後）とキャプチャ数
  - **アプリ別**の合計推定時間
  - （可能なら）**Webサイト（ドメイン）別**の合計推定時間
  - **タイムライン**（時刻→作業ラベル/アプリ/ドメイン/短い要約）
- 画像は原則保存しない（必要ならデバッグ設定で保存可）

### 非ゴール（v0.1時点）
- 厳密な稼働時間の計測（サンプリングなので推定）
- キー入力/マウス操作の完全ログ（プライバシー/実装難度が高い）
- OCR精度の最大化（多少雑でも「傾向把握」を優先）
- ヒューマンインザループによる文脈付与（初期はこだわらない）
- 長期間やっている作業に対しての文脈付与（初期はこだわらない）

## 3. 期待アウトプット（最終形）
### 3.1 日次Markdown（例）
- ファイル名例: `EVERYTIME-LOG/out/2026-02-03.md`
- 構成:
  - サマリ（記録期間、キャプチャ数、推定総時間）
  - 本日のメイン作業（推定）: TOP3の作業名と時間（LLM付与時は要約も）
  - アプリ使用状況（推定）: アプリ別の推定時間・使用回数・使用傾向・主な用途
  - タイムライン（推定）: セグメント単位（連続した同一作業をまとめ）で時刻・作業名・時間を列挙

### 3.2 LLM要約ファイル（任意）
- ファイル名例: `EVERYTIME-LOG/out/2026-02-03.llm.json`
- OpenAI APIを使って各セグメントに「task_title」「task_summary」「category」「confidence」を付与
- `enrich` コマンドで生成し、`summarize` がこれを参照してMarkdownに反映する

### 3.3 ログ（JSONL）
- ファイル名例: `EVERYTIME-LOG/logs/2026-02-03.jsonl`
- 1行=1キャプチャのイベント（スキーマは後述）

## 4. 用語
- **Capture**: 1回のサンプリング（スクショ→OCR→JSONL追記）
- **Event**: JSONLの1行
- **Interval**: キャプチャ間隔（デフォルト 5分）
- **Context**: 前面アプリ、ウィンドウタイトル、（可能なら）ブラウザURL（OCRはディスプレイ別に保持）

## 5. 全体アーキテクチャ
### 5.1 データフロー
1. スケジューラ（launchd / 手動）で `capture` を起動
2. 前面アプリ/ウィンドウタイトル/（可能なら）URLを取得（v0.2はChromeのみ）
3. 全ディスプレイを `screencapture` で一時ファイルに保存（1ディスプレイ=1枚）
4. ディスプレイごとにVision OCRでテキスト抽出
5. ディスプレイごとに除外判定/マスキングし、`ocr_by_display` に記録 → JSONLに追記
6. 一時画像を削除（デフォルト）
7. 日次で `enrich`（任意）→ `summarize` を実行し、Markdownを生成
   - `enrich`: OpenAI APIでセグメントに作業名/要約を付与（`out/YYYY-MM-DD.llm.json`）
   - `summarize`: JSONLとLLM結果からMarkdownを生成

### 5.2 主要コンポーネント（実装済み）
- `collect.py`
  - AppleScriptで前面アプリ/ウィンドウタイトル取得
  - ブラウザURL取得（Chromeのみ）
- `capture.py`
  - `screencapture` 実行（ディスプレイごとに保存）、OCR/除外/マスクを経てJSONLに追記
- `ocr.py`
  - Vision OCR（Swift helper `EVERYTIME-LOG/bin/ecocr`）
- `exclusions.py` / `redact.py`
  - 除外判定（アプリ/ドメイン/キーワード）とマスキング
- `jsonl.py`
  - JSONL追記/読み取り
- `segments.py`
  - JSONLイベントを「作業セグメント」にまとめ、特徴量を抽出
- `enrich.py` / `llm.py`
  - OpenAI APIで作業セグメントに要約/ラベル付け
- `summarize.py`
  - JSONL→集計→Markdown（LLM結果があれば反映）
- `menubar.py`
  - メニューバー常駐UI（Start/Stop、間隔変更、除外設定、手動キャプチャ、日次生成、ステータス表示）
- `launchd.py`
  - LaunchAgent plist生成と `launchctl` 操作（capture/menubar/daily の3種）
- `export`
  - （将来）Notionへ同期

## 6. 実行形態（スケジューリング）
### 6.1 最小（手動）
- `everlog capture`
- `everlog enrich --date 2026-02-03`（任意: LLM要約）
- `everlog summarize --date 2026-02-03`

### 6.2 収集（推奨: launchd短命プロセス）
- `launchd` の LaunchAgent としてログイン時に定期起動
- v0.2は「短命プロセスを定期起動」を採用（5分間隔をデフォルト）
  - Install/Reload: `everlog launchd capture install`
  - Start: `everlog launchd capture start`
  - Stop: `everlog launchd capture stop`
  - Restart: `everlog launchd capture restart`
  - Status: `everlog launchd capture status`

### 6.3 日次処理（推奨: launchd StartCalendarInterval）
- 毎日23:55に `enrich` → `summarize` を自動実行
  - Install: `everlog launchd daily install`
  - Start/Stop/Restart/Status/Uninstall: 同様のサブコマンド

### 6.4 UI（推奨: メニューバー常駐）
要件: 起動/再起動、Start/Stop、間隔変更、除外設定、今すぐ1回キャプチャ、今日のMarkdown生成、今日のキャプチャ回数、前回のキャプチャ時間。

- 実装方針: **GUIアプリ（常駐）＋裏は短命**（GUIは操作と設定編集、収集はlaunchdの短命起動が担当）
- UIは `rumps` でメニューバー常駐
  - 手動起動: `everlog menubar`
  - ログイン時の自動起動（任意）: `everlog launchd menubar install`

## 7. 取得するデータ（スキーマ）
### 7.1 JSONL Event スキーマ（v0.1）
必須:
- `id` (string): UUID
- `ts` (string): ISO8601（例 `2026-02-03T09:15:00-08:00`）
- `tz` (string): `-08:00` のようなUTCオフセット
- `interval_sec` (number): 設定上の間隔（推定時間計算に利用）
- `active_app` (string): 例 `Arc`, `Terminal`
- `window_title` (string): 例 `PR #123 — GitHub`
- `active_context` (object): アクティブ情報の明示
  - `app` (string)
  - `window_title` (string)
  - `browser` (object | null): `browser` と同じ形式
- `ocr_by_display` (array[object]): ディスプレイ別OCR
  - `display` (number): 1始まり
  - `image` (string): 画像ファイル名
  - `ocr_text` (string): そのディスプレイのOCRテキスト（除外時は空）
  - `excluded` (boolean)
  - `excluded_reason` (string, optional)
  - `error` (object, optional)

任意（取れれば）:
- `bundle_id` (string): 例 `company.thebrowser.Browser`
- `browser` (object):
  - `name` (string): `Arc|Chrome|Safari|...`
  - `url` (string): アクティブタブURL
  - `domain` (string): `example.com`
- `ocr_text` (string): 旧形式の互換用（必要なら `ocr_by_display` から導出）。空文字のことがある。
- `screen` (object):
  - `width` (number)
  - `height` (number)
  - `displays` (number): ディスプレイ数
- `ocr_langs` (array[string]): 例 `["ja-JP","en-US"]`
- `confidence` (number): OCR信頼度の簡易指標（実装できる場合）
- `error` (object): 失敗時のエラー（スクショ失敗/OCR失敗など）

### 7.2 JSONL例
```json
{"id":"b3b0c5e2-0f15-4f6a-8c6b-2e9c44c7d6d9","ts":"2026-02-03T09:15:00-08:00","tz":"-08:00","interval_sec":300,"active_app":"Google Chrome","window_title":"Work Log Spec","browser":{"name":"Chrome","url":"https://www.notion.so/...","domain":"notion.so"},"active_context":{"app":"Google Chrome","window_title":"Work Log Spec","browser":{"name":"Chrome","url":"https://www.notion.so/...","domain":"notion.so"}},"ocr_text":"","ocr_by_display":[{"display":1,"image":"b3b0c5e2-...png","ocr_text":"...","excluded":false},{"display":2,"image":"b3b0c5e2-...-d2.png","ocr_text":"","excluded":true,"excluded_reason":"text_kw:password"}]}
```

## 8. 集計ロジック（推定時間）
サンプリングのため「その間隔ずっと同じ作業だった」と仮定して推定する。

- 1イベントあたり `interval_sec` を作業時間として加算
- 例外
  - スリープ復帰後など間隔が大きく飛んだ場合は、上限（例 2×interval）でクリップする案
  - `error` があるイベントは時間計上しない/別枠にする

### 8.1 アプリ別時間
- `active_app` の集計
- `window_title` は「作業ラベル推定」の材料として使う（集計のキーにはしないのが無難）

### 8.2 ドメイン別時間（Web）
- `browser.domain` が取れたときのみ集計
- 取れない場合は `active_app` のみ

## 9. Markdown出力仕様（テンプレ）
### 9.1 ファイル
- 出力先: `EVERYTIME-LOG/out/YYYY-MM-DD.md`

### 9.2 セクション案
1. ヘッダ
   - 日付、記録期間、キャプチャ数、推定総時間
2. サマリ（推定）
   - アプリTop N（時間）
   - ドメインTop N（時間）
3. タイムライン
   - `09:15` `Arc` `notion.so` — （短い作業ラベル）
4. メモ/学び（任意）
   - AI生成 or ルール抽出

### 9.3 作業ラベルの生成
ルールベース（デフォルト）とAI（オプション）の2段構え。

- ルールベース（デフォルト）
  - `active_app` + `window_title` + `domain` から短いラベルを作る
  - 例: `Arc / notion.so / Work Log Spec`
- AI（`enrich` コマンド）
  - セグメント単位でOCRキーワード/スニペットをOpenAI APIに送信
  - `task_title`（短い作業名）、`task_summary`（1〜2文の要約）、`category`（dev/meeting/research/writing/admin/other）を付与
  - 結果は `out/YYYY-MM-DD.llm.json` に保存し、`summarize` が参照
  - **注意**: OCRテキスト等がOpenAI APIに送信されるため、除外/マスク設定を確認すること

## 10. プライバシー / セキュリティ方針（必須検討）
### 10.1 デフォルト方針（提案）
- 画像は保存しない（OCR後に削除）
- JSONLにはOCR全文を残す（方針確定）。ログはローカルに限定
- オプションで以下を提供
  - `exclude_apps`: 例 `1Password`, `Keychain Access`
  - `exclude_domains`: 例 `bank.com`
  - `redact_patterns`: メール/電話/カードっぽい文字列をマスク
  - `no_ocr`: OCRを無効にしてメタデータのみ（会議など）

### 10.2 macOS権限
最低限必要:
- 画面収録（Screen Recording）: スクショ/OCRのため
場合により必要:
- アクセシビリティ（Accessibility）: ウィンドウ情報取得や自動操作が必要な場合

## 11. Notion連携（将来）仕様案
Notionは「日次ページ」か「イベントDB」かで設計が変わる。

### 案A: 日次ページ（推奨）
- 1日=1ページを作成
- ページ本文にMarkdown相当の内容をブロックで反映
- 長所: 人間が見やすい / 運用が単純
- 短所: イベント単位検索や再集計が弱い

### 案B: イベントDB + 日次ページ
- DB（WorkEvents）にイベントを同期（必要最小限のメタデータ）
- 日次ページはDBビュー + サマリを貼る
- 長所: 検索/集計/フィルタが強い
- 短所: 実装と同期コストが増える（API制限/更新差分）

### 11.1 上記の意味（補足）
- **日次ページ**: 「2026-02-03の作業ログ」という1つのページを作って、その本文にサマリやタイムラインを貼る方式。
- **イベントDB**: 「キャプチャ1件＝DBの1レコード」として溜める方式。Notion上で「Arcだけ表示」「notion.soだけ表示」「今週の上位アプリ」などを後から検索・集計しやすい。

### 11.2 v0.1の方針（確定）
- Notion同期は **1日=1ページ（案A）** のみで十分
- Notionには **OCR全文は同期しない**
  - 同期するのは「日次Markdown相当のサマリ＋タイムライン（+必要なら短い抜粋）」まで
  - OCR全文の正はローカルJSONL（`EVERYTIME-LOG/logs`）とする

### 同期の設計ポイント
- 冪等性: `id`（UUID）をNotion側の一意キーとして保存する
- 差分更新: 既存IDがあれば更新、なければ作成
- レート制限: バッチ/間引きが必要
- 秘匿: OCR全文も同期可能だが、漏えいリスクが上がる（除外/マスキングが重要）

## 12. 設定（実装済み）
設定ファイル: `EVERYTIME-LOG/config.json`

- `interval_sec`（default 300 / 5分）
- `browser`（default `chrome`）
- `keep_screenshots`（default false）
- `capture_app_path`（任意: 画面収録権限を付与した `.app` のパス）
- `exclude`:
  - `apps`: 除外アプリ一覧（default `["1Password"]`）
  - `domain_keywords`: 除外ドメインキーワード一覧
  - `text_keywords`: 除外テキストキーワード一覧
- `redact`:
  - `enable_email` / `enable_phone` / `enable_credit_card` / `enable_auth_nearby`

### 12.1 環境変数
- `EVERLOG_OCR_BIN`: OCRヘルパーのパス（互換: `EVERYTIMECAPTURE_OCR_BIN`）
- `EVERLOG_CAPTURE_APP`: 画面収録権限を付与した `.app` のパス（互換: `EVERYTIMECAPTURE_CAPTURE_APP`）
- `OPENAI_API_KEY`: LLM要約（`enrich`）に必要
- `EVERLOG_LLM_MODEL`: LLMモデル名（default `gpt-5-nano`、互換: `EVERYTIMECAPTURE_LLM_MODEL`）

## 13. 実装方針（フェーズ）
### Phase 0（まず動かす）✅
- `capture` → JSONL追記 → 画像削除
- `summarize` → 日次Markdown

### Phase 1（実用化）✅
- launchdで定期実行（capture/menubar/daily）
- 除外設定、ログ保存先の標準化、簡易マスキング
- macOSアプリ化（py2app / AppleScript wrapper）による画面収録権限問題の回避

### Phase 2（拡張）🔄
- AI要約（`enrich` コマンド）✅
- タグ付け/プロジェクト分類（category: dev/meeting/research/writing/admin/other）✅
- Notion出力（未着手）

## 14. 主要な未確定事項（決める必要あり）
- スリープ/ロック/離席の検知方法（macOS側で何を見てスキップ判定するか）
- Notionは「日次ページ」か「DB中心」か（まずは案A、OCR全文同期はオプション）

## 15. 本スレで確定した方針（v0.2）
- ログ保存先: プロジェクト直下の `EVERYTIME-LOG/`
- スケジューリング:
  - 定期キャプチャ: `launchd` で短命プロセスを定期起動（デフォルト5分）
  - 日次処理: `launchd` で毎日23:55に `enrich` → `summarize` を実行
- スクショ: 保存しない（OCR後に削除）
- OCR: JSONLにOCR全文を保存
- 収集: ブラウザURLはChromeのみ取得
- スリープ/ロック: 検知できた場合はイベントを記録せずスキップ（推定時間にも計上しない）
- LLM要約: OpenAI API（`enrich`）でセグメントに作業名/要約/カテゴリを付与（任意）
- Notion（将来）: 1日1ページのみ同期。OCR全文は同期しない（ローカルJSONLが正）
- UI: メニューバー常駐（Start/Stop、間隔変更、除外設定、手動キャプチャ、日次生成、ステータス表示）
- macOSアプリ化: `py2app` または AppleScript wrapper で `.app` を作成し、画面収録権限を付与
