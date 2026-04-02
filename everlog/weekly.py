from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Iterator

from .llm import (
    DEFAULT_LLM_MODEL,
    LlmError,
    analyze_weekly_summary,
    analyze_weekly_task_clusters,
    calc_cost_usd,
    openai_endpoint_reachable,
)
from .notion_sync import notion_sync_enabled, sync_weekly
from .paths import ensure_dirs, get_paths
from .summarize import _extract_usage_tokens_full, _fmt_cost, _fmt_int, build_day_snapshot

ProgressCallback = Callable[[int, str], None]


@dataclass
class WeeklyPendingItem:
    week_start: str
    week_end: str
    stage: str
    retry_count: int
    last_attempted_at: str
    next_retry_after: str
    last_error_kind: str
    last_error: str
    updated_at: str


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _format_date(value: date) -> str:
    return value.isoformat()


def _normalize_week_start(value: str) -> str:
    d = _parse_iso_date(value)
    if d.weekday() != 0:
        raise ValueError(f"week_start must be Monday: {value}")
    return _format_date(d)


def _week_end(week_start: str) -> str:
    return _format_date(_parse_iso_date(week_start) + timedelta(days=6))


def _week_dates(week_start: str) -> list[str]:
    start = _parse_iso_date(week_start)
    return [_format_date(start + timedelta(days=i)) for i in range(7)]


def _default_week_start(today: date | None = None) -> str:
    cur = today or _now().date()
    this_monday = cur - timedelta(days=cur.weekday())
    return _format_date(this_monday - timedelta(days=7))


def _fmt_hm(total_sec: int) -> str:
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    return f"{h}時間{m}分" if h else f"{m}分"


def _weekly_usage_line(label: str, usage: dict[str, Any] | None, cost: float | None, model: str) -> str:
    tokens = _extract_usage_tokens_full(usage)
    if not tokens:
        return f"  - {label}: 未実行"
    inp, outp, cached = tokens
    cached_note = f"（cached {_fmt_int(cached)}）" if cached else ""
    model_note = f" / model {model}" if model else ""
    return (
        f"  - {label}: input {_fmt_int(inp)}{cached_note} / output {_fmt_int(outp)} tokens"
        f"（cost: {_fmt_cost(cost)}）{model_note}"
    )


def _weekday_ja(value: str) -> str:
    names = ["月", "火", "水", "木", "金", "土", "日"]
    try:
        return names[_parse_iso_date(value).weekday()]
    except Exception:
        return ""


def _llm_timeout_sec() -> int:
    raw = (
        os.environ.get("EVERLOG_LLM_TIMEOUT_SEC")
        or os.environ.get("EVERYTIMECAPTURE_LLM_TIMEOUT_SEC")
        or ""
    ).strip()
    try:
        timeout = int(raw) if raw else 180
    except Exception:
        timeout = 180
    return max(30, timeout)


def _weekly_llm_timeout_sec() -> int:
    raw = (
        os.environ.get("EVERLOG_WEEKLY_LLM_TIMEOUT_SEC")
        or os.environ.get("EVERYTIMECAPTURE_WEEKLY_LLM_TIMEOUT_SEC")
        or ""
    ).strip()
    try:
        timeout = int(raw) if raw else min(_llm_timeout_sec(), 45)
    except Exception:
        timeout = min(_llm_timeout_sec(), 45)
    return max(15, timeout)


def _weekly_llm_max_attempts() -> int:
    raw = (
        os.environ.get("EVERLOG_WEEKLY_LLM_MAX_ATTEMPTS")
        or os.environ.get("EVERYTIMECAPTURE_WEEKLY_LLM_MAX_ATTEMPTS")
        or ""
    ).strip()
    try:
        attempts = int(raw) if raw else 1
    except Exception:
        attempts = 1
    return max(1, attempts)


def _weekly_retention_days() -> int:
    raw = (
        os.environ.get("EVERLOG_WEEKLY_RETENTION_DAYS")
        or os.environ.get("EVERYTIMECAPTURE_WEEKLY_RETENTION_DAYS")
        or ""
    ).strip()
    try:
        days = int(raw) if raw else 20
    except Exception:
        days = 20
    return max(1, days)


def _weekly_root() -> Path:
    return get_paths().home / "weekly"


def _weekly_days_dir() -> Path:
    path = _weekly_root() / "days"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _weekly_weeks_dir() -> Path:
    path = _weekly_root() / "weeks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_path() -> Path:
    path = _weekly_root() / "weekly_pending.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _lock_path() -> Path:
    path = _weekly_root() / ".weekly-run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _week_dir(week_start: str) -> Path:
    path = _weekly_weeks_dir() / week_start
    path.mkdir(parents=True, exist_ok=True)
    return path


def _week_meta_path(week_start: str) -> Path:
    return _week_dir(week_start) / "weekly.meta.json"


def _week_clusters_path(week_start: str) -> Path:
    return _week_dir(week_start) / "weekly.clusters.json"


def _week_summary_llm_path(week_start: str) -> Path:
    return _week_dir(week_start) / "weekly.summary.llm.json"


def _normalize_report_filename(filename: str | None) -> str:
    name = str(filename or "weekly.report.md").strip()
    if not name:
        return "weekly.report.md"
    if Path(name).name != name:
        raise ValueError(f"output filename must not contain path separators: {filename}")
    if not name.endswith(".md"):
        name = f"{name}.md"
    return name


def _week_report_path(week_start: str, report_filename: str | None = None) -> Path:
    return _week_dir(week_start) / _normalize_report_filename(report_filename)


def _week_hourly_only_report_path(week_start: str) -> Path:
    return _week_dir(week_start) / "weekly.report.hourly-only.md"


def _day_snapshot_path(day: str) -> Path:
    return _weekly_days_dir() / f"{day}.hourly.json"


def cleanup_weekly_storage() -> None:
    ensure_dirs()
    retention_days = _weekly_retention_days()
    today = _now().date()

    days_dir = _weekly_days_dir()
    for path in days_dir.glob("*.hourly.json"):
        try:
            day = _parse_iso_date(path.name.replace(".hourly.json", ""))
        except Exception:
            continue
        if (today - day).days >= retention_days:
            path.unlink(missing_ok=True)

    weeks_dir = _weekly_weeks_dir()
    for path in weeks_dir.iterdir():
        if not path.is_dir():
            continue
        try:
            week_start = _parse_iso_date(path.name)
        except Exception:
            continue
        if (today - week_start).days >= retention_days:
            shutil.rmtree(path, ignore_errors=True)


def _load_day_snapshot(day: str) -> dict[str, Any]:
    path = _day_snapshot_path(day)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_pending() -> dict[str, WeeklyPendingItem]:
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
    out: dict[str, WeeklyPendingItem] = {}
    for row in items:
        if not isinstance(row, dict):
            continue
        week_start = str(row.get("week_start") or "").strip()
        week_end = str(row.get("week_end") or "").strip()
        if not week_start or not week_end:
            continue
        out[week_start] = WeeklyPendingItem(
            week_start=week_start,
            week_end=week_end,
            stage=str(row.get("stage") or ""),
            retry_count=int(row.get("retry_count") or 0),
            last_attempted_at=str(row.get("last_attempted_at") or ""),
            next_retry_after=str(row.get("next_retry_after") or ""),
            last_error_kind=str(row.get("last_error_kind") or ""),
            last_error=str(row.get("last_error") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )
    return out


def _save_pending(items: dict[str, WeeklyPendingItem]) -> None:
    rows = [
        {
            "week_start": item.week_start,
            "week_end": item.week_end,
            "stage": item.stage,
            "retry_count": item.retry_count,
            "last_attempted_at": item.last_attempted_at,
            "next_retry_after": item.next_retry_after,
            "last_error_kind": item.last_error_kind,
            "last_error": item.last_error,
            "updated_at": item.updated_at,
        }
        for _, item in sorted(items.items())
    ]
    _pending_path().write_text(
        json.dumps({"items": rows, "updated_at": _now_iso()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def has_pending_weeks() -> bool:
    return bool(_load_pending())


def _pending_due(item: WeeklyPendingItem, now: datetime | None = None) -> bool:
    if not item.next_retry_after:
        return True
    try:
        due_at = datetime.fromisoformat(item.next_retry_after)
    except Exception:
        return True
    return due_at <= (now or _now())


def _mark_pending(
    items: dict[str, WeeklyPendingItem],
    *,
    week_start: str,
    week_end: str,
    stage: str,
    error_kind: str,
    error: str,
    retry_delay_min: int = 10,
) -> None:
    prev = items.get(week_start)
    retry_count = int(prev.retry_count) + 1 if prev else 1
    now = _now()
    items[week_start] = WeeklyPendingItem(
        week_start=week_start,
        week_end=week_end,
        stage=stage,
        retry_count=retry_count,
        last_attempted_at=now.isoformat(),
        next_retry_after=(now + timedelta(minutes=retry_delay_min)).isoformat(),
        last_error_kind=error_kind,
        last_error=error,
        updated_at=now.isoformat(),
    )
    print(
        f"[weekly] Marked pending: {week_start} stage={stage} retry={retry_count} "
        f"kind={error_kind} error={error}"
    )


def _clear_pending(items: dict[str, WeeklyPendingItem], week_start: str) -> None:
    if week_start in items:
        del items[week_start]
        print(f"[weekly] Cleared pending: {week_start}")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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


def weekly_run_locked() -> bool:
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
def _weekly_run_lock() -> Iterator[bool]:
    ensure_dirs()
    cleanup_weekly_storage()
    path = _lock_path()
    payload = {"pid": os.getpid(), "started_at": _now_iso()}
    acquired = False

    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if weekly_run_locked():
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


def _build_task_items(snapshots: list[dict[str, Any]], *, prefer_enriched: bool = True) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for snapshot in snapshots:
        day = str(snapshot.get("date") or "").strip()
        hours = snapshot.get("hours") or []
        if not isinstance(hours, list):
            continue
        for idx, hour in enumerate(hours):
            if not isinstance(hour, dict):
                continue
            items.append(
                {
                    "id": f"{day}:{idx}",
                    "date": day,
                    "hour_start_ts": str(hour.get("hour_start_ts") or ""),
                    "hour_end_ts": str(hour.get("hour_end_ts") or ""),
                    "hour_title": (
                        (
                            str(hour.get("hour_title_enriched") or "").strip()
                            or str(hour.get("hour_title") or "").strip()
                        )
                        if prefer_enriched
                        else str(hour.get("hour_title") or "").strip()
                    ),
                    "hour_summary": (
                        (
                            str(hour.get("hour_summary_enriched") or "").strip()
                            or str(hour.get("hour_summary") or "").strip()
                        )
                        if prefer_enriched
                        else str(hour.get("hour_summary") or "").strip()
                    ),
                    "cluster_labels": list(hour.get("cluster_labels") or []),
                    "active_sec_est": int(hour.get("active_sec_est") or 0),
                }
            )
    return items


def _snapshot_hours(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    hours = snapshot.get("hours") or []
    if not isinstance(hours, list):
        return []
    return [hour for hour in hours if isinstance(hour, dict)]


def _snapshot_is_usable(snapshot: dict[str, Any]) -> bool:
    status = str(snapshot.get("status") or "").strip()
    if status == "complete":
        return True
    return bool(_snapshot_hours(snapshot) or _snapshot_daily(snapshot))


def _snapshot_input_summary(snapshots: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    incomplete_days: list[dict[str, Any]] = []
    for snapshot in snapshots:
        status = str(snapshot.get("status") or "").strip()
        if status == "complete":
            continue
        incomplete_days.append(
            {
                "date": str(snapshot.get("date") or "").strip(),
                "status": status or "incomplete",
                "incomplete_reason": str(snapshot.get("incomplete_reason") or "").strip(),
                "hour_count": len(_snapshot_hours(snapshot)),
                "total_active_sec_est": int(snapshot.get("total_active_sec_est") or 0),
            }
        )
    return ("complete" if not incomplete_days else "incomplete"), incomplete_days


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _dedupe_texts(values: list[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = _clean_text(raw)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if limit is not None and len(out) >= limit:
            break
    return out


def _looks_toolish_text(value: str) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    markers = ("http", ".com", " / ", "Google Chrome")
    return any(marker in text for marker in markers)


def _snapshot_daily(snapshot: dict[str, Any]) -> dict[str, Any]:
    daily = snapshot.get("daily") or {}
    return daily if isinstance(daily, dict) else {}


def _snapshot_highlights(snapshot: dict[str, Any]) -> list[str]:
    daily = _snapshot_daily(snapshot)
    values = daily.get("highlights") or []
    if not isinstance(values, list):
        return []
    return _dedupe_texts([str(v or "") for v in values], limit=5)


def _snapshot_hour_narratives(snapshot: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for hour in _snapshot_hours(snapshot):
        enriched = _clean_text(hour.get("hour_summary_enriched"))
        summary = _clean_text(hour.get("hour_summary"))
        if enriched:
            texts.append(enriched)
        elif summary:
            texts.append(summary)
    return _dedupe_texts(texts, limit=4)


def _snapshot_hour_titles(snapshot: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    for hour in _snapshot_hours(snapshot):
        enriched = _clean_text(hour.get("hour_title_enriched"))
        title = _clean_text(hour.get("hour_title"))
        if enriched:
            titles.append(enriched)
        elif title:
            titles.append(title)
    return _dedupe_texts(titles, limit=4)


def _build_day_note(snapshot: dict[str, Any]) -> dict[str, Any]:
    day = str(snapshot.get("date") or "").strip()
    daily = _snapshot_daily(snapshot)
    daily_title = _clean_text(daily.get("daily_title"))
    daily_summary = _clean_text(daily.get("daily_summary"))
    daily_detail = _clean_text(daily.get("daily_detail"))
    highlights = _snapshot_highlights(snapshot)
    hour_narratives = _snapshot_hour_narratives(snapshot)
    hour_titles = _snapshot_hour_titles(snapshot)

    title = daily_title or (hour_titles[0] if hour_titles and not _looks_toolish_text(hour_titles[0]) else "")
    overview = daily_summary or daily_detail or (hour_narratives[0] if hour_narratives else "")
    detail = ""
    if daily_detail and daily_detail != overview:
        detail = daily_detail
    elif len(hour_narratives) >= 2:
        detail = " ".join(hour_narratives[1:3])
    elif highlights:
        detail = "主な論点は" + "、".join(highlights[:3]) + "。"

    if not overview and title:
        overview = f"{title}を中心に進めた。"
    if not detail and len(highlights) >= 2:
        detail = "週報用の要点として、" + "、".join(highlights[:3]) + "を整理した。"

    return {
        "date": day,
        "weekday": _weekday_ja(day),
        "title": title,
        "overview": overview,
        "detail": detail,
        "highlights": highlights,
        "hour_titles": hour_titles,
        "hour_narratives": hour_narratives,
        "status": str(snapshot.get("status") or "").strip() or "incomplete",
        "incomplete_reason": _clean_text(snapshot.get("incomplete_reason")),
        "total_active_sec_est": int(snapshot.get("total_active_sec_est") or 0),
    }


def _build_day_notes(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _build_day_notes_hourly_only(snapshots)


def _build_day_note_hourly_only(snapshot: dict[str, Any]) -> dict[str, Any]:
    day = str(snapshot.get("date") or "").strip()
    titles = _dedupe_texts(
        [
            _clean_text(hour.get("hour_title"))
            for hour in _snapshot_hours(snapshot)
            if _clean_text(hour.get("hour_title"))
        ],
        limit=4,
    )
    summaries = _dedupe_texts(
        [
            _clean_text(hour.get("hour_summary"))
            for hour in _snapshot_hours(snapshot)
            if _clean_text(hour.get("hour_summary"))
        ],
        limit=4,
    )
    title = titles[0] if titles and not _looks_toolish_text(titles[0]) else ""
    overview = summaries[0] if summaries else ""
    detail = " ".join(summaries[1:3]) if len(summaries) >= 2 else ""
    return {
        "date": day,
        "weekday": _weekday_ja(day),
        "title": title,
        "overview": overview,
        "detail": detail,
        "highlights": [],
        "hour_titles": titles,
        "hour_narratives": summaries,
        "status": str(snapshot.get("status") or "").strip() or "incomplete",
        "incomplete_reason": _clean_text(snapshot.get("incomplete_reason")),
        "total_active_sec_est": int(snapshot.get("total_active_sec_est") or 0),
    }


def _build_day_notes_hourly_only(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_build_day_note_hourly_only(snapshot) for snapshot in snapshots]


def _week_themes(day_notes: list[dict[str, Any]]) -> list[str]:
    candidates = []
    for note in day_notes:
        title = _clean_text(note.get("title"))
        if title and not _looks_toolish_text(title):
            candidates.append(title.removesuffix("の日"))
        for item in note.get("highlights") or []:
            text = _clean_text(item)
            if text and not _looks_toolish_text(text):
                candidates.append(text)
    return _dedupe_texts(candidates, limit=3)


def _build_weekly_summary_fallback(
    week_start: str,
    week_end: str,
    day_notes: list[dict[str, Any]],
) -> dict[str, Any]:
    active_notes = [
        note
        for note in day_notes
        if _clean_text(note.get("overview")) or _clean_text(note.get("detail")) or _clean_text(note.get("title"))
    ]
    themes = _week_themes(day_notes)
    if themes:
        title_seed = "と".join(themes[:2])
        weekly_title = (title_seed if len(title_seed) <= 36 else themes[0]) + "を進めた週"
    else:
        weekly_title = f"{week_start} - {week_end} の振り返り"

    summary_parts: list[str] = []
    if themes:
        summary_parts.append(f"今週は{themes[0]}を軸に進めた。")
    if len(active_notes) >= 2:
        summary_parts.append(f"{len(active_notes)}日分の記録から、前半から後半にかけて論点を整理しながら作業を前進させたことが読み取れる。")
    elif active_notes:
        summary_parts.append("記録のある日は限られるものの、継続的に進めたテーマが確認できる。")
    weekly_summary = " ".join(summary_parts) or "今週の記録をもとに主要テーマを振り返った。"

    detail_parts: list[str] = []
    for note in active_notes[:4]:
        coherent = bool(note.get("highlights")) or (
            str(note.get("status") or "").strip() == "complete" and _clean_text(note.get("title"))
        )
        if not coherent:
            continue
        label = f"{note['date']}（{note['weekday']}）" if note.get("weekday") else str(note.get("date") or "")
        overview = _clean_text(note.get("overview"))
        detail_text = _clean_text(note.get("detail"))
        text = ""
        if overview and not _looks_toolish_text(overview):
            text = overview
        elif detail_text and not _looks_toolish_text(detail_text):
            text = detail_text
        if text:
            detail_parts.append(f"{label}は{text}")
    if themes:
        detail_parts.append(f"週全体では{themes[0]}を中心に、関連する判断や整理を積み重ねていた。")
    if active_notes:
        detail_parts.append("次のアクションは、週内で整理した論点を具体的な実装や連携作業へ落とし込むことにある。")
    weekly_detail = " ".join(detail_parts) or "週全体の詳細を組み立てるだけの十分な記録はありませんでした。"

    highlights: list[str] = []
    for note in active_notes:
        title = _clean_text(note.get("title"))
        if title and not _looks_toolish_text(title):
            highlights.append(title.removesuffix("の日"))
        for item in note.get("highlights") or []:
            text = _clean_text(item)
            if text and not _looks_toolish_text(text):
                highlights.append(text)
    highlights = _dedupe_texts(highlights, limit=3)

    return {
        "week_start": week_start,
        "week_end": week_end,
        "weekly_title": weekly_title,
        "weekly_summary": weekly_summary,
        "weekly_detail": weekly_detail,
        "highlights": highlights,
        "confidence": 0.0,
        "source": "fallback",
    }


def _merge_weekly_summary(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    if not primary:
        return dict(fallback)
    merged = dict(fallback)
    merged.update(primary)
    for key in ("weekly_title", "weekly_summary", "weekly_detail"):
        if not _clean_text(merged.get(key)):
            merged[key] = fallback.get(key, "")
    highlights = primary.get("highlights") if isinstance(primary, dict) else None
    if isinstance(highlights, list):
        merged["highlights"] = _dedupe_texts([str(v or "") for v in highlights], limit=5)
    if not merged.get("highlights"):
        merged["highlights"] = list(fallback.get("highlights") or [])
    if str(primary.get("source") or "").strip():
        merged["source"] = str(primary.get("source") or "").strip()
    return merged


def _validate_clusters(task_items: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> dict[str, Any]:
    task_ids = [str(item.get("id") or "") for item in task_items if str(item.get("id") or "").strip()]
    assigned: list[str] = []
    empty_cluster_ids: list[str] = []
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        item_ids = [str(item_id or "") for item_id in (cluster.get("item_ids") or []) if str(item_id or "").strip()]
        if not item_ids:
            empty_cluster_ids.append(str(cluster.get("cluster_id") or ""))
        assigned.extend(item_ids)
    duplicates = sorted({item_id for item_id in assigned if assigned.count(item_id) > 1})
    unassigned = sorted(set(task_ids) - set(assigned))
    unknown = sorted(set(assigned) - set(task_ids))
    return {
        "unassigned_item_ids": unassigned,
        "duplicate_item_ids": duplicates,
        "unknown_item_ids": unknown,
        "empty_cluster_ids": empty_cluster_ids,
        "is_valid": not unassigned and not duplicates and not unknown and not empty_cluster_ids,
    }


def _fallback_cluster_name(task_item: dict[str, Any]) -> str:
    labels = [str(label or "").strip() for label in (task_item.get("cluster_labels") or []) if str(label or "").strip()]
    if labels:
        return " / ".join(labels[:2])
    title = str(task_item.get("hour_title") or "").strip()
    if title:
        return title
    summary = str(task_item.get("hour_summary") or "").strip()
    if summary:
        return summary[:40] + ("…" if len(summary) > 40 else "")
    return "（名称未設定）"


def _build_fallback_clusters_data(
    week_start: str,
    week_end: str,
    task_items: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in task_items:
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue
        canonical_name = _fallback_cluster_name(item)
        key = " ".join(canonical_name.split()).lower() or item_id
        row = grouped.setdefault(
            key,
            {
                "canonical_task_name": canonical_name,
                "item_ids": [],
                "total_active_sec_est": 0,
            },
        )
        row["item_ids"].append(item_id)
        row["total_active_sec_est"] += int(item.get("active_sec_est") or 0)

    sorted_rows = sorted(
        grouped.values(),
        key=lambda row: (
            -int(row.get("total_active_sec_est") or 0),
            str(row.get("canonical_task_name") or ""),
        ),
    )
    clusters = [
        {
            "cluster_id": f"fallback-{idx}",
            "canonical_task_name": str(row.get("canonical_task_name") or "").strip() or "（名称未設定）",
            "item_ids": list(row.get("item_ids") or []),
            "confidence": 0.2,
        }
        for idx, row in enumerate(sorted_rows, start=1)
    ]
    return {
        "week_start": week_start,
        "week_end": week_end,
        "model": DEFAULT_LLM_MODEL,
        "generated_at": _now_iso(),
        "task_items": task_items,
        "clusters": clusters,
        "validation": _validate_clusters(task_items, clusters),
        "usage": {},
        "cost_usd": None,
        "strategy": "fallback",
        "fallback_reason": reason,
    }


def _run_weekly_clustering(
    week_start: str,
    week_end: str,
    task_items: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not task_items:
        return (
            {
                "week_start": week_start,
                "week_end": week_end,
                "model": DEFAULT_LLM_MODEL,
                "generated_at": _now_iso(),
                "task_items": [],
                "clusters": [],
                "validation": {
                    "unassigned_item_ids": [],
                    "duplicate_item_ids": [],
                    "unknown_item_ids": [],
                    "empty_cluster_ids": [],
                    "is_valid": True,
                },
                "usage": {},
                "cost_usd": None,
            },
            None,
            None,
        )

    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    last_error = "weekly clustering fell back"
    last_error_kind = "other"
    max_attempts = _weekly_llm_max_attempts()
    for attempt in range(max_attempts):
        try:
            res = analyze_weekly_task_clusters(
                week_start,
                week_end,
                task_items,
                DEFAULT_LLM_MODEL,
                api_key,
                timeout_sec=_weekly_llm_timeout_sec(),
            )
            clusters = res.data.get("clusters") if isinstance(res.data, dict) else None
            clusters_list = clusters if isinstance(clusters, list) else []
            validation = _validate_clusters(task_items, clusters_list)
            if validation.get("is_valid"):
                return (
                    {
                        "week_start": week_start,
                        "week_end": week_end,
                        "model": res.model,
                        "generated_at": _now_iso(),
                        "task_items": task_items,
                        "clusters": clusters_list,
                        "validation": validation,
                        "usage": res.usage,
                        "cost_usd": calc_cost_usd(res.usage, res.model),
                    },
                    None,
                    None,
                )
            last_error_kind = "llm_invalid_json"
            last_error = f"invalid weekly clusters output: {json.dumps(validation, ensure_ascii=False)}"
        except LlmError as e:
            last_error = str(e)
            last_error_kind = "network_unreachable" if not openai_endpoint_reachable() else "other"
        if attempt + 1 < max_attempts:
            time.sleep(1.0 + attempt)
    return (
        _build_fallback_clusters_data(
            week_start,
            week_end,
            task_items,
            reason=last_error or "weekly clustering failed",
        ),
        last_error_kind,
        last_error or "weekly clustering failed",
    )


def _run_weekly_summary(
    week_start: str,
    week_end: str,
    day_notes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    usable_notes = [
        note
        for note in day_notes
        if _clean_text(note.get("overview")) or _clean_text(note.get("detail")) or _clean_text(note.get("title"))
    ]
    if not usable_notes:
        return None
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return None
    max_attempts = _weekly_llm_max_attempts()
    for attempt in range(max_attempts):
        try:
            res = analyze_weekly_summary(
                week_start,
                week_end,
                [
                    {
                        "date": str(note.get("date") or ""),
                        "weekday": str(note.get("weekday") or ""),
                        "title": str(note.get("title") or ""),
                        "overview": str(note.get("overview") or ""),
                        "detail": str(note.get("detail") or ""),
                        "highlights": list(note.get("highlights") or []),
                        "total_active_sec_est": int(note.get("total_active_sec_est") or 0),
                        "status": str(note.get("status") or ""),
                    }
                    for note in usable_notes
                ],
                DEFAULT_LLM_MODEL,
                api_key,
                timeout_sec=_weekly_llm_timeout_sec(),
            )
            data = res.data if isinstance(res.data, dict) else {}
            return {
                "week_start": week_start,
                "week_end": week_end,
                "model": res.model,
                "generated_at": _now_iso(),
                "weekly_title": str(data.get("weekly_title") or "").strip(),
                "weekly_summary": str(data.get("weekly_summary") or "").strip(),
                "weekly_detail": str(data.get("weekly_detail") or "").strip(),
                "highlights": [str(v or "").strip() for v in (data.get("highlights") or []) if str(v or "").strip()],
                "confidence": float(data.get("confidence") or 0.0),
                "usage": res.usage,
                "cost_usd": calc_cost_usd(res.usage, res.model),
                "source": "llm",
            }
        except LlmError:
            if attempt + 1 < max_attempts:
                time.sleep(1.0 + attempt)
    return None


def _daily_top_lines(task_items: list[dict[str, Any]], clusters_data: dict[str, Any]) -> dict[str, list[str]]:
    by_id = {
        str(item.get("id") or ""): item
        for item in task_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    per_day: dict[str, dict[str, int]] = {}
    for cluster in clusters_data.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        name = str(cluster.get("canonical_task_name") or "").strip() or "（名称未設定）"
        for item_id in cluster.get("item_ids") or []:
            item = by_id.get(str(item_id or ""))
            if not item:
                continue
            day = str(item.get("date") or "").strip()
            per_day.setdefault(day, {})
            per_day[day][name] = per_day[day].get(name, 0) + int(item.get("active_sec_est") or 0)

    out: dict[str, list[str]] = {}
    for day, by_name in per_day.items():
        rows = sorted(by_name.items(), key=lambda row: (-row[1], row[0]))
        out[day] = [f"{name}（{_fmt_hm(sec)}）" for name, sec in rows[:5]]
    return out


def _build_weekly_markdown(
    *,
    week_start: str,
    week_end: str,
    total_active_sec_est: int,
    day_notes: list[dict[str, Any]],
    summary_data: dict[str, Any] | None,
    clusters_data: dict[str, Any],
    report_variant_note: str | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# 週次レポート {week_start} - {week_end}")
    lines.append("")
    if report_variant_note:
        lines.append(f"- 生成方式: {report_variant_note}")
    lines.append(f"- 対象期間: {week_start} 〜 {week_end}")
    lines.append(f"- 推定総作業時間: {_fmt_hm(total_active_sec_est)}")
    lines.append(f"- 対象日数: {len(_week_dates(week_start))}日")
    lines.append("")

    lines.append("<details>")
    lines.append("<summary>📊 LLMサマリー</summary>")
    lines.append("")
    lines.append(f"- 対象期間: {week_start} 〜 {week_end}")
    lines.append(f"- 推定総作業時間: {_fmt_hm(total_active_sec_est)}")
    lines.append(f"- 対象日数: {len(_week_dates(week_start))}日")
    lines.append("- LLM使用量（内訳）:")

    cluster_usage = clusters_data.get("usage") if isinstance(clusters_data, dict) else None
    cluster_cost = clusters_data.get("cost_usd") if isinstance(clusters_data, dict) else None
    cluster_model = str(clusters_data.get("model") or "") if isinstance(clusters_data, dict) else ""
    summary_usage = summary_data.get("usage") if isinstance(summary_data, dict) else None
    summary_cost = summary_data.get("cost_usd") if isinstance(summary_data, dict) else None
    summary_model = str(summary_data.get("model") or "") if isinstance(summary_data, dict) else ""

    lines.append(
        _weekly_usage_line(
            "weekly-clustering（タスククラスタリング）",
            cluster_usage if isinstance(cluster_usage, dict) else None,
            float(cluster_cost) if isinstance(cluster_cost, (int, float)) else None,
            cluster_model,
        )
    )
    lines.append(
        _weekly_usage_line(
            "weekly-summary（総括生成）",
            summary_usage if isinstance(summary_usage, dict) else None,
            float(summary_cost) if isinstance(summary_cost, (int, float)) else None,
            summary_model,
        )
    )

    total_in = 0
    total_out = 0
    total_cost = 0.0
    any_cost = False
    for usage, cost in (
        (cluster_usage, cluster_cost),
        (summary_usage, summary_cost),
    ):
        tokens = _extract_usage_tokens_full(usage if isinstance(usage, dict) else None)
        if tokens:
            total_in += int(tokens[0])
            total_out += int(tokens[1])
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
            any_cost = True
    lines.append(
        f"  - 合計: input {_fmt_int(total_in)} / output {_fmt_int(total_out)} tokens（cost: {_fmt_cost(total_cost if any_cost else None)}）"
    )
    lines.append("")
    lines.append("</details>")
    lines.append("")

    lines.append("## 今週の総括")
    lines.append("")
    if summary_data:
        title = str(summary_data.get("weekly_title") or "").strip()
        summary = str(summary_data.get("weekly_summary") or "").strip()
        detail = str(summary_data.get("weekly_detail") or "").strip()
        highlights = [str(v or "").strip() for v in (summary_data.get("highlights") or []) if str(v or "").strip()]
        if title:
            lines.append(f"- タイトル: {title}")
        if summary:
            lines.append(f"- 概要: {summary}")
        if detail:
            lines.append("")
            lines.append(detail)
        if highlights:
            lines.append("")
            lines.append("## 今週のハイライト")
            lines.append("")
            for item in highlights[:5]:
                lines.append(f"- {item}")
    else:
        lines.append("今週の記録から十分な総括を作れませんでした。")
    lines.append("")

    lines.append("## 日別ログ")
    lines.append("")
    notes_by_day = {str(note.get("date") or ""): note for note in day_notes}
    for day in _week_dates(week_start):
        note = notes_by_day.get(day) or {
            "date": day,
            "weekday": _weekday_ja(day),
            "overview": "",
            "detail": "",
            "highlights": [],
            "status": "incomplete",
            "incomplete_reason": "snapshot_missing",
            "total_active_sec_est": 0,
        }
        weekday = str(note.get("weekday") or "").strip()
        label = f"{day}（{weekday}）" if weekday else day
        lines.append(f"### {label}")
        lines.append("")
        lines.append(f"- 推定作業時間: {_fmt_hm(int(note.get('total_active_sec_est') or 0))}")
        overview = str(note.get("overview") or "").strip()
        detail = str(note.get("detail") or "").strip()
        if overview:
            lines.append("")
            lines.append(overview)
        if detail and detail != overview:
            lines.append("")
            lines.append(detail)
        if not overview and not detail:
            lines.append("")
            lines.append("記録不足のため、この日の自然言語要約は生成できませんでした。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_week_outputs(
    *,
    week_start: str,
    week_end: str,
    snapshots: list[dict[str, Any]],
    day_notes: list[dict[str, Any]],
    clusters_data: dict[str, Any],
    summary_data: dict[str, Any] | None,
    notion_sync_status: str,
    snapshot_status: str,
    incomplete_days: list[dict[str, Any]],
    report_filename: str | None = None,
) -> Path:
    report_filename = _normalize_report_filename(report_filename)
    total_active_sec_est = sum(int(snapshot.get("total_active_sec_est") or 0) for snapshot in snapshots)
    meta = {
        "week_start": week_start,
        "week_end": week_end,
        "generated_at": _now_iso(),
        "report_filename": report_filename,
        "source_dates": [str(snapshot.get("date") or "").strip() for snapshot in snapshots],
        "total_active_sec_est": total_active_sec_est,
        "snapshot_status": snapshot_status,
        "incomplete_source_dates": incomplete_days,
        "day_notes": day_notes,
        "weekly_title": str(summary_data.get("weekly_title") or "").strip() if isinstance(summary_data, dict) else "",
        "summary_source": str(summary_data.get("source") or "").strip() if isinstance(summary_data, dict) else "",
        "clustering_status": "fallback" if str(clusters_data.get("strategy") or "") == "fallback" else "complete",
        "clustering_fallback_reason": str(clusters_data.get("fallback_reason") or "").strip(),
        "notion_sync_status": notion_sync_status,
    }
    report_md = _build_weekly_markdown(
        week_start=week_start,
        week_end=week_end,
        total_active_sec_est=total_active_sec_est,
        day_notes=day_notes,
        summary_data=summary_data,
        clusters_data=clusters_data,
    )
    _write_json(_week_clusters_path(week_start), clusters_data)
    if summary_data and str(summary_data.get("source") or "").strip() == "llm":
        _write_json(_week_summary_llm_path(week_start), summary_data)
    elif _week_summary_llm_path(week_start).exists():
        _week_summary_llm_path(week_start).unlink(missing_ok=True)
    _write_json(_week_meta_path(week_start), meta)
    report_path = _week_report_path(week_start, report_filename)
    report_path.write_text(report_md, encoding="utf-8")
    return report_path


def build_weekly_report_hourly_only_preview(
    week_start: str,
    *,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> Path | None:
    def _progress(percent: int, stage: str) -> None:
        if progress_callback:
            progress_callback(percent, stage)

    week_start = _normalize_week_start(week_start or _default_week_start())
    week_end = _week_end(week_start)
    _progress(10, f"weekly-preview: 日次スナップショット確認 {week_start}")
    snapshots: list[dict[str, Any]] = []
    for day in _week_dates(week_start):
        snapshot = _load_day_snapshot(day)
        if force or not snapshot:
            build_day_snapshot(day, force=force)
            snapshot = _load_day_snapshot(day)
        if not snapshot:
            return None
        snapshots.append(snapshot)

    snapshot_status, incomplete_days = _snapshot_input_summary(snapshots)
    day_notes = _build_day_notes_hourly_only(snapshots)
    task_items = _build_task_items(snapshots, prefer_enriched=False)
    clusters_data = _build_fallback_clusters_data(
        week_start,
        week_end,
        task_items,
        reason="hourly_only_preview",
    )
    summary_data = _build_weekly_summary_fallback(week_start, week_end, day_notes)
    report_md = _build_weekly_markdown(
        week_start=week_start,
        week_end=week_end,
        total_active_sec_est=sum(int(snapshot.get("total_active_sec_est") or 0) for snapshot in snapshots),
        day_notes=day_notes,
        summary_data=summary_data,
        clusters_data=clusters_data,
        report_variant_note="hour-llm only preview",
    )
    report_path = _week_hourly_only_report_path(week_start)
    report_path.write_text(report_md, encoding="utf-8")
    print(f"[weekly] Wrote hourly-only preview: {report_path}")
    _progress(100, f"weekly-preview: 完了 {week_start}")
    return report_path


def _retry_notion_sync_only(
    week_start: str,
    pending: dict[str, WeeklyPendingItem],
    report_filename: str | None = None,
) -> Path | None:
    meta_path = _week_meta_path(week_start)
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    stored_report_filename = ""
    if isinstance(meta, dict):
        stored_report_filename = str(meta.get("report_filename") or "").strip()
    report_path = _week_report_path(week_start, report_filename or stored_report_filename or None)
    if not report_path.exists():
        return None
    week_end = str(meta.get("week_end") or _week_end(week_start))
    try:
        sync_weekly(week_start, week_end, report_path)
        meta["notion_sync_status"] = "success"
        _write_json(meta_path, meta if isinstance(meta, dict) else {})
        _clear_pending(pending, week_start)
    except Exception as e:
        meta["notion_sync_status"] = "pending"
        _write_json(meta_path, meta if isinstance(meta, dict) else {})
        _mark_pending(
            pending,
            week_start=week_start,
            week_end=week_end,
            stage="notion_sync",
            error_kind="notion_error",
            error=str(e),
        )
    return report_path


def build_weekly_report(
    week_start: str,
    *,
    force: bool = False,
    pending_item: WeeklyPendingItem | None = None,
    progress_callback: ProgressCallback | None = None,
    report_filename: str | None = None,
) -> Path | None:
    def _progress(percent: int, stage: str) -> None:
        if progress_callback:
            progress_callback(percent, stage)

    week_start = _normalize_week_start(week_start)
    report_filename = _normalize_report_filename(report_filename)
    week_end = _week_end(week_start)
    pending = _load_pending()

    if pending_item and pending_item.stage == "notion_sync" and not force:
        _progress(20, f"weekly: Notion再同期 {week_start}")
        path = _retry_notion_sync_only(week_start, pending, report_filename)
        _save_pending(pending)
        return path

    report_path = _week_report_path(week_start, report_filename)
    meta_path = _week_meta_path(week_start)
    if report_path.exists() and meta_path.exists() and not force and week_start not in pending:
        return report_path

    _progress(10, f"weekly: 日次スナップショット確認 {week_start}")
    snapshots: list[dict[str, Any]] = []
    for day in _week_dates(week_start):
        snapshot = _load_day_snapshot(day)
        if force or (not snapshot):
            build_day_snapshot(day, force=force)
            snapshot = _load_day_snapshot(day)
        if not snapshot:
            reason = "snapshot_missing"
            error_kind = "network_unreachable" if openai_endpoint_reachable() is False else "snapshot_missing"
            _mark_pending(
                pending,
                week_start=week_start,
                week_end=week_end,
                stage="snapshot_generation",
                error_kind=error_kind,
                error=reason,
            )
            _save_pending(pending)
            return None
        if not _snapshot_is_usable(snapshot):
            reason = str(snapshot.get("incomplete_reason") or "snapshot_missing")
            error_kind = "network_unreachable" if openai_endpoint_reachable() is False else "snapshot_missing"
            _mark_pending(
                pending,
                week_start=week_start,
                week_end=week_end,
                stage="snapshot_generation",
                error_kind=error_kind,
                error=reason,
            )
            _save_pending(pending)
            return None
        snapshots.append(snapshot)

    snapshot_status, incomplete_days = _snapshot_input_summary(snapshots)
    day_notes = _build_day_notes(snapshots)
    task_items = _build_task_items(snapshots, prefer_enriched=False)
    _progress(40, f"weekly: タスククラスタリング {week_start}")
    clusters_data, error_kind, error = _run_weekly_clustering(week_start, week_end, task_items)
    if clusters_data is None:
        _mark_pending(
            pending,
            week_start=week_start,
            week_end=week_end,
            stage="task_clustering",
            error_kind=error_kind or "other",
            error=error or "weekly clustering failed",
        )
        _save_pending(pending)
        return None

    _progress(65, f"weekly: 週次総括生成 {week_start}")
    summary_fallback = _build_weekly_summary_fallback(week_start, week_end, day_notes)
    summary_llm = _run_weekly_summary(week_start, week_end, day_notes)
    summary_data = _merge_weekly_summary(summary_llm, summary_fallback)

    _progress(80, f"weekly: レポート書込 {week_start}")
    try:
        report_path = _write_week_outputs(
            week_start=week_start,
            week_end=week_end,
            snapshots=snapshots,
            day_notes=day_notes,
            clusters_data=clusters_data,
            summary_data=summary_data,
            notion_sync_status="pending" if notion_sync_enabled() else "skipped",
            snapshot_status=snapshot_status,
            incomplete_days=incomplete_days,
            report_filename=report_filename,
        )
    except Exception as e:
        _mark_pending(
            pending,
            week_start=week_start,
            week_end=week_end,
            stage="report_write",
            error_kind="other",
            error=str(e),
        )
        _save_pending(pending)
        return None

    _progress(92, f"weekly: Notion同期 {week_start}")
    if notion_sync_enabled():
        try:
            sync_weekly(week_start, week_end, report_path)
            meta = json.loads(_week_meta_path(week_start).read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                meta["notion_sync_status"] = "success"
                _write_json(_week_meta_path(week_start), meta)
            if snapshot_status == "complete":
                _clear_pending(pending, week_start)
        except Exception as e:
            meta = json.loads(_week_meta_path(week_start).read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                meta["notion_sync_status"] = "pending"
                _write_json(_week_meta_path(week_start), meta)
            _mark_pending(
                pending,
                week_start=week_start,
                week_end=week_end,
                stage="notion_sync",
                error_kind="notion_error",
                error=str(e),
            )
    elif snapshot_status == "complete":
        _clear_pending(pending, week_start)

    if snapshot_status != "complete":
        reasons = ", ".join(
            f"{item.get('date')}: {item.get('incomplete_reason') or 'incomplete'}"
            for item in incomplete_days
        )
        _mark_pending(
            pending,
            week_start=week_start,
            week_end=week_end,
            stage="snapshot_generation",
            error_kind="snapshot_missing",
            error=f"report_written_with_partial_snapshots ({reasons})",
        )
    elif str(clusters_data.get("strategy") or "") == "fallback":
        _mark_pending(
            pending,
            week_start=week_start,
            week_end=week_end,
            stage="task_clustering",
            error_kind="llm_invalid_json",
            error=(
                "report_written_with_fallback_clustering "
                f"({str(clusters_data.get('fallback_reason') or '').strip() or 'weekly clustering failed'})"
            ),
        )
    elif not notion_sync_enabled():
        _clear_pending(pending, week_start)

    _save_pending(pending)
    _progress(100, f"weekly: 完了 {week_start}")
    return report_path


def run_weekly_automation(
    *,
    week_start: str | None = None,
    retry_pending_only: bool = False,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
    output_name: str | None = None,
) -> list[Path]:
    ensure_dirs()
    cleanup_weekly_storage()
    pending = _load_pending()
    report_filename = _normalize_report_filename(output_name)
    targets: list[tuple[str, WeeklyPendingItem | None]] = []
    if retry_pending_only:
        now = _now()
        for item in sorted(pending.values(), key=lambda row: row.week_start):
            if _pending_due(item, now):
                targets.append((item.week_start, item))
    else:
        target = _normalize_week_start(week_start) if week_start else _default_week_start()
        targets.append((target, pending.get(target)))

    if not targets:
        print("[weekly] No target weeks to process.")
        return []

    outputs: list[Path] = []
    with _weekly_run_lock() as acquired:
        if not acquired:
            print("[weekly] Skip: another weekly-run is already active.")
            return []
        for idx, (target_week_start, pending_item) in enumerate(targets, start=1):
            if progress_callback:
                progress_callback(
                    int(((idx - 1) / max(1, len(targets))) * 100),
                    f"weekly: 開始 {target_week_start}",
                )
            path = build_weekly_report(
                target_week_start,
                force=force,
                pending_item=pending_item,
                progress_callback=progress_callback,
                report_filename=report_filename,
            )
            if path is not None:
                outputs.append(path)
    return outputs
