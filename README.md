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

ビルド例（Xcode Command Line Toolsが必要）:
```sh
cd ocr/ecocr
swift build -c release
mkdir -p EVERYTIME-LOG/bin
cp -f .build/release/ecocr EVERYTIME-LOG/bin/ecocr
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

### 3.5) launchd（日次処理: enrich + summarize）
毎日23:55に `enrich` → `summarize` を自動実行します（`OPENAI_API_KEY` が必要）。
```sh
./.venv/bin/everlog launchd daily install
```

停止/再開/状態確認/アンインストール:
```sh
./.venv/bin/everlog launchd daily stop
./.venv/bin/everlog launchd daily start
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

### メニューバーUI仕様（表示）
- 定期キャプチャ: 「●動作中」or「○停止中」
- 今日のキャプチャ回数: x回
- 前回キャプチャ時間: yy/MM/dd H:mm

## macOSアプリ化（py2app）
macOSの「画面収録」設定で `python` / `.venv/bin/python` が追加できない（追加しても一覧に反映されない）場合、
`everlog` を `.app` としてビルドし、**その `.app` に画面収録権限を付与**するのが一番安定します。

進捗や現状の詰まりどころは `APPIFICATION.md` にまとめています。

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
pip install -r requirements-macos-app.txt
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

## 保存先
- ログ: `EVERYTIME-LOG/logs/YYYY-MM-DD.jsonl`
- 出力: `EVERYTIME-LOG/out/YYYY-MM-DD.md`（LLM結果: `YYYY-MM-DD.llm.json`）
- 一時: `EVERYTIME-LOG/tmp/`
- バイナリ: `EVERYTIME-LOG/bin/`（OCRヘルパー `ecocr` など）
- 設定: `EVERYTIME-LOG/config.json`
- launchdログ: `~/.everlog/capture.out.log`, `capture.err.log`, `menubar.*.log`, `daily.*.log`

## 環境変数
- `EVERLOG_OCR_BIN`: OCRヘルパーのパス（互換: `EVERYTIMECAPTURE_OCR_BIN`）
- `EVERLOG_CAPTURE_APP`: 画面収録権限を付与した `.app` のパス（互換: `EVERYTIMECAPTURE_CAPTURE_APP`）
- `OPENAI_API_KEY`: LLM要約（`enrich`）に必要
- `EVERLOG_LLM_MODEL`: LLMモデル名（default `gpt-5-nano`、互換: `EVERYTIMECAPTURE_LLM_MODEL`）

## 詳細ドキュメント
- `DESIGN.md`: 設計仕様（要件・データ形式・実行形態・プライバシー方針）
- `ARCHITECTURE.md`: 実装マップ（ファイル別の役割と連携）
- `EXCLUSIONS.md`: 除外・マスキングルール
- `APPIFICATION.md`: macOSアプリ化の経緯と手順
