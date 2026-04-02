from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Iterator

from .paths import ensure_dirs, get_paths
from .summarize import build_day_snapshot
from .weekly import cleanup_weekly_storage, run_weekly_automation


@dataclass
class PendingItem:
    date: str
    retry_count: int
    last_error: str
    updated_at: str


def _pending_path() -> Path:
    return get_paths().home / "daily_pending.json"


def _lock_path() -> Path:
    return get_paths().home / ".daily-run.lock"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _now() -> datetime:
    return datetime.now().astimezone()


def _lock_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def daily_run_locked() -> bool:
    path = _lock_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    pid = int(data.get("pid") or 0)
    started_at = str(data.get("started_at") or "")
    if started_at:
        try:
            started = datetime.fromisoformat(started_at)
            if (_now() - started) > timedelta(hours=12):
                path.unlink(missing_ok=True)
                return False
        except Exception:
            pass
    if pid and _lock_pid_running(pid):
        return True
    path.unlink(missing_ok=True)
    return False


@contextmanager
def _daily_run_lock() -> Iterator[bool]:
    ensure_dirs()
    path = _lock_path()
    payload = {"pid": os.getpid(), "started_at": _now_iso()}
    acquired = False

    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if daily_run_locked():
                yield False
                return
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        acquired = True
        break

    if not acquired:
        yield False
        return

    try:
        yield True
    finally:
        path.unlink(missing_ok=True)


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


def _snapshot_path(date: str) -> Path:
    return get_paths().home / "weekly" / "days" / f"{date}.hourly.json"


def _is_snapshot_complete_for_date(date: str) -> bool:
    path = _snapshot_path(date)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(data.get("status") or "").strip() == "complete"


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
    # Keep "today snapshot" as a nightly job (23:55+).
    return now.hour == 23 and now.minute >= 55


def run_daily_automation() -> int:
    """
    Run daily snapshot orchestration.

    Behavior:
      - Always retry pending dates first.
      - On startup/daytime, auto-try yesterday when it is missing/incomplete.
      - At 23:55+ run today's snapshot as the regular daily job.
      - If snapshot is incomplete, keep date in pending.
    """
    with _daily_run_lock() as acquired:
        if not acquired:
            print("[daily] Skip: another daily-run is already active.")
            return 0

        ensure_dirs()
        cleanup_weekly_storage()
        now = _now()
        today = now.date().isoformat()
        yesterday = (now.date() - timedelta(days=1)).isoformat()

        pending = _load_pending()

        queue: list[str] = []
        queue.extend(sorted(pending.keys()))
        if not _is_snapshot_complete_for_date(yesterday):
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
            if _is_snapshot_complete_for_date(date):
                _clear_pending(pending, date)
                continue
            try:
                print(f"[daily] Building snapshot: {date}")
                out_path = build_day_snapshot(date)
                processed += 1
                try:
                    data = json.loads(out_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
                if str(data.get("status") or "").strip() != "complete":
                    reason = str(data.get("incomplete_reason") or "snapshot_incomplete")
                    _mark_pending(pending, date, reason)
                else:
                    print(f"[daily] Snapshot succeeded: {date}")
                    _clear_pending(pending, date)
            except Exception as e:
                _mark_pending(pending, date, str(e))

        _save_pending(pending)
        run_weekly_automation(retry_pending_only=True)
        return processed
