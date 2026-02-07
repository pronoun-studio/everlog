# Role: 「アクティブなディスプレイ（display index）」を取得する（外部ヘルパー `ecdisplay` のラッパ）。
# How: `EVERYTIME-LOG/bin/ecdisplay`（または環境変数指定）を起動し、stdoutのJSONをパースして返す。
# Key functions: `get_active_display_info()`
# Collaboration: `everlog/capture.py` がこの結果を JSONL に記録し、`ocr_by_display[].display` と対応づける。
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import ensure_dirs, get_paths


@dataclass(frozen=True)
class ActiveDisplayInfo:
    display: int | None
    source: str
    point_x: float | None = None
    point_y: float | None = None
    error: str | None = None


def get_active_display_info() -> ActiveDisplayInfo:
    """
    `screencapture -D N` の N（1始まり）として使える「アクティブディスプレイ番号」を推定して返す。

    - 原則: frontmost app の最前面ウィンドウ中心点が属するディスプレイ
    - フォールバック: マウス位置が属するディスプレイ

    返り値の `display` が None の場合、判定に失敗している（またはヘルパー未配置）。
    """
    ensure_dirs()

    env = (os.environ.get("EVERLOG_DISPLAY_BIN") or os.environ.get("EVERYTIMECAPTURE_DISPLAY_BIN") or "").strip()
    if env:
        bin_path = Path(env).expanduser()
    else:
        bin_path = get_paths().bin_dir / "ecdisplay"

    if not bin_path.exists():
        return ActiveDisplayInfo(
            display=None,
            source="missing",
            error=f"display helper not found: {bin_path}",
        )

    proc = subprocess.run(
        [str(bin_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        return ActiveDisplayInfo(
            display=None,
            source="error",
            error=(proc.stderr.strip() or "ecdisplay failed"),
        )

    try:
        data = json.loads(proc.stdout)
    except Exception as e:
        return ActiveDisplayInfo(
            display=None,
            source="error",
            error=f"failed to parse ecdisplay output: {e}",
        )

    point = data.get("point") if isinstance(data, dict) else None
    px = py = None
    if isinstance(point, dict):
        try:
            px = float(point.get("x")) if point.get("x") is not None else None
            py = float(point.get("y")) if point.get("y") is not None else None
        except Exception:
            px = py = None

    display_val = None
    if isinstance(data, dict) and data.get("active_display") is not None:
        try:
            display_val = int(data.get("active_display"))
        except Exception:
            display_val = None

    source = str(data.get("source") or "unknown") if isinstance(data, dict) else "unknown"
    err = str(data.get("error") or "") if isinstance(data, dict) else ""
    return ActiveDisplayInfo(
        display=display_val,
        source=source,
        point_x=px,
        point_y=py,
        error=(err or None),
    )

