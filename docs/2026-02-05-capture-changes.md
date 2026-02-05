# 2026-02-05 Capture Changes Summary

このドキュメントは、このチャット内で行ったキャプチャ関連の変更点と該当コード箇所をまとめたものです。

## 1) 目的
- 複数ディスプレイ環境で、キャプチャ画像を全ディスプレイ分取得する。
- OCRは取得した全画像から抽出する。
- `keep_screenshots` により一時画像の削除有無を制御する。

## 2) 主要なコード変更

### 2.1 複数ディスプレイのキャプチャ
**ファイル**: `everlog/capture.py`
**関数**: `_screencapture_to`

- `screencapture` を `-D` で display 1..N に対して実行する方式に変更。
- 出力ファイル名は `event_id.png`（display 1）と `event_id-d2.png`（display 2）以降。
- 画面数を超える `-D` で `Invalid display specified` が出たら、その時点でループを抜ける。

該当箇所（概略）:
```python
# everlog/capture.py
for display_idx in range(1, 7):
    out_path = path if display_idx == 1 else path.with_name(f"{path.stem}-d{display_idx}{path.suffix}")
    cmd = ["/usr/sbin/screencapture", "-x", "-t", "png", "-D", str(display_idx), str(out_path)]
    ...
    if "invalid display specified" in stderr_l:
        if captured_any:
            break
```

### 2.2 OCR対象を全画像に拡張
**ファイル**: `everlog/capture.py`
**関数**: `run_capture_once` 内（キャプチャ後）

- `event_id*.png` を glob で収集して全て OCR。
- OCR結果は `"\n\n".join()` で結合し、以後の除外判定・マスキングに利用。

該当箇所（概略）:
```python
img_paths = sorted(paths.tmp_dir.glob(f"{event_id}*.png"))
ocr_texts = []
for path in img_paths:
    ocr = run_local_ocr(path)
    if ocr.text:
        ocr_texts.append(ocr.text)
ocr_text = "\n\n".join(ocr_texts)
```

### 2.3 画像削除ロジックを複数ファイル対応
**ファイル**: `everlog/capture.py`
**関数**: `run_capture_once` の `finally` ブロック

- `keep_screenshots == false` の場合、`event_id*.png` の全ファイルを削除。

該当箇所（概略）:
```python
if not cfg.keep_screenshots:
    for path in (img_paths or [img_path]):
        path.unlink(missing_ok=True)
```

## 3) 設定変更
**ファイル**: `EVERYTIME-LOG/config.json`

- `keep_screenshots` を `true` に変更（自動削除を停止）

該当箇所:
```json
"keep_screenshots": true
```

## 4) ビルド・運用上の注意
- `.app` を使用している場合、**ソース変更は再ビルドしないと反映されない**。
- 反映手順（py2app版）:
  - `cd macos_app`
  - `../.venv/bin/python setup.py py2app`
- 再ビルド後、`capture` を再起動すること。

## 5) 期待される動作
- `EVERYTIME-LOG/tmp/` に同一 `event_id` で複数ファイルが生成される
  - 例: `xxxxxxxx-....png`, `xxxxxxxx-....-d2.png`, `xxxxxxxx-....-d3.png`
- OCRは全ファイルから抽出され、1回のキャプチャにつきJSONL 1行にまとまる

