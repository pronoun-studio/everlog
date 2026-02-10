# macOS wrapper app

macOSの「画面収録」設定で `python` / `.venv/bin/python` が追加できない（追加しても一覧に反映されない）場合、
`everlog` を `.app` としてビルドし、**その `.app` に画面収録権限を付与**するのが一番安定します。

2種類のビルド方法があります:
- **py2app版**（推奨）: 本格的な `.app` を作成。GUI拡張しやすい
- **AppleScript版**（pip不要）: ネットワーク制限がある環境向けの最小ラッパー

## 方法A: py2app版（推奨）

### Build
```sh
cd macos_app
../.venv/bin/python -m pip install -r ../requirements.txt
../.venv/bin/python -m pip install -r requirements.txt
../.venv/bin/python setup.py py2app
```

Outputs:
- `macos_app/dist/everlog.app`（旧名互換: `macos_app/dist/EverytimeCapture.app`）

### Grant Screen Recording
`システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音` に
`everlog.app` を追加してON。

### Use with launchd
```sh
# config.json に capture_app_path を設定
# または環境変数で指定:
export EVERLOG_CAPTURE_APP="/Users/arima/DEV/everytimecapture/macos_app/dist/everlog.app"
./.venv/bin/everlog launchd capture install
./.venv/bin/everlog launchd capture restart
```

## 方法B: AppleScript版（pip不要）

`py2app` は便利ですが、ネットワーク制限がある環境だと `pip install py2app` ができない場合があります。
その場合は、`build_capture_app.sh` で AppleScript 製の最小 `.app` を作れます。

### Build
```sh
./macos_app/build_capture_app.sh
```

Outputs:
- `macos_app/dist/everlog-capture.app`（旧名互換: `macos_app/dist/EverytimeCaptureCapture.app`）

### Grant Screen Recording
`システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音` に
`everlog-capture.app` を追加してON。

### Use with launchd
```sh
export EVERLOG_CAPTURE_APP="/Users/arima/DEV/everytimecapture/macos_app/dist/everlog-capture.app"
./.venv/bin/everlog launchd capture install
./.venv/bin/everlog launchd capture restart
```

## Status / Notes
- 生成した `.app` に「画面収録とシステムオーディオ録音」権限を付与しない限り、`screencapture` は失敗します
- py2app版は固有の Bundle ID (`com.everlog.app`) を持つため、TCC が安定して同一アプリとして認識します
- 詳細な経緯・トラブルシューティングは `APPIFICATION.md` を参照
