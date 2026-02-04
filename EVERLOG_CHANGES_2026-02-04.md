<!--
Role: 2026-02-04 に実施した「サービス名を everlog に変更」作業の変更点を、運用/開発/再ビルドまで含めて一枚にまとめる。
How: まずユーザー影響（コマンド/保存先/権限）→互換性→実装差分→再ビルド手順→確認項目の順で記載する。
-->
# everlog へのリネーム作業まとめ（2026-02-04）

## 目的
このリポジトリ内で「サービス名（表示名/コマンド/ラベル等）」を `everlog` に統一する。

## 変更サマリ（ユーザー影響）
- CLI:
  - 新: `everlog`（推奨）
  - 旧: `everytimecapture`（互換エイリアスとして残す）
- launchd ラベル:
  - 新: `com.everlog.capture` / `com.everlog.menubar` / `com.everlog.daily`
  - 旧: `com.everytimecapture.*`（停止/参照の互換あり）
- 保存先ディレクトリ（ログ/出力/設定）:
  - 新デフォルト: プロジェクト直下の `EVERLOG-LOG/`（互換: `EVERYTIME-LOG/`）
  - 互換: 旧ディレクトリ名 `EVERYTIME-LOG/` を自動検出して利用
- 環境変数:
  - 新: `EVERLOG_OCR_BIN`, `EVERLOG_CAPTURE_APP`, `EVERLOG_LLM_MODEL`, `EVERLOG_DATE_OVERRIDE`
  - 互換: `EVERYTIMECAPTURE_OCR_BIN`, `EVERYTIMECAPTURE_CAPTURE_APP`, `EVERYTIMECAPTURE_LLM_MODEL`, `EVERYTIMECAPTURE_DATE_OVERRIDE`
- launchd ログ出力先:
  - 新: `~/.everlog/*.log`
  - 旧: `~/.everytimecapture/*.log`（既存ログは残るが、新しいジョブは `~/.everlog` に出る）

## 互換性ポリシー
- `everytimecapture` パッケージは削除せず、`everlog` へ転送する「互換 shim」として残す。
  - 例: `python -m everytimecapture.cli ...` は `everlog` と同じ挙動になる。
- `EVERYTIME-LOG/` が存在する場合、`EVERLOG-LOG/` を作る前に既存の `EVERYTIME-LOG/` を優先して使う（ログの連続性を優先）。
  - `EVERLOG-LOG/` へ移行したい場合は、ディレクトリをリネームする。

## 実装変更（コード/設定）
### 1) Python パッケージ構成
- 実装本体: `everlog/`（旧 `everytimecapture/` を複製して移行）
- 互換 shim: `everytimecapture/`（各モジュールが `from everlog.xxx import *` で再エクスポート）

### 2) エントリポイント（CLI）
- `pyproject.toml`:
  - パッケージ名を `everlog` に変更
  - `project.scripts` に `everlog` を追加
  - 互換のため `everytimecapture` も `everlog.cli:main` を指すように維持

### 3) 保存先の決定ロジック
- `everlog/paths.py`:
  - 優先順位:
    1. プロジェクト直下 `EVERLOG-LOG/`（互換: `EVERYTIME-LOG/`）
    2. 既知の開発パス（`~/DEV/...`）配下の `EVERLOG-LOG/`

### 4) launchd（ラベル/ログ/互換停止）
- `everlog/launchd.py`:
  - ラベルを `com.everlog.*` に変更
  - `install/stop/restart/status` は、新旧ラベルを見て可能な限り「どちらでも」止めたり状態取得できるようにした
  - plist の環境変数には `PYTHONPATH` など実行に必要な値を埋め込む
  - `StandardOutPath/StandardErrorPath` は `~/.everlog/` に変更

### 5) menubar（表示名/内部CLI呼び出し）
- `everlog/menubar.py`:
  - UI 表示・通知名を `everlog` に統一
  - 内部で呼ぶ CLI を `python -m everlog.cli ...` に変更
  - 旧ラベルの launchd ジョブが動いている場合も検知できるようにした（状態表示/自動起動判定）

## macOS アプリ（再ビルド手順と成果物）
### A) py2app 版（本命）
ビルド:
```sh
cd macos_app
../.venv/bin/python setup.py py2app
```

生成物:
- `macos_app/dist/everlog.app`（Bundle ID: `com.everlog.app`）

### B) AppleScript/JXA 版（pip 不要の最小ラッパー）
ビルド:
```sh
./macos_app/build_capture_app.sh
```

生成物:
- `macos_app/dist/everlog-capture.app`

## 権限付与（Screen Recording）
`システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音` に
`macos_app/dist/everlog.app`（または `everlog-capture.app`）を追加して ON にする。

## 運用コマンド（推奨）
### 1) 手動実行
```sh
./.venv/bin/everlog capture
./.venv/bin/everlog summarize --date today
```

### 2) launchd
```sh
./.venv/bin/everlog launchd capture install
./.venv/bin/everlog launchd menubar install
./.venv/bin/everlog launchd daily install
```

## 既知の注意点
- 既存の `~/Library/LaunchAgents/com.everytimecapture.*.plist` は自動で削除しない（停止はする）。不要なら手動で削除する。
- ネットワーク制限がある環境だと `pip install -e .` や build-isolation が失敗することがある。
  - その場合は既存 venv（このリポジトリの `./.venv/`）で `python -m everlog.cli ...` を使うのが確実。

## この作業で更新/追加された主なファイル
- 実装: `everlog/`（新規） / `everytimecapture/`（互換 shim 化）
- パッケージ/CLI: `pyproject.toml`
- ドキュメント: `README.md`, `DESIGN.md`, `ARCHITECTURE.md`, `EXCLUSIONS.md`, `SETUP_NOTES.md`, `APPIFICATION.md`, `macos_app/README.md`
- macOS アプリ: `macos_app/setup.py`, `macos_app/Everlog.py`, `macos_app/build_capture_app.sh`, `macos_app/create_icns.sh`, `macos_app/Everlog.icns`
- 除外設定: `.gitignore`（`EVERLOG-LOG/` を追加）

## 動作確認（この環境で実施した範囲）
- `python3 -m everlog.cli summarize --date 2026-02-04` が成功し、`EVERYTIME-LOG/out/2026-02-04.md` を更新できること
- `python3 -m everytimecapture.cli summarize --date 2026-02-04` が同様に動くこと（互換 shim の確認）
