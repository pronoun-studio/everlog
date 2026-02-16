<!--
Role: プロジェクトのセットアップ/利用方法（コマンド、権限、OCRビルド）を短く案内する。
How: 最小の手順だけを載せ、詳細な設計は DESIGN.md / ARCHITECTURE.md に委譲する。
Key sections: セットアップ、OCR、launchd、menubar、LLM要約。
Collaboration: 実装は `everlog/` と `ocr/ecocr/` にあり、運用ルールは EXCLUSIONS.md を参照する。
-->
# everlog

macOSで、定期スクリーンショット→ローカルOCR→JSONL保存→（任意）LLM要約→日次Markdown生成を行う個人用ツール。

## 重要（権限）
- 画面収録（Screen Recording）権限が必要です（スクショ/OCRのため）。
- アクセシビリティ権限が必要になる場合があります（前面ウィンドウ情報の取得のため）。
  - `launchd` 経由で動かす場合、権限は「実際に実行されるPython（例: `.venv/bin/python`）」に付与されます。
  - うまく動かない場合は、`システム設定 → プライバシーとセキュリティ → 画面収録` に `python` / `python3` / `.venv/bin/python` が入っているか確認してください。

## セットアップ（推奨: venv）
```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

初回実行で、プロジェクト直下の `EVERYTIME-LOG/` 配下を作成します。
（保存先を固定したい場合は環境変数 `EVERLOG_LOG_HOME` を設定してください）

## OCR（ローカル）
v0.1では「Vision OCRヘルパー（Swift製）」をログディレクトリ配下の `bin/ecocr` に置く想定です。
（暫定で `EVERLOG_OCR_BIN`（互換: `EVERYTIMECAPTURE_OCR_BIN`）でも指定できます。）
また、複数ディスプレイ環境で「アクティブなディスプレイ番号（`screencapture -D` の番号）」を推定するヘルパー `ecdisplay` も同じ `bin/` 配下に置けます。
（暫定で `EVERLOG_DISPLAY_BIN`（互換: `EVERYTIMECAPTURE_DISPLAY_BIN`）でも指定できます。）

ビルド例（Xcode Command Line Toolsが必要）:
```sh
cd ocr/ecocr
swift build -c release
mkdir -p EVERYTIME-LOG/bin
cp -f .build/release/ecocr EVERYTIME-LOG/bin/ecocr
cp -f .build/release/ecdisplay EVERYTIME-LOG/bin/ecdisplay
```

## 使い方
（venv を使っている場合は、`./.venv/bin/` を付けるか `source .venv/bin/activate` を実行してください）
### 1) 1回だけキャプチャ
```sh
./.venv/bin/everlog capture
```

### 2) 今日のMarkdown生成
```sh
./.venv/bin/everlog summarize --date today
```

### 2.5) LLMで要約を付与（任意）
OpenAI APIを使ってセグメントに作業名/要約を付けます（`out/YYYY-MM-DD.llm.json` を生成）。
```sh
# どれか1つでOK:
# - 環境変数で渡す（シェル実行向け）
export OPENAI_API_KEY="(your key)"
export EVERLOG_LLM_MODEL="gpt-5-nano"   # or gpt-5-mini

# - もしくは .env に書く（launchd / .app 実行でも拾いやすい。推奨）
#   例: プロジェクト直下の `EVERYTIME-LOG/.env` またはプロジェクト直下の `.env`
#   （別の場所に固定したい場合は `EVERLOG_LOG_HOME` を設定）
#   OPENAI_API_KEY=...
#   EVERLOG_LLM_MODEL=gpt-5-nano
./.venv/bin/everlog enrich --date today
./.venv/bin/everlog summarize --date today
```
※ OCRテキスト等がOpenAI APIに送信されます。必要に応じて除外/マスク設定を確認してください。

オプション:
- `--model`: LLMモデル名（デフォルト: 環境変数 `EVERLOG_LLM_MODEL`（互換: `EVERYTIMECAPTURE_LLM_MODEL`）または `gpt-5-nano`）
- `--max-segments`: 送信するセグメント数の上限（デフォルト: 80）

### 3) launchd（短命プロセス定期起動）
インストール（5分間隔・ユーザーLaunchAgent）:
```sh
./.venv/bin/everlog launchd capture install
```

停止:
```sh
./.venv/bin/everlog launchd capture stop
```

再開:
```sh
./.venv/bin/everlog launchd capture start
```

状態確認:
```sh
./.venv/bin/everlog launchd capture status
```

再起動（設定変更を反映）:
```sh
./.venv/bin/everlog launchd capture restart
```

アンインストール:
```sh
./.venv/bin/everlog launchd capture uninstall
```

### 3.5) launchd（日次処理: summarize オーケストレーション）
毎日23:55に当日分の `summarize` を自動実行します（`OPENAI_API_KEY` が必要）。
また、起動時（RunAtLoad）に未完了日の再試行を行います。

- 起動時: 未完了の pending 日付 + 昨日分（未生成/未完了なら）を再試行
- 23:55: pending 日付を先に再試行し、その後に当日分を実行
- LLM未完了（`⚠️ 未完成...` や `hour/daily/hour-enrich` のいずれか未実行）の場合は失敗扱いで pending に残り、次回起動時または次回23:55で再実行
- 日次自動実行では `EVERLOG_LLM_TIMEOUT_SEC=300` を設定し、ネットワーク遅延時のタイムアウトを緩和
```sh
./.venv/bin/everlog launchd daily install
```

停止/再開/再起動/状態確認/アンインストール:
```sh
./.venv/bin/everlog launchd daily stop
./.venv/bin/everlog launchd daily start
./.venv/bin/everlog launchd daily restart
./.venv/bin/everlog launchd daily status
./.venv/bin/everlog launchd daily uninstall
```

### 4) メニューバーUI（rumps）
```sh
./.venv/bin/everlog menubar
```

ログイン時にメニューバーを自動起動したい場合:
```sh
./.venv/bin/everlog launchd menubar install
```

停止:
```sh
./.venv/bin/everlog launchd menubar stop
```

再開:
```sh
./.venv/bin/everlog launchd menubar start
```

状態確認:
```sh
./.venv/bin/everlog launchd menubar status
```

再起動:
```sh
./.venv/bin/everlog launchd menubar restart
```

アンインストール:
```sh
./.venv/bin/everlog launchd menubar uninstall
```

### メニューバーUI仕様（操作）
- 起動: PC起動時に自動起動（`launchd menubar install`）
- 自動起動トグル: メニューの「自動起動: 有効/無効」から切り替え
- 終了: メニューバーUIの「終了」→ システム自体が終了し再起動しない
- 定期キャプチャ開始: 「●定期キャプチャの開始」→ ステータスが「●動作中」になり通知が出る
- 定期キャプチャ停止: 「○定期キャプチャの停止」→ ステータスが「○停止中」になり通知が出る
- 定期キャプチャの再適用: 「everlogを再起動」→ 定期キャプチャ設定を再インストール/再読み込み
- 間隔変更: 1分/5分/10分/30分（デフォルトは5分）。選択中はチェックマーク表示。変更時に通知が出る
- 除外設定: 「除外設定を開く」→ 1画面のダイアログで除外項目（アプリ/ドメイン/テキスト）を編集・保存
- 今すぐ1回キャプチャ: 「今すぐ1回キャプチャ」
- 今日のマークダウン生成: 「今日のマークダウン生成」

## 主要な実行方法（2つ）
マークダウン生成には主に以下の2つの方法があります:

1. **メニューバーから手動実行**: `everlog.app` のメニューバーから「今日のマークダウン生成」を選択
2. **毎日23:55に自動実行**: `launchd daily install` で設定。毎日23:55に自動でマークダウン生成

いずれの場合も、出力は `EVERYTIME-LOG/out/<date>/<run_id>/` ディレクトリに格納されます。
（`out/<date>.md` への直接出力は廃止されました）

### Pythonコード変更時の再ビルド
`everlog.app`（メニューバー）はpy2appでビルドされており、**Pythonコードがアプリ内にバンドル**されています。
そのため、`everlog/` 配下のPythonコードを変更した場合は以下の手順が必要です:

```sh
# 1. .app を再ビルド
cd macos_app
python setup.py py2app --dist-dir dist

# 2. 実行中のメニューバーを終了して再起動
pkill -f "everlog.app/Contents/MacOS/everlog"
open dist/everlog.app
```

**注**: `launchd daily`（23:55自動実行）は `.venv/bin/python` を直接呼び出すため、
`pip install -e .` を実行すればPythonコード変更が反映されます（再ビルド不要）。
ただし、launchdのplist設定を変更した場合は再インストールが必要です:
```sh
./.venv/bin/everlog launchd daily uninstall
./.venv/bin/everlog launchd daily install
```

### メニューバーUI仕様（表示）
- 定期キャプチャ: 「●動作中」or「○停止中」
- 今日のキャプチャ回数: x回
- 前回キャプチャ時間: yy/MM/dd H:mm

### 進捗表示（マークダウン生成時）
「今日のマークダウン生成」を実行すると、ネイティブの進捗パネルが表示されます:
- 現在の処理ステージ名（データ読み込み → LLM要約 → Markdown生成 など）
- 進捗パーセンテージ（プログレスバー）
- 処理完了後に自動で閉じる

## macOSアプリ化（py2app）
macOSの「画面収録」設定で `python` / `.venv/bin/python` が追加できない（追加しても一覧に反映されない）場合、
`everlog` を `.app` としてビルドし、**その `.app` に画面収録権限を付与**するのが一番安定します。

進捗や現状の詰まりどころは `docs/archive/APPIFICATION.md` にまとめています。

### 権限付与のフロー（2パターン）
**A. Pythonに権限付与（シンプル）**  
`python` / `.venv/bin/python` に画面収録権限を付与し、そのまま `launchd` で動かす方法。

**B. `.app` に権限付与（安定）**  
`everlog.app` に権限を付与し、`launchd` を `.app` 経由で動かす方法。
この場合は **`EVERLOG_CAPTURE_APP`（または `config.json` の `capture_app_path`）を設定して
`launchd` を再インストール/再起動** して反映します。

### 0) ネットワーク制限がある場合（pip不要）
AppleScript製の最小 `.app` を作成できます:
```sh
./macos_app/build_capture_app.sh
```
生成物:
- `macos_app/dist/everlog-capture.app`
  - 旧名互換: `macos_app/dist/EverytimeCaptureCapture.app`（以前のビルド成果物）

### 1) ビルド
```sh
pip install -r requirements.txt
pip install -r macos_app/requirements.txt
cd macos_app
python setup.py py2app
```

生成物:
- `macos_app/dist/everlog.app`
  - 旧名互換: `macos_app/dist/EverytimeCapture.app`（以前のビルド成果物）

### 2) 権限付与
`システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音` に
`Everlog.app` を追加してONにします。

### 3) launchdから `.app` 経由で capture する
次のどちらかで指定できます:
- 環境変数 `EVERLOG_CAPTURE_APP`（互換: `EVERYTIMECAPTURE_CAPTURE_APP`）
- 設定 `config.json` の `capture_app_path`

例（環境変数）:
```sh
export EVERLOG_CAPTURE_APP="/Users/arima/DEV/everytimecapture/macos_app/dist/everlog-capture.app"
./.venv/bin/everlog launchd capture install
```

### 5) 全停止（quit）
`quit` コマンドで、定期キャプチャとメニューバーを停止し自動起動を無効化できます:
```sh
./.venv/bin/everlog quit
```

## Notion同期（任意）
生成されたマークダウンを Notion データベースに自動同期できます。

### 準備
1. Notion Integration を作成（https://www.notion.so/my-integrations）
2. 対象データベースに Integration を招待
3. 環境変数を設定:
```sh
# .env に追加
NOTION_API_KEY=ntn_xxxxxx...
NOTION_DATABASE_ID=25ab3850f8c38153b303c6c408d7cd4e
EVERLOG_NOTION_SYNC=1
```

### 同期タイミング
- `summarize` 実行時に自動同期（`EVERLOG_NOTION_SYNC=1` の場合）
- 同期失敗時は `~/.everlog/notion_pending.json` に記録され、次回 `summarize` 時に再試行

### Notionデータベース構造
| プロパティ名 | 型 | Everlogからの値 |
|---|---|---|
| `活動ログ` | Title | 日報タイトル |
| `実施日_編集可` | Date | 対象日付 |
| `ジャンル` | Multi-select | `AutoLog`（固定） |

詳細: `docs/NOTION_SYNC.md`

## 保存先
- ログ: `EVERYTIME-LOG/logs/YYYY-MM-DD.jsonl`
- 出力: `EVERYTIME-LOG/out/YYYY-MM-DD/<run_id>/`
  - マークダウン: `YY-MM-DD_<daily_title>.md`
  - LLM結果: `YYYY-MM-DD.hourly.llm.json`, `YYYY-MM-DD.daily.llm.json`, `YYYY-MM-DD.hour-enrich.llm.json`
  - （任意）segment-llm結果: `YYYY-MM-DD.llm.json`
- トレース: `EVERYTIME-LOG/trace/YYYY-MM-DD/<run_id>/`
  - `run.json`, `stage-00.raw.jsonl`, `stage-01.entities.jsonl`, `stage-02.segment.jsonl` など
- 一時: `EVERYTIME-LOG/tmp/`
- バイナリ: `EVERYTIME-LOG/bin/`（OCRヘルパー `ecocr` など）
- 設定: `EVERYTIME-LOG/config.json`
- launchdログ: `~/.everlog/capture.out.log`, `capture.err.log`, `menubar.*.log`, `daily.*.log`
- Notion pending: `~/.everlog/notion_pending.json`

## 環境変数
### 基本設定
- `EVERLOG_LOG_HOME`: ログ保存先ディレクトリ（デフォルト: プロジェクト直下の `EVERYTIME-LOG/`、互換: `EVERYTIMECAPTURE_LOG_HOME`）
- `EVERLOG_OCR_BIN`: OCRヘルパーのパス（互換: `EVERYTIMECAPTURE_OCR_BIN`）
- `EVERLOG_DISPLAY_BIN`: アクティブディスプレイ推定ヘルパーのパス（互換: `EVERYTIMECAPTURE_DISPLAY_BIN`）
- `EVERLOG_CAPTURE_APP`: 画面収録権限を付与した `.app` のパス（互換: `EVERYTIMECAPTURE_CAPTURE_APP`）

### LLM要約
- `OPENAI_API_KEY`: LLM要約（`enrich`）に必要
- `EVERLOG_LLM_MODEL`: LLMモデル名（default `gpt-5-nano`、互換: `EVERYTIMECAPTURE_LLM_MODEL`）

### Notion同期
- `NOTION_API_KEY`: Notion Integration Token
- `NOTION_DATABASE_ID`: 同期先データベースID（32文字、ハイフンは自動削除）
- `EVERLOG_NOTION_SYNC`: `1` で同期を有効化

### プライバシー/安全
- `EVERLOG_SAFE_MARKDOWN`: 最終Markdown/Notion同期のタイトルで、PII・認証情報・典型的なトークン等をローカルでマスクする（default: 有効）。無効化する場合は `0` を指定（互換: `EVERYTIMECAPTURE_SAFE_MARKDOWN`）

### デバッグ/トレース
- `EVERLOG_TRACE_STAGE_MAX`: トレース出力の最大ステージ番号（default: `2`。`3`〜`7` で中間ファイルを増やす）
- `EVERLOG_OUTPUT_RUN_ID`: 出力ディレクトリの `run_id` を固定（通常は自動生成）
- `EVERLOG_TRACE_RUN_ID`: トレースディレクトリの `run_id` を固定（通常は自動生成）

## 設定ファイル（config.json）
`EVERYTIME-LOG/config.json` で以下を設定できます:
```json
{
  "interval_sec": 300,         // キャプチャ間隔（秒）
  "browser": "chrome",         // URL取得対象ブラウザ
  "keep_screenshots": false,   // スクリーンショットを保持するか
  "capture_app_path": null,    // 画面収録権限を付与した.appのパス
  "exclude": {
    "apps": ["1Password"],     // 除外アプリ
    "domain_keywords": [...],  // 除外ドメインキーワード
    "text_keywords": [...]     // 除外テキストキーワード
  },
  "redact": {
    "enable_email": true,      // メールアドレスをマスク
    "enable_phone": false,     // 電話番号をマスク
    "enable_credit_card": true,// カード番号をマスク（Luhnチェック）
    "enable_auth_nearby": true // 認証キーワード近傍をマスク
  }
}
```

## パイプライン概要
`summarize` コマンドは以下のステージを経てMarkdownを生成します:
1. **stage-00.raw**: 生データ保存
2. **stage-01.entities**: 特徴抽出（アプリ/ドメイン/URL/パス）
3. **stage-02.segment**: セグメント化（連続した同一作業をグループ化）
4. **stage-03.segment**: セグメント内OCR統合（重複削除）
5. **stage-04.hour-pack**: 1時間単位でパッケージ化
6. **stage-05.hour-llm**: LLMで1時間ごとの要約生成
7. **stage-06.daily-llm**: LLMで1日全体の総括生成
8. **stage-07.hour-enrich-llm**: 各時間帯の目的・意味を再解釈

詳細: `docs/PIPLINE_3.md`

## 詳細ドキュメント
- `docs/DESIGN.md`: 設計仕様（要件・データ形式・実行形態・プライバシー方針）
- `docs/ARCHITECTURE.md`: 実装マップ（ファイル別の役割と連携）
- `docs/EXCLUSIONS.md`: 除外・マスキングルール
- `docs/PRIVACY-CONTENTS.md`: プライバシー保護対象の分類（PII/認証/金融/機微情報など）
- `docs/NOTION_SYNC.md`: Notion同期の設計と設定方法
- `docs/PIPLINE_3.md`: パイプライン詳細（Active Display First / 1時間タイムライン）
- `docs/archive/APPIFICATION.md`: macOSアプリ化の経緯と手順

---

## 初期セットアップ手順まとめ（新規環境向け）

GitHubからクローンして新しいMac環境で動かすための手順です。

### 1. リポジトリのクローンとPython環境構築
```sh
git clone https://github.com/pronoun-studio/everlog.git
cd everlog
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 2. OCRバイナリのビルド（Xcode Command Line Tools必要）
```sh
# Xcode CLTがない場合は先にインストール
xcode-select --install

cd ocr/ecocr
swift build -c release
mkdir -p ../../EVERYTIME-LOG/bin
cp -f .build/release/ecocr ../../EVERYTIME-LOG/bin/ecocr
cp -f .build/release/ecdisplay ../../EVERYTIME-LOG/bin/ecdisplay
cd ../..
```

### 3. .envファイルの作成（APIキー設定）
```sh
# プロジェクト直下に .env を作成
cat > .env << 'EOF'
OPENAI_API_KEY=sk-あなたのOpenAIキー
EVERLOG_LLM_MODEL=gpt-5-nano

# Notion同期を使う場合（任意）
# NOTION_API_KEY=ntn_あなたのNotionキー
# NOTION_DATABASE_ID=データベースID（32文字）
# EVERLOG_NOTION_SYNC=1
EOF
```

### 4. メニューバーアプリのビルド（推奨）
**推奨**: `.app` に画面収録権限を付与するのが最も安定します。
```sh
pip install -r macos_app/requirements.txt
cd macos_app
python setup.py py2app
cd ..
```

### 5. macOS権限の付与（手動）
`システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音` で：
- **推奨**: `macos_app/dist/everlog.app` を追加してON
- または `.venv/bin/python` を追加してON

### 6. 起動（推奨: メニューバーUI）
```sh
# メニューバーアプリを起動
open macos_app/dist/everlog.app

# ログイン時に自動起動したい場合
./.venv/bin/everlog launchd menubar install
```

メニューバーから「●定期キャプチャの開始」「今日のマークダウン生成」などが利用できます。

### 自動生成されるディレクトリ
以下は初回実行時に自動で作成されます（手動作成不要）：
- `EVERYTIME-LOG/` - ログ保存ディレクトリ
  - `logs/` - キャプチャログ（JSONL）
  - `out/` - 出力（マークダウン等）
  - `trace/` - デバッグ用トレース
  - `tmp/` - 一時ファイル
  - `bin/` - OCRバイナリ配置場所（手順2で配置済み）

### GitHubに含まれないもの（各環境で用意が必要）
| ファイル/ディレクトリ | 理由 | 対応 |
|---|---|---|
| `.env` | APIキー等の機密情報 | 各自で作成（手順3） |
| `.venv/` | Python仮想環境 | 各自でセットアップ（手順1） |
| `EVERYTIME-LOG/` | ログ・出力データ | 初回実行で自動生成 |
| `ocr/ecocr/.build/` | Swiftビルド成果物 | 各自でビルド（手順2） |
| `macos_app/dist/` | py2appビルド成果物 | 各自でビルド（手順4） |
