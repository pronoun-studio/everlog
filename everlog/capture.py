# Role: 「1回のキャプチャ」を実行してJSONLに追記する（スクショ→OCR→除外/マスク→保存）。
# How: 前面コンテキストを取得し、`screencapture` で一時画像を作成して `ecocr`（Vision）でOCRし、除外・マスキング後にJSONLへ追記する。
# Key functions: `run_capture_once()`, `_screencapture_to()`, `_today_jsonl_path()`
# Collaboration: `collect`（前面情報）/ `ocr`（OCR実行）/ `exclusions`（除外判定）/ `redact`（マスク）/ `jsonl`（追記）/ `paths`（保存先）/ `config`（設定）と連携する。
from __future__ import annotations

import os
import sys
import uuid
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
    cmd = ["/usr/sbin/screencapture", "-x", "-t", "png", str(path)]
    p = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if p.returncode != 0:
        raise ScreenCaptureError(
            cmd=cmd,
            returncode=p.returncode,
            stdout=(p.stdout or "").strip(),
            stderr=(p.stderr or "").strip(),
        )


def run_capture_once(force: bool = False) -> None:
    cfg = load_config()
    paths = ensure_dirs()

    ts, tz = now_iso_local()
    event_id = str(uuid.uuid4())

    ctx = collect_active_context(cfg)

    # まずメタデータだけで除外判定（タイトル等でログイン/決済っぽい場合）
    decision = should_exclude(ctx, ocr_preview=None, cfg=cfg)
    if decision.excluded and not force:
        append_jsonl(
            _today_jsonl_path(),
            {
                "id": event_id,
                "ts": ts,
                "tz": tz,
                "interval_sec": cfg.interval_sec,
                "active_app": ctx.active_app,
                "window_title": "[REDACTED]",
                "browser": None if not ctx.browser else {"name": ctx.browser.name, "url": "", "domain": ctx.browser.domain},
                # Keep schema stable: `ocr_text` is always present (empty when skipped).
                "ocr_text": "",
                "excluded": True,
                "excluded_reason": decision.reason,
            },
        )
        return

    img_path = paths.tmp_dir / f"{event_id}.png"
    try:
        _screencapture_to(img_path)
        ocr = run_local_ocr(img_path)
        decision2 = should_exclude(ctx, ocr_preview=ocr.text[:4000], cfg=cfg)
        if decision2.excluded and not force:
            append_jsonl(
                _today_jsonl_path(),
                {
                    "id": event_id,
                    "ts": ts,
                    "tz": tz,
                    "interval_sec": cfg.interval_sec,
                    "active_app": ctx.active_app,
                    "window_title": "[REDACTED]",
                    "browser": None
                    if not ctx.browser
                    else {"name": ctx.browser.name, "url": "", "domain": ctx.browser.domain},
                    "ocr_text": "",
                    "excluded": True,
                    "excluded_reason": decision2.reason,
                },
            )
            return

        text = redact_text(ocr.text, cfg)
        event: dict[str, Any] = {
            "id": event_id,
            "ts": ts,
            "tz": tz,
            "interval_sec": cfg.interval_sec,
            "active_app": ctx.active_app,
            "window_title": ctx.window_title,
            "browser": None
            if not ctx.browser
            else {"name": ctx.browser.name, "url": ctx.browser.url, "domain": ctx.browser.domain},
            "ocr_text": text,
            "excluded": False,
        }
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
                "browser": None
                if not ctx.browser
                else {"name": ctx.browser.name, "url": ctx.browser.url, "domain": ctx.browser.domain},
                "excluded": False,
                "ocr_text": "",
                "error": error,
                "runner": runner,
            },
        )
    finally:
        if not cfg.keep_screenshots:
            try:
                img_path.unlink(missing_ok=True)
            except Exception:
                pass
