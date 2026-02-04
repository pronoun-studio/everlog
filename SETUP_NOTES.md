# セットアップと手順メモ（everlog）

このドキュメントは「目的 → 現状の課題 → 直近の解決手順 → LLM対応 → その後のステップ」を短くまとめたメモです。

---

## 🎯 目的（そもそも）
- スクリーンショット + OCR から **「そのPCで何をしていたか」を再現できる作業ログ**を作る
- JSONLに時系列で記録し、LLMで意味付けしてMarkdown日報にまとめる

---

## 📊 現在のステータス（2026-02-04 更新）

| 項目 | 状態 | 備考 |
|------|------|------|
| Phase 0: 保存先統一 | ✅ 完了 | `EVERLOG-LOG/` を使用（互換: `EVERYTIME-LOG/`） |
| Phase 1: Automation権限 | ✅ 完了 | AppleScript正常動作 |
| Phase 1: Screen Recording権限 | ✅ 完了 | `python3` を追加済み |
| Phase 1: launchd capture | ✅ 動作中 | 定期実行登録済み |
| Phase 1: launchd menubar | ✅ 動作中 | PID確認済み |
| スクリーンショット取得 | ✅ 成功 | `tmp/` に保存される |
| OCRテキスト取得 | ✅ 成功 | 3000文字以上認識確認済み |
| Phase 3: enrich (LLM解析) | ✅ 成功 | gpt-5-nano + Responses API |
| Phase 4: summarize (MD生成) | ✅ 成功 | Markdown日報生成確認済み |

---

## ✅ 解決済みの課題

### OCRが空を返す問題（解決済み）
**原因**: Vision Frameworkの設定不足 + バイナリ未更新

**修正内容** (`ocr/ecocr/Sources/main.swift`):
```swift
request.recognitionLevel = .accurate  // .fast → .accurate
request.recognitionLanguages = ["ja-JP", "en-US"]  // 追加
```

**重要**: ソース修正後は必ず再ビルド＆配置が必要：
```bash
cd ocr/ecocr && swift build -c release
cp .build/release/ecocr ~/DEV/everytimecapture/EVERLOG-LOG/bin/ecocr
```

---

## 0. 保存先の統一（Phase 0）✅ 完了
- プロジェクト直下の `EVERLOG-LOG/` を正として使う（互換: `EVERYTIME-LOG/` も自動検出）
- `EVERLOG-LOG/` があれば自動でそちらが使われる

## 1. 権限の付与（Phase 1）✅ 完了

### 1-1) Automation（Apple Events）✅
AppleScriptを使うために必要。以下で許可ダイアログを出す。

```
osascript -e 'tell application "System Events" to name of (first application process whose frontmost is true)'
```

ダイアログが出ない場合は一度リセットして再トリガー：

```
tccutil reset AppleEvents
```

### 1-2) Screen Recording ✅
スクショ取得のため必須。`.venv` は隠しフォルダなので以下で追加する。

1) 画面収録の「＋」を押す  
2) ファイル選択で `Cmd + Shift + G`  
3) これを貼り付けて移動：

```
/Users/arima/DEV/everytimecapture/.venv/bin
```

4) **`python3`** を選んで追加 → ON（`python3.10` はシンボリックリンクで選択不可）

### 1-3) Accessibility
ウィンドウタイトル取得のため必要。Screen Recordingと同じ手順で追加。
（現状 `window_title` が空の場合があるが、動作には支障なし）

## 2. launchd の再インストール ✅ 完了
権限が付いたら以下を実行：

```
./.venv/bin/everlog launchd capture install
./.venv/bin/everlog launchd menubar install
```

確認コマンド：
```
launchctl list | rg -n "everlog|everytimecapture" || true
```

---

## 2.5 macOSアプリ化（py2app）: 画面収録の権限問題回避
症状:
- 「画面収録とシステムオーディオ録音」の `＋` から `python` / `.venv/bin/python*` を選んでも、一覧に追加されない/反映されない
- launchd経由のcaptureが `could not create image from display` で失敗し続ける

対策:
`everlog` を `.app` にして、**その `.app` に画面収録権限を付与**する。

### ビルド
```sh
pip install -r requirements.txt
pip install -r requirements-macos-app.txt
cd macos_app
python setup.py py2app
```

生成物:
- `macos_app/dist/everlog.app`（旧名互換: `macos_app/dist/EverytimeCapture.app`）

※ ネットワーク制限などで `pip install` ができない場合は、pip不要の方法があります:
```sh
./macos_app/build_capture_app.sh
```
生成物:
- `macos_app/dist/everlog-capture.app`（旧名互換: `macos_app/dist/EverytimeCaptureCapture.app`）

### 権限付与
`システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音` に
`Everlog.app` を追加してONにする。

### launchdが `.app` 経由で capture を実行するようにする
次のどちらかで指定:
- 環境変数 `EVERLOG_CAPTURE_APP`（互換: `EVERYTIMECAPTURE_CAPTURE_APP`）
- `EVERLOG-LOG/config.json` の `capture_app_path`（互換: `EVERYTIME-LOG/config.json`）

例:
```sh
export EVERLOG_CAPTURE_APP="/Users/arima/DEV/everytimecapture/macos_app/dist/everlog-capture.app"
./.venv/bin/everlog launchd capture install
./.venv/bin/everlog launchd capture restart
```

## 3. 動作確認（スクショ/OCR）
```
./.venv/bin/everlog capture
```

確認ポイント:
- `EVERLOG-LOG/tmp/` にスクショが残る（いまは `keep_screenshots=true`）
- `EVERLOG-LOG/logs/YYYY-MM-DD.jsonl` の `error` が激減している
- `window_title` と `ocr_text` が空ではない行が増えている

---

## 3.5 作業シグナルの最小整備（Phase 2）
方針:
- 普段使うブラウザはChromeのみ → 対応拡張は後回し
- `bundle_id` / 画面サイズなどの補助情報は今は不要
- LLM入力用に **短い特徴量（キーワード/ファイル名/抜粋）** を作る → 対応済み

セグメント化の入力（LLMに渡す形式）:
- 開始/終了/アプリ/ドメイン/タイトル/キーワード/OCR抜粋（数行）

---

## 4. LLM解析（Phase 3）✅ 完了
.env にAPIキーを入れておけば、毎回 source しなくても自動で読み込む。

```bash
./.venv/bin/everlog enrich --date today
./.venv/bin/everlog summarize --date today
```

**使用API**: OpenAI Responses API (`/v1/responses`)  
**デフォルトモデル**: `gpt-5-nano`（環境変数 `EVERLOG_LLM_MODEL`（互換: `EVERYTIMECAPTURE_LLM_MODEL`）で変更可）

出力:
- LLM結果: `EVERLOG-LOG/out/YYYY-MM-DD.llm.json`
- Markdown: `EVERLOG-LOG/out/YYYY-MM-DD.md`

LLM出力に期待する内容:
- `task_title`（短い作業名）
- `task_summary`（1〜2文）
- `category`（dev/meeting/research/writing/admin/other）
- `confidence`（自信度）

---

## 4.5 Markdownテンプレの固定（Phase 4）✅ 完了
必須セクション:
- ヘッダ（記録期間、キャプチャ数、推定時間）
- 本日のメイン作業（LLM生成TOP3）
- アプリ使用状況（回数＋推定時間＋用途）
- タイムライン（セグメント単位で「いつ何をしていたか」）
- 参考（データ品質：除外数、失敗数）

---

## 5. 次のステップ

### 直近のTODO
1. ~~**OCR問題の調査・修正**~~ ✅ 完了
2. ~~`enrich` / `summarize` コマンドの動作確認~~ ✅ 完了
3. launchd定期実行のエラー比率を5%未満に（現在まだ高い）

### その後
- 2/3 の `*.llm.json` を生成してフォーマット調整
- スクショ不要になったら `keep_screenshots=false` に戻す
- launchd経由のScreen Recording権限問題を調査

---

## 📝 変更履歴
- **2026-02-04 16:08**: Phase 3-4 完了（enrich/summarize動作確認、gpt-5-nano + Responses API）
- **2026-02-04 15:45**: OCR問題解決（`.accurate` + 言語設定 + 再ビルド）
- **2026-02-04 15:20**: Phase 1 完了（権限付与・launchd設定）、OCR問題を発見
