# Role: `everlog` CLIの入口。サブコマンドを解釈して各機能に振り分ける。
# How: `argparse` でコマンド体系を定義し、capture/summarize/launchd/menubar を同一のCLIから実行できるようにする。
# Key functions: `main()`, `_parse_args()`
# Collaboration: captureは `everlog/capture.py`、日次生成は `everlog/summarize.py`、自動起動は `everlog/launchd.py`、UIは `everlog/menubar.py` に委譲する。
from __future__ import annotations

import argparse
import sys

from .capture import run_capture_once
from .daily_runner import run_daily_automation
from .enrich import enrich_day_with_llm
from .launchd import (
    launchd_capture_install,
    launchd_capture_restart,
    launchd_capture_start,
    launchd_capture_status,
    launchd_capture_stop,
    launchd_capture_uninstall,
    launchd_daily_install,
    launchd_daily_restart,
    launchd_daily_start,
    launchd_daily_status,
    launchd_daily_stop,
    launchd_daily_uninstall,
    launchd_menubar_install,
    launchd_menubar_restart,
    launchd_menubar_start,
    launchd_menubar_status,
    launchd_menubar_stop,
    launchd_menubar_stop_for_quit,
    launchd_menubar_uninstall,
)
from .menubar import run_menubar
from .summarize import summarize_day_to_markdown


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="everlog")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_capture = sub.add_parser("capture", help="Capture screenshot → OCR → append JSONL")
    p_capture.add_argument("--force", action="store_true", help="Capture even if excluded/locked heuristics match")

    p_sum = sub.add_parser("summarize", help="Summarize JSONL into daily Markdown")
    p_sum.add_argument("--date", default="today", help="YYYY-MM-DD or 'today'")
    sub.add_parser("daily-run", help="Run daily summarize orchestration (pending/yesterday/today)")

    p_enrich = sub.add_parser("enrich", help="Enrich JSONL via LLM (writes out/YYYY-MM-DD.llm.json)")
    p_enrich.add_argument("--date", default="today", help="YYYY-MM-DD or 'today'")
    p_enrich.add_argument(
        "--model",
        default=None,
        help="LLM model (default: EVERLOG_LLM_MODEL or gpt-5-nano; legacy: EVERYTIMECAPTURE_LLM_MODEL)",
    )
    p_enrich.add_argument("--max-segments", type=int, default=80, help="Max segments to send")

    p_launchd = sub.add_parser("launchd", help="Install/start/stop launchd agent")
    sub2 = p_launchd.add_subparsers(dest="launchd_target", required=True)

    p_l_capture = sub2.add_parser("capture", help="Periodic capture agent (StartInterval)")
    sub3 = p_l_capture.add_subparsers(dest="launchd_cmd", required=True)
    sub3.add_parser("install", help="Write plist and (re)load agent")
    sub3.add_parser("start", help="Load agent")
    sub3.add_parser("stop", help="Unload agent")
    sub3.add_parser("restart", help="Kickstart agent")
    sub3.add_parser("status", help="Print agent status")
    sub3.add_parser("uninstall", help="Unload agent and remove plist")

    p_l_menubar = sub2.add_parser("menubar", help="Menu bar agent (KeepAlive)")
    sub4 = p_l_menubar.add_subparsers(dest="launchd_cmd2", required=True)
    sub4.add_parser("install", help="Write plist and (re)load agent")
    sub4.add_parser("start", help="Load agent")
    sub4.add_parser("stop", help="Unload agent")
    sub4.add_parser("restart", help="Kickstart agent")
    sub4.add_parser("status", help="Print agent status")
    sub4.add_parser("uninstall", help="Unload agent and remove plist")
    sub4.add_parser("quit", help="Stop agent and disable autostart")

    p_l_daily = sub2.add_parser("daily", help="Daily enrich+summarize agent (23:55)")
    sub5 = p_l_daily.add_subparsers(dest="launchd_cmd3", required=True)
    sub5.add_parser("install", help="Write plist and (re)load agent")
    sub5.add_parser("start", help="Load agent")
    sub5.add_parser("stop", help="Unload agent")
    sub5.add_parser("restart", help="Kickstart agent")
    sub5.add_parser("status", help="Print agent status")
    sub5.add_parser("uninstall", help="Unload agent and remove plist")

    sub.add_parser("menubar", help="Run menu bar UI (requires rumps)")
    sub.add_parser("quit", help="Stop menubar and capture (disable autostart)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd == "capture":
        run_capture_once(force=args.force)
        return 0
    if args.cmd == "summarize":
        summarize_day_to_markdown(args.date)
        return 0
    if args.cmd == "daily-run":
        run_daily_automation()
        return 0
    if args.cmd == "enrich":
        enrich_day_with_llm(args.date, model=args.model, max_segments=args.max_segments)
        return 0
    if args.cmd == "launchd":
        if args.launchd_target == "capture":
            if args.launchd_cmd == "install":
                launchd_capture_install()
            elif args.launchd_cmd == "start":
                launchd_capture_start()
            elif args.launchd_cmd == "stop":
                launchd_capture_stop()
            elif args.launchd_cmd == "restart":
                launchd_capture_restart()
            elif args.launchd_cmd == "status":
                launchd_capture_status()
            elif args.launchd_cmd == "uninstall":
                launchd_capture_uninstall()
        elif args.launchd_target == "menubar":
            if args.launchd_cmd2 == "install":
                launchd_menubar_install()
            elif args.launchd_cmd2 == "start":
                launchd_menubar_start()
            elif args.launchd_cmd2 == "stop":
                launchd_menubar_stop()
            elif args.launchd_cmd2 == "restart":
                launchd_menubar_restart()
            elif args.launchd_cmd2 == "status":
                launchd_menubar_status()
            elif args.launchd_cmd2 == "uninstall":
                launchd_menubar_uninstall()
            elif args.launchd_cmd2 == "quit":
                launchd_menubar_stop_for_quit()
        elif args.launchd_target == "daily":
            if args.launchd_cmd3 == "install":
                launchd_daily_install()
            elif args.launchd_cmd3 == "start":
                launchd_daily_start()
            elif args.launchd_cmd3 == "stop":
                launchd_daily_stop()
            elif args.launchd_cmd3 == "restart":
                launchd_daily_restart()
            elif args.launchd_cmd3 == "status":
                launchd_daily_status()
            elif args.launchd_cmd3 == "uninstall":
                launchd_daily_uninstall()
        return 0
    if args.cmd == "menubar":
        run_menubar()
        return 0
    if args.cmd == "quit":
        launchd_capture_uninstall()
        launchd_menubar_uninstall()
        return 0
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
