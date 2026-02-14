from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any

from .paths import ensure_dirs, get_paths
from .summarize import summarize_day_to_markdown


_INCOMPLETE_MARKER = "⚠️ 未完成: 1時間LLM要約が無効/失敗のため、タイムラインは不完全です。"


@dataclass
class PendingItem:
    date: str
    retry_count: int
    last_error: str
    updated_at: str


def _pending_path() -> Path:
    return get_paths().home / "daily_pending.json"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _load_pending() -> dict[str, PendingItem]:
    path = _pending_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    out: dict[str, PendingItem] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "").strip()
        if not date:
            continue
        out[date] = PendingItem(
            date=date,
            retry_count=int(row.get("retry_count") or 0),
            last_error=str(row.get("last_error") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )
    return out


def _save_pending(items: dict[str, PendingItem]) -> None:
    path = _pending_path()
    rows: list[dict[str, Any]] = []
    for k in sorted(items.keys()):
        it = items[k]
        rows.append(
            {
                "date": it.date,
                "retry_count": int(it.retry_count),
                "last_error": it.last_error,
                "updated_at": it.updated_at,
            }
        )
    path.write_text(
        json.dumps({"items": rows, "updated_at": _now_iso()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _find_latest_md_for_date(date: str) -> Path | None:
    out_day = get_paths().out_dir / date
    if not out_day.exists():
        return None
    candidates: list[Path] = []
    for run_dir in out_day.iterdir():
        if not run_dir.is_dir():
            continue
        for md in run_dir.glob("*.md"):
            if md.is_file():
                candidates.append(md)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _is_summary_complete_for_date(date: str) -> bool:
    md = _find_latest_md_for_date(date)
    if not md:
        return False
    try:
        text = md.read_text(encoding="utf-8")
    except Exception:
        return False
    return _INCOMPLETE_MARKER not in text


def _mark_pending(items: dict[str, PendingItem], date: str, reason: str) -> None:
    prev = items.get(date)
    retry_count = int(prev.retry_count) + 1 if prev else 1
    items[date] = PendingItem(
        date=date,
        retry_count=retry_count,
        last_error=reason,
        updated_at=_now_iso(),
    )
    print(f"[daily] Marked pending: {date} (retry={retry_count}, reason={reason})")


def _clear_pending(items: dict[str, PendingItem], date: str) -> None:
    if date in items:
        del items[date]
        print(f"[daily] Cleared pending: {date}")


def _should_run_today(now: datetime) -> bool:
    # Keep "today summary" as a nightly job (23:55+).
    return now.hour == 23 and now.minute >= 55


def run_daily_automation() -> int:
    """
    Run daily summarize orchestration.

    Behavior:
      - Always retry pending dates first.
      - On startup/daytime, auto-try yesterday when it is missing/incomplete.
      - At 23:55+ run today's summarize as the regular daily job.
      - If LLM did not run (incomplete marker in markdown), keep date in pending.
    """
    ensure_dirs()
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    pending = _load_pending()

    queue: list[str] = []
    queue.extend(sorted(pending.keys()))
    if not _is_summary_complete_for_date(yesterday):
        queue.append(yesterday)
    if _should_run_today(now):
        queue.append(today)

    # preserve order, unique
    seen: set[str] = set()
    ordered: list[str] = []
    for d in queue:
        if d in seen:
            continue
        seen.add(d)
        ordered.append(d)

    if not ordered:
        print("[daily] No target dates to process.")
        return 0

    processed = 0
    for date in ordered:
        # Skip when already complete.
        if _is_summary_complete_for_date(date):
            _clear_pending(pending, date)
            continue
        try:
            print(f"[daily] Running summarize: {date}")
            out_path = summarize_day_to_markdown(date)
            processed += 1
            try:
                text = out_path.read_text(encoding="utf-8")
            except Exception:
                text = ""
            if _INCOMPLETE_MARKER in text:
                _mark_pending(pending, date, "LLM incomplete (hour-llm missing)")
            else:
                print(f"[daily] Summarize succeeded: {date}")
                _clear_pending(pending, date)
        except Exception as e:
            _mark_pending(pending, date, str(e))

    _save_pending(pending)
    return processed

