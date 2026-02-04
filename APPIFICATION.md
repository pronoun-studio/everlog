<!--
Role: 「App化プロジェクト」の目的・途中経過・現在の状態・次のアクションをまとめる。
How: 現象→仮説→実施した変更→現状→次の手順 の順で短く残す。
-->
# App化PJT（Screen Recording問題の回避）まとめ

## 目的
- launchd 経由の定期キャプチャが `screencapture` で失敗し続ける問題を解消する
- 将来的にGUI（メニューバー/設定/状態）を足す前提で「許可を付けられる実行主体（.app）」に寄せる

## 背景（発生している問題）
- `EVERLOG-LOG/logs/2026-02-04.jsonl` で `screencapture failed (code=1): could not create image from display` が連続（互換: `EVERYTIME-LOG/...`）
- macOSの「画面収録とシステムオーディオ録音」画面で `python` / `.venv/bin/python*` を `＋` から追加しようとしても、
  一覧に反映されず（選択が無視される挙動）＝権限を付与できない

## 追加した診断ログ（原因特定のため）
- `everlog/capture.py:1`
  - `error.stage= s creencapture` / `returncode` / `stderr` / `cmd` を JSONL に追記
  - `runner`（`python_resolved`, `XPC_SERVICE_NAME`, `cwd`, `ppid` など）を JSONL に追記
  - 代表例: `xpc_service_name=com.everlog.capture`（launchd）や、実体Pythonパスが分かる（互換: `com.everytimecapture.capture`）

## App化の方針
画面収録権限の付与において、この挙動（「＋ → パス指定 → 開く」まで行けるのに、一覧に何も追加されず画面も変わらない）は、macOS側が“アプリ（.app）以外の実行ファイルをこの画面に追加できず、選択が無視されているときの典型です。なので今の状態だと、python3.10 を追加できていません（＝launchdの失敗は直ってないまま）。

この場合の現実的な回避策は「許可を付けられる .app を用意して、それに画面収録権限を付ける」です。
「System Settingsで権限を付けられる対象」に寄せる。

### A) `py2app`（本命・GUI拡張しやすい）
- 追加: `macos_app/setup.py:1` / `macos_app/Everlog.py:1` / `requirements-macos-app.txt:1`
- ただし現状、この環境はネットワーク制限があり `pip install py2app` が失敗する可能性が高い

### B) AppleScript/JXAの最小 `.app`（pip不要・当面の回避策）
- 追加: `macos_app/build_capture_app.sh:1`
  - `osacompile -l JavaScript` で `macos_app/dist/EverlogCapture.app` を生成
  - `.app` の `run(argv)` で `python -m everlog.cli <args>` を実行するだけの薄いラッパー

## launchd側の変更（.app経由で実行できるように）
- `everlog/launchd.py:1`
  - `EVERLOG_CAPTURE_APP`（互換: `EVERYTIMECAPTURE_CAPTURE_APP`）もしくは `config.json` の `capture_app_path` がある場合、
    `.app` を優先して起動する
  - `.app` は `open -a` がコケる環境があったため、`Contents/MacOS/<CFBundleExecutable>` を直接実行する経路も追加
- 現在の plist 状態（例）:
  - `~/Library/LaunchAgents/com.everlog.capture.plist` が `.../EverlogCapture.app/Contents/MacOS/applet capture` を実行する形になっている（互換: `com.everytimecapture.capture.plist`）

## 現状（2026-02-04 18:20 時点）
- **py2app 版 `.app`**: `macos_app/dist/everlog.app`
  - Bundle ID: `com.everlog.app`
  - 実行体: `Contents/MacOS/Everlog`（汎用 `applet` ではない）
- `config.json` に `capture_app_path` を設定済み
- launchd plist は `everlog.app/Contents/MacOS/everlog capture` を実行する形に再生成済み
- **次のステップ**: `.app` に Screen Recording 権限を付与する

## 次にやること（運用手順）
1) **`.app` に画面収録権限を付与**
   - `システム設定 → プライバシーとセキュリティ → 画面収録とシステムオーディオ録音`
   - `+` ボタン → `macos_app/dist/everlog.app` を選択 → ON
   - Finder で開く: `open /Users/arima/DEV/everytimecapture/macos_app/dist/`
2) 反映のために定期ジョブを再読み込み
   ```sh
   ./.venv/bin/everlog launchd capture restart
   ```
3) それでも失敗する場合は、手動で `.app` を起動して「許可ダイアログが出る状況」を作る
   ```sh
   /Users/arima/DEV/everytimecapture/macos_app/dist/everlog.app/Contents/MacOS/everlog capture
   ```
4) 最終手段: TCC リセット → `.app` を再度許可 → `restart`
   ```sh
   tccutil reset ScreenCapture
   # → システム設定で再度 .app を追加して ON
   ./.venv/bin/everlog launchd capture restart
   ```

## TODO（App化の未解決）
- ~~**スクショ間隔ごとに許諾ダイアログが毎回出る問題を解消**~~ → **py2app 版で解消見込み**
  - py2app でビルドした `.app` は固有の Bundle ID (`com.everlog.app`) と専用実行体を持つため、
    TCC が安定して同一アプリとして認識する
  - 1回許可すれば以後はサイレントに動くはず（要検証）
- 将来の改善:
  - コード署名（Developer ID）を追加すると、さらに安定する
  - GUI（メニューバー/設定画面）を足す場合は `LSUIElement` を `False` に変更

## 補足（なぜ `.app` でやるのか）
- launchd（バックグラウンド）＋ raw python 実行だと「権限の付与対象が分からない/付けられない」問題が起きやすい
- `.app` なら System Settings で確実に権限を付けられ、将来GUIを足すときも自然に拡張できる
