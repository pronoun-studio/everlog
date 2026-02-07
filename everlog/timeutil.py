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


_RUN_ID_MINUTE: str | None = None
_RUN_ID_SEQ: int = 0


def _to_base36(n: int) -> str:
    if n <= 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    v = n
    while v > 0:
        v, r = divmod(v, 36)
        out = digits[r] + out
    return out


def make_run_id() -> str:
    dt = datetime.now().astimezone()
    minute_key = dt.strftime("%Y%m%d%H%M")
    global _RUN_ID_MINUTE, _RUN_ID_SEQ
    if _RUN_ID_MINUTE != minute_key:
        _RUN_ID_MINUTE = minute_key
        _RUN_ID_SEQ = 1
    else:
        _RUN_ID_SEQ += 1
    hour = dt.strftime("%H").lstrip("0") or "0"
    return f"{hour}-{dt.strftime('%M')}-{_to_base36(_RUN_ID_SEQ)}"
