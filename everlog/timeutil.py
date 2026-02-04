# Role: ログ用の時刻表現を統一し、`today` のような引数を日付に正規化する。
# How: ローカルタイムゾーン付きISO文字列（秒粒度）とUTCオフセットを生成し、日次処理で扱いやすい形式に揃える。
# Key functions: `now_iso_local()`, `normalize_date_arg()`
# Collaboration: `everlog/capture.py` がイベント時刻の生成に使い、`everlog/summarize.py` が日付引数の解釈に使う。
from __future__ import annotations

from datetime import datetime, timezone


def now_iso_local() -> tuple[str, str]:
    dt = datetime.now().astimezone()
    tz = dt.strftime("%z")
    tz_fmt = f"{tz[:3]}:{tz[3:]}" if len(tz) == 5 else tz
    return dt.isoformat(timespec="seconds"), tz_fmt


def normalize_date_arg(date_arg: str) -> str:
    if date_arg == "today":
        return datetime.now().astimezone().date().isoformat()
    return date_arg
