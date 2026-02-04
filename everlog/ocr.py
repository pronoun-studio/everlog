# Role: ローカルOCRを実行してテキストを返す（外部ヘルパー `ecocr` のラッパ）。
# How: ログディレクトリ配下の `bin/ecocr`（または環境変数）を起動し、stdoutのJSON（{"text":...}）をパースして返す。
# Key functions: `run_local_ocr()`, `OcrResult`
# Collaboration: `everlog/capture.py` が一時スクショパスを渡して結果を受け取る。Swift側実装は `ocr/ecocr/` にあり、ビルドして配置する。
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import ensure_dirs, get_paths


@dataclass(frozen=True)
class OcrResult:
    text: str


def run_local_ocr(image_path: Path) -> OcrResult:
    # v0.1: 外部OCR課金回避のためローカルOCR。実装は後で差し替え可能。
    # ここではmacOS環境で利用可能なOCRヘルパー（Swift/Vision）を想定し、
    # 環境変数 EVERLOG_OCR_BIN（互換: EVERYTIMECAPTURE_OCR_BIN）でパスを指定できるようにする。
    ensure_dirs()
    ocr_env = (os.environ.get("EVERLOG_OCR_BIN") or os.environ.get("EVERYTIMECAPTURE_OCR_BIN") or "").strip()
    if ocr_env:
        ocr_bin = Path(ocr_env).expanduser()
    else:
        ocr_bin = get_paths().bin_dir / "ecocr"
    if not ocr_bin.exists():
        raise RuntimeError(
            f"OCRヘルパーが見つかりません。`{ocr_bin}` にVision OCRバイナリを配置するか、"
            "環境変数 EVERLOG_OCR_BIN（互換: EVERYTIMECAPTURE_OCR_BIN）でパスを指定してください。"
        )

    proc = subprocess.run(
        [str(ocr_bin), str(image_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "OCR failed")
    data = json.loads(proc.stdout)
    return OcrResult(text=str(data.get("text", "")))
