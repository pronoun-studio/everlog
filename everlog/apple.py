# Role: AppleScript（osascript）実行を1箇所に集約する。
# How: `osascript -e` をサブプロセス実行し、失敗時はstderrを含む例外として扱うことで呼び出し側の分岐を単純化する。
# Key functions: `run_osascript()`
# Collaboration: `everlog/collect.py` から呼ばれ、前面アプリ/ウィンドウ/Chromeタブ情報などの取得に使われる。
from __future__ import annotations

import subprocess


def run_osascript(script: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
    return proc.stdout.strip()
