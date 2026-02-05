# Role: 「1回のキャプチャ」を実行してJSONLに追記する（スクショ→OCR→除外/マスク→保存）。
# How: 前面コンテキストを取得し、`screencapture` で一時画像を作成して `ecocr`（Vision）でOCRし、除外・マスキング後にJSONLへ追記する。
# Key functions: `run_capture_once()`, `_screencapture_to()`, `_today_jsonl_path()`
# Collaboration: `collect`（前面情報）/ `ocr`（OCR実行）/ `exclusions`（除外判定）/ `redact`（マスク）/ `jsonl`（追記）/ `paths`（保存先）/ `config`（設定）と連携する。
from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import subprocess

from .collect import collect_active_context
from .config import load_config
from .exclusions import should_exclude
from .jsonl import append_jsonl
from .ocr import run_local_ocr
from .paths import ensure_dirs, get_paths
from .redact import redact_text
from .timeutil import now_iso_local


class ScreenCaptureError(RuntimeError):
    def __init__(self, *, cmd: list[str], returncode: int, stdout: str, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        msg = f"screencapture failed (code={returncode})"
        if stderr:
            msg += f": {stderr}"
        elif stdout:
            msg += f": {stdout}"
        super().__init__(msg)


def _today_jsonl_path() -> Path:
    paths = get_paths()
    date = os.environ.get("EVERLOG_DATE_OVERRIDE") or os.environ.get("EVERYTIMECAPTURE_DATE_OVERRIDE")
    if not date:
        from datetime import datetime

        date = datetime.now().astimezone().date().isoformat()
    return paths.logs_dir / f"{date}.jsonl"


def _runner_info() -> dict[str, Any]:
    exe = Path(sys.executable)
    try:
        exe_resolved = str(exe.resolve())
    except Exception:
        exe_resolved = str(exe)

    return {
        "python": str(exe),
        "python_resolved": exe_resolved,
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "launch_job_label": os.environ.get("LAUNCH_JOB_LABEL"),
        "xpc_service_name": os.environ.get("XPC_SERVICE_NAME"),
        "term": os.environ.get("TERM"),
        "isatty_stdin": sys.stdin.isatty(),
        "isatty_stdout": sys.stdout.isatty(),
        "isatty_stderr": sys.stderr.isatty(),
    }


def _screencapture_hint(stderr: str, *, python_resolved: str) -> str | None:
    s = (stderr or "").lower()
    if "could not create image from display" in s:
        return (
            "Screen Recording権限が無い/効いていない可能性が高いです。"
            "特にlaunchd経由では、権限は「実際に実行されるPython」に付与されます。"
            f"システム設定 → プライバシーとセキュリティ → 画面収録 に `{python_resolved}` を追加してONにし、"
            "その後 `everlog launchd capture restart` を試してください。"
        )
    return None


def _screencapture_to(path: Path) -> None:
    # -x: no sound / no UI
    # If Screen Recording permission is missing (common under launchd), screencapture exits non-zero.
    # Capture stderr to preserve the reason in JSONL for easier debugging.
    #
    # NOTE: In some environments, screencapture with a single output path only
    # captures the main display. To make multi-display capture reliable, iterate
    # displays with -D and write one file per display (event_id.png, event_id-d2.png, ...).
    captured_any = False
    for display_idx in range(1, 7):
        out_path = path if display_idx == 1 else path.with_name(f"{path.stem}-d{display_idx}{path.suffix}")
        cmd = ["/usr/sbin/screencapture", "-x", "-t", "png", "-D", str(display_idx), str(out_path)]
        p = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if p.returncode == 0:
            captured_any = True
            continue

        stderr = (p.stderr or "").strip()
        stderr_l = stderr.lower()
        # If the display index does not exist, stop after capturing existing displays.
        if "could not create image from display" in stderr_l or "invalid display specified" in stderr_l:
            if captured_any:
                break
        raise ScreenCaptureError(
            cmd=cmd,
            returncode=p.returncode,
            stdout=(p.stdout or "").strip(),
            stderr=stderr,
        )

    if not captured_any:
        raise ScreenCaptureError(
            cmd=["/usr/sbin/screencapture", "-x", "-t", "png", "-D", "1", str(path)],
            returncode=1,
            stdout="",
            stderr="screencapture produced no image files",
        )


def _display_index_from_path(path: Path) -> int:
    stem = path.stem
    m = re.search(r"-d(\d+)$", stem)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 1
    return 1


def _parse_pmset_assertions(output: str) -> dict[str, int]:
    stats: dict[str, int] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line or line.endswith(":"):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            stats[parts[0]] = int(parts[1])
    return stats


def _read_pmset_assertions() -> dict[str, int] | None:
    try:
        p = subprocess.run(
            ["/usr/bin/pmset", "-g", "assertions"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    stats = _parse_pmset_assertions(p.stdout or "")
    return stats or None


def _parse_pmset_log_event(line: str) -> tuple[datetime, str, str] | None:
    match = re.match(
        r"^(?P<ts>\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}) (?P<tz>[+-]\\d{4})\\s+(?P<event>\\w+)",
        line,
    )
    if not match:
        return None
    event = match.group("event")
    if event not in {"Sleep", "DarkWake", "Wake"}:
        return None
    try:
        dt = datetime.strptime(
            f"{match.group('ts')} {match.group('tz')}",
            "%Y-%m-%d %H:%M:%S %z",
        )
    except ValueError:
        return None
    return dt, event, line.strip()


def _read_pmset_recent_power_event(max_lines: int = 400) -> tuple[datetime, str, str] | None:
    try:
        p = subprocess.run(
            ["/usr/bin/pmset", "-g", "log"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    lines = (p.stdout or "").splitlines()
    if not lines:
        return None
    recent = lines[-max_lines:]
    last_event: tuple[datetime, str, str] | None = None
    for line in recent:
        parsed = _parse_pmset_log_event(line)
        if parsed:
            last_event = parsed
    return last_event


def _should_skip_capture_due_to_sleep() -> tuple[bool, dict[str, Any] | None]:
    stats = _read_pmset_assertions()
    if stats:
        user_active = stats.get("UserIsActive")
        prevent_idle_sleep = stats.get("PreventUserIdleSystemSleep")
        prevent_idle_display = stats.get("PreventUserIdleDisplaySleep")
        if (
            user_active == 0
            and prevent_idle_sleep == 0
            and (prevent_idle_display in (None, 0))
        ):
            return True, {
                "source": "pmset",
                "user_active": user_active,
                "prevent_idle_sleep": prevent_idle_sleep,
                "prevent_idle_display": prevent_idle_display,
            }

    recent = _read_pmset_recent_power_event()
    if recent:
        event_dt, event_name, raw = recent
        age_sec = (datetime.now(event_dt.tzinfo) - event_dt).total_seconds()
        if age_sec <= 900 and event_name in {"Sleep", "DarkWake"}:
            return True, {
                "source": "pmset_log",
                "event": event_name,
                "age_sec": int(age_sec),
                "line": raw,
            }

    return False, stats


def run_capture_once(force: bool = False) -> None:
    cfg = load_config()
    paths = ensure_dirs()

    ts, tz = now_iso_local()
    event_id = str(uuid.uuid4())

    if not force:
        skip, details = _should_skip_capture_due_to_sleep()
        if skip:
            detail_str = f" ({details})" if details else ""
            print(f"[capture] skipped due to sleep/inactive{detail_str}", file=sys.stderr)
            return

    ctx = collect_active_context(cfg)
    browser_obj = None if not ctx.browser else {"name": ctx.browser.name, "url": ctx.browser.url, "domain": ctx.browser.domain}
    active_context = {
        "app": ctx.active_app,
        "window_title": ctx.window_title,
        "browser": browser_obj,
    }

    # まずメタデータだけで除外判定（タイトル等でログイン/決済っぽい場合）
    decision = should_exclude(ctx, ocr_preview=None, cfg=cfg)
    if decision.excluded and not force:
        browser_redacted = None if not ctx.browser else {"name": ctx.browser.name, "url": "", "domain": ctx.browser.domain}
        active_context_redacted = {
            "app": ctx.active_app,
            "window_title": "[REDACTED]",
            "browser": browser_redacted,
        }
        append_jsonl(
            _today_jsonl_path(),
            {
                "id": event_id,
                "ts": ts,
                "tz": tz,
                "interval_sec": cfg.interval_sec,
                "active_app": ctx.active_app,
                "window_title": "[REDACTED]",
                "browser": browser_redacted,
                "active_context": active_context_redacted,
                # Keep schema stable: `ocr_text` is always present (empty when skipped).
                "ocr_text": "",
                "ocr_by_display": [],
                "excluded": True,
                "excluded_reason": decision.reason,
            },
        )
        return

    img_path = paths.tmp_dir / f"{event_id}.png"
    img_paths: list[Path] = []
    try:
        _screencapture_to(img_path)
        # If multiple displays are present, screencapture writes 1 file per screen.
        # Collect all matching outputs so OCR covers the entire workspace.
        img_paths = sorted(paths.tmp_dir.glob(f"{event_id}*.png"))
        if not img_paths:
            raise RuntimeError("screencapture produced no image files")

        ocr_by_display: list[dict[str, Any]] = []
        for path in img_paths:
            display_idx = _display_index_from_path(path)
            try:
                ocr = run_local_ocr(path)
                raw_text = ocr.text or ""
            except Exception as e:
                ocr_by_display.append(
                    {
                        "display": display_idx,
                        "image": path.name,
                        "ocr_text": "",
                        "excluded": False,
                        "error": {"message": str(e)},
                    }
                )
                continue

            decision2 = should_exclude(ctx, ocr_preview=raw_text[:4000], cfg=cfg)
            excluded = decision2.excluded and not force
            display_text = "" if excluded else redact_text(raw_text, cfg)
            entry: dict[str, Any] = {
                "display": display_idx,
                "image": path.name,
                "ocr_text": display_text,
                "excluded": excluded,
            }
            if excluded:
                entry["excluded_reason"] = decision2.reason
            ocr_by_display.append(entry)

        all_excluded = bool(ocr_by_display) and all(e.get("excluded", False) for e in ocr_by_display)
        event: dict[str, Any] = {
            "id": event_id,
            "ts": ts,
            "tz": tz,
            "interval_sec": cfg.interval_sec,
            "active_app": ctx.active_app,
            "window_title": ctx.window_title,
            "browser": browser_obj,
            "active_context": active_context,
            # ocr_text is kept for backward compatibility; prefer ocr_by_display.
            "ocr_text": "",
            "ocr_by_display": ocr_by_display,
            "excluded": all_excluded,
        }
        if all_excluded:
            reasons = sorted({e.get("excluded_reason", "") for e in ocr_by_display if e.get("excluded_reason")})
            if reasons:
                event["excluded_reason"] = ",".join(reasons)
        # osascript権限エラーがあれば記録（キャプチャ自体は続行）
        if ctx.osascript_error:
            event["osascript_error"] = ctx.osascript_error
        append_jsonl(_today_jsonl_path(), event)
    except Exception as e:
        runner = _runner_info()
        error: dict[str, Any] = {"message": str(e)}

        if isinstance(e, ScreenCaptureError):
            error.update(
                {
                    "stage": "screencapture",
                    "returncode": e.returncode,
                    "stderr": e.stderr,
                    "stdout": e.stdout,
                    "cmd": e.cmd,
                }
            )
            hint = _screencapture_hint(e.stderr, python_resolved=str(runner.get("python_resolved", "")))
            if hint:
                error["hint"] = hint

        append_jsonl(
            _today_jsonl_path(),
            {
                "id": event_id,
                "ts": ts,
                "tz": tz,
                "interval_sec": cfg.interval_sec,
                "active_app": ctx.active_app,
                "window_title": ctx.window_title,
                "browser": browser_obj,
                "active_context": active_context,
                "excluded": False,
                "ocr_text": "",
                "ocr_by_display": [],
                "error": error,
                "runner": runner,
            },
        )
    finally:
        if not cfg.keep_screenshots:
            for path in (img_paths or [img_path]):
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
