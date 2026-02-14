# Role: メニューバー常駐UI（rumps）で、収集の開始/停止・設定変更・手動実行・日次生成・状態表示を提供する。
# How: rumpsのメニュー項目から「同じPython実行環境でCLIをサブプロセス実行」して処理を統一し、当日のJSONLを読んで回数/最終時刻を表示する。
# Key functions: `run_menubar()`（UI本体）
# Collaboration: launchd操作は `everlog/launchd.py`（を呼ぶCLI）に委譲し、設定は `everlog/config.py` を更新する。状態表示と日次生成は `everlog/jsonl.py` / `everlog/summarize.py` の成果物を参照する。
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .config import load_config, save_config
from .jsonl import read_jsonl
from .launchd import capture_program_args
from .llm import _load_dotenv_if_needed
from .paths import get_paths
from .timeutil import make_run_id


class ProgressPanel:
    """macOS native progress panel using PyObjC."""

    def __init__(self, title: str = "処理中..."):
        try:
            from AppKit import (
                NSPanel,
                NSMakeRect,
                NSProgressIndicator,
                NSTextField,
                NSFont,
                NSColor,
                NSWindowStyleMaskTitled,
                NSWindowStyleMaskClosable,
                NSBackingStoreBuffered,
                NSProgressIndicatorBarStyle,
            )
            from PyObjCTools import AppHelper

            self._AppHelper = AppHelper

            # Create panel
            panel_width = 360
            panel_height = 100
            self._panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, panel_width, panel_height),
                NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
                NSBackingStoreBuffered,
                False,
            )
            self._panel.setTitle_(title)
            self._panel.setLevel_(3)  # Floating level
            self._panel.center()

            content = self._panel.contentView()

            # Progress bar
            self._progress = NSProgressIndicator.alloc().initWithFrame_(
                NSMakeRect(20, 40, panel_width - 40, 20)
            )
            self._progress.setStyle_(NSProgressIndicatorBarStyle)
            self._progress.setMinValue_(0)
            self._progress.setMaxValue_(100)
            self._progress.setDoubleValue_(0)
            self._progress.setIndeterminate_(False)
            content.addSubview_(self._progress)

            # Status label
            self._label = NSTextField.alloc().initWithFrame_(
                NSMakeRect(20, 65, panel_width - 40, 20)
            )
            self._label.setStringValue_("準備中...")
            self._label.setBezeled_(False)
            self._label.setDrawsBackground_(False)
            self._label.setEditable_(False)
            self._label.setSelectable_(False)
            self._label.setFont_(NSFont.systemFontOfSize_(13))
            content.addSubview_(self._label)

            # Percent label
            self._percent_label = NSTextField.alloc().initWithFrame_(
                NSMakeRect(20, 15, panel_width - 40, 20)
            )
            self._percent_label.setStringValue_("0%")
            self._percent_label.setBezeled_(False)
            self._percent_label.setDrawsBackground_(False)
            self._percent_label.setEditable_(False)
            self._percent_label.setSelectable_(False)
            self._percent_label.setFont_(NSFont.monospacedDigitSystemFontOfSize_weight_(12, 0.5))
            self._percent_label.setTextColor_(NSColor.secondaryLabelColor())
            content.addSubview_(self._percent_label)

            self._available = True
        except ImportError:
            self._available = False
            self._panel = None

    def show(self):
        """Show the progress panel."""
        if self._available and self._panel:
            self._panel.makeKeyAndOrderFront_(None)

    def hide(self):
        """Hide the progress panel."""
        if self._available and self._panel:
            self._panel.orderOut_(None)

    def update(self, percent: int, stage: str):
        """Update progress display."""
        if not self._available:
            return

        def do_update():
            if self._progress:
                self._progress.setDoubleValue_(float(percent))
            if self._label:
                self._label.setStringValue_(stage)
            if self._percent_label:
                self._percent_label.setStringValue_(f"{percent}%")

        # Schedule on main thread
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(do_update)
        except Exception:
            do_update()

    @property
    def available(self) -> bool:
        return self._available


def _require_rumps():
    try:
        import rumps  # type: ignore

        return rumps
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "menubar を使うには rumps が必要です。`pip install -r requirements.txt` を実行してください。"
        ) from e


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _today_log_path() -> Path:
    return get_paths().logs_dir / f"{_today()}.jsonl"


def _format_last_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except Exception:
        return "-"
    hour = dt.strftime("%H").lstrip("0") or "0"
    return f"{dt.strftime('%y/%m/%d')} {hour}:{dt.strftime('%M')}"


def _capture_stats() -> tuple[int, str]:
    events = read_jsonl(_today_log_path())
    if not events:
        return 0, "-"
    last = events[-1].get("ts") or "-"
    return len(events), _format_last_ts(str(last))


def _latest_md_in_dir(dir_path: Path) -> Path | None:
    try:
        items = [p for p in dir_path.glob("*.md") if p.is_file()]
    except Exception:
        return None
    if not items:
        return None
    return max(items, key=lambda p: p.stat().st_mtime)


def _run(cmd: list[str], *, env_override: dict[str, str] | None = None) -> None:
    # Always load .env on menubar actions to keep env consistent.
    _load_dotenv_if_needed()
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    subprocess.run(cmd, check=False, env=env)


def _run_cli(args: list[str]) -> None:
    _run([sys.executable, "-m", "everlog.cli", *args])


def _agent_running() -> bool:
    uid = str(os.getuid())
    labels = ["com.everlog.capture", "com.everytimecapture.capture"]
    for label in labels:
        proc = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{uid}/{label}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True
    return False


def _current_app_bundle() -> Path | None:
    try:
        exe = Path(sys.executable).resolve()
    except Exception:
        exe = Path(sys.executable)
    for parent in [exe, *exe.parents]:
        if parent.suffix == ".app":
            return parent
    return None


def _ensure_capture_running() -> None:
    cfg = load_config()
    app_bundle = _current_app_bundle()
    if app_bundle:
        bundle_path = str(app_bundle)
        if not cfg.capture_app_path or not Path(cfg.capture_app_path).exists():
            cfg.capture_app_path = bundle_path
            save_config(cfg)
        elif cfg.capture_app_path != bundle_path and Path(bundle_path).exists():
            cfg.capture_app_path = bundle_path
            save_config(cfg)
    if _agent_running():
        return
    _run_cli(["launchd", "capture", "install"])


def _menubar_plist_paths() -> list[Path]:
    base = Path.home() / "Library" / "LaunchAgents"
    return [
        base / "com.everlog.menubar.plist",
        base / "com.everytimecapture.menubar.plist",
    ]


def _autostart_enabled() -> bool:
    return any(p.exists() for p in _menubar_plist_paths())


def _launched_by_launchd_menubar() -> bool:
    return os.environ.get("XPC_SERVICE_NAME") in {"com.everlog.menubar", "com.everytimecapture.menubar"}


def run_menubar() -> None:
    # Ensure .env is loaded at startup.
    _load_dotenv_if_needed()
    rumps = _require_rumps()
    log_home = get_paths().home
    pid = os.getpid()
    debug_path = log_home / "menubar.capture.log"

    def _debug(msg: str) -> None:
        ts = datetime.now().astimezone().isoformat()
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            with debug_path.open("a", encoding="utf-8", errors="ignore") as f:
                f.write(f"{ts} pid={pid} {msg}\n")
        except Exception:
            pass

    class App(rumps.App):
        def __init__(self):
            super().__init__("everlog", quit_button=None)
            self.count_item = rumps.MenuItem("今日のキャプチャ回数: -")
            self.last_item = rumps.MenuItem("前回キャプチャ時間: -")
            self.status_item = rumps.MenuItem("定期キャプチャ: 確認中…")
            self.count_item.set_callback(None)
            self.last_item.set_callback(None)
            self.status_item.set_callback(None)

            self.start_item = rumps.MenuItem("●定期キャプチャの開始", callback=self.on_start)
            self.stop_item = rumps.MenuItem("○定期キャプチャの停止", callback=self.on_stop)
            self.autostart_item = rumps.MenuItem("自動起動: 確認中…", callback=self.on_toggle_autostart)

            self.interval_items: dict[int, rumps.MenuItem] = {}
            for sec, label in [
                (10, "10秒（デバッグ用）"),
                (60, "1分"),
                (300, "5分（デフォルト）"),
            ]:
                item = rumps.MenuItem(f"間隔: {label}", callback=lambda _, s=sec: self._set_interval(s))
                self.interval_items[sec] = item
            self.custom_interval_item = rumps.MenuItem(
                "間隔: キャプチャ間隔を指定",
                callback=self.on_set_custom_interval,
            )

            self.menu = [
                self.count_item,
                self.last_item,
                self.status_item,
                None,
                self.autostart_item,
                self.start_item,
                self.stop_item,
                None,
                self.interval_items[10],
                self.interval_items[60],
                self.interval_items[300],
                self.custom_interval_item,
                rumps.MenuItem("除外設定を開く", callback=self.on_open_exclusions),
                None,
                rumps.MenuItem("今すぐ1回キャプチャ", callback=self.on_capture_now),
                rumps.MenuItem("今日のマークダウン生成", callback=self.on_summarize_today),
                rumps.MenuItem("日付を指定してマークダウン生成", callback=self.on_summarize_date),
                None,
                rumps.MenuItem("終了", callback=self.on_quit),
            ]

            self.timer = rumps.Timer(self.on_tick, 5)
            self.timer.start()
            _ensure_capture_running()
            self.on_tick(None)

        def on_tick(self, _):
            c, last = _capture_stats()
            self.count_item.title = f"今日のキャプチャ回数: {c}回"
            self.last_item.title = f"前回キャプチャ時間: {last}"
            running = _agent_running()
            if running:
                self.title = "everlog ●"
                self.status_item.title = "定期キャプチャ: ●動作中"
            else:
                self.title = "everlog ○"
                self.status_item.title = "定期キャプチャ: ○停止中"
            self._sync_interval_menu()
            self._sync_autostart_menu()

        def on_start(self, _):
            _run_cli(["launchd", "capture", "install"])
            self.on_tick(None)
            rumps.notification("everlog", "Start", "定期キャプチャを開始しました")

        def on_stop(self, _):
            _run_cli(["launchd", "capture", "stop"])
            self.on_tick(None)
            rumps.notification("everlog", "Stop", "定期キャプチャを停止しました")

        def _set_interval(self, sec: int):
            cfg = load_config()
            cfg.interval_sec = sec
            save_config(cfg)
            if _agent_running():
                _run_cli(["launchd", "capture", "install"])
            self._sync_interval_menu()
            interval_str = self._format_interval(sec)
            rumps.notification("everlog", "間隔変更", f"キャプチャ間隔を {interval_str} に変更しました")

        def _sync_interval_menu(self):
            cfg = load_config()
            current = cfg.interval_sec
            for sec, item in self.interval_items.items():
                item.state = 1 if sec == current else 0
            self.custom_interval_item.state = 1 if current not in self.interval_items else 0
            self.custom_interval_item.title = (
                "間隔: キャプチャ間隔を指定"
                if current in self.interval_items
                else f"間隔: キャプチャ間隔を指定（現在 {self._format_interval(current)}）"
            )

        def _format_interval(self, sec: int) -> str:
            if sec < 60:
                return f"{sec}秒"
            minute, second = divmod(sec, 60)
            if second == 0:
                return f"{minute}分"
            return f"{minute}分{second}秒"

        def _prompt_int(self, title: str, message: str, default_value: int) -> int | None:
            win = rumps.Window(
                message=message,
                title=title,
                default_text=str(default_value),
                ok="決定",
                cancel="キャンセル",
                dimensions=(220, 24),
            )
            res = win.run()
            if not res.clicked:
                return None
            value = res.text.strip()
            if not value.isdigit():
                rumps.alert("入力エラー", "0以上の整数を入力してください。")
                return None
            return int(value)

        def _prompt_interval_single_dialog(self, current_sec: int) -> tuple[bool, int | None]:
            """
            Show a native macOS dialog with minute/second fields.
            Returns (shown, value):
              - shown=False: native dialog unavailable, caller may fallback.
              - shown=True and value=None: cancelled or invalid input.
              - shown=True and value=int: valid interval in seconds.
            """
            try:
                from AppKit import NSAlert, NSAlertFirstButtonReturn, NSMakeRect, NSView, NSTextField
            except Exception:
                return (False, None)

            minute_default, second_default = divmod(max(0, int(current_sec)), 60)

            def _make_label(text: str, x: float, y: float, w: float, h: float):
                label = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
                label.setStringValue_(text)
                label.setBezeled_(False)
                label.setDrawsBackground_(False)
                label.setEditable_(False)
                label.setSelectable_(False)
                return label

            alert = NSAlert.alloc().init()
            alert.setMessageText_("キャプチャ間隔を指定")
            alert.setInformativeText_("分・秒を入力してください")
            alert.addButtonWithTitle_("決定")
            alert.addButtonWithTitle_("キャンセル")

            accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 280, 56))
            minute_label = _make_label("分", 0, 30, 24, 22)
            minute_field = NSTextField.alloc().initWithFrame_(NSMakeRect(26, 28, 80, 24))
            minute_field.setStringValue_(str(minute_default))
            second_label = _make_label("秒", 126, 30, 24, 22)
            second_field = NSTextField.alloc().initWithFrame_(NSMakeRect(152, 28, 80, 24))
            second_field.setStringValue_(str(second_default))

            accessory.addSubview_(minute_label)
            accessory.addSubview_(minute_field)
            accessory.addSubview_(second_label)
            accessory.addSubview_(second_field)
            alert.setAccessoryView_(accessory)

            response = alert.runModal()
            if response != NSAlertFirstButtonReturn:
                return (True, None)

            minute_text = str(minute_field.stringValue()).strip()
            second_text = str(second_field.stringValue()).strip()
            if not minute_text.isdigit() or not second_text.isdigit():
                rumps.alert("入力エラー", "分・秒ともに0以上の整数を入力してください。")
                return (True, None)

            minute = int(minute_text)
            second = int(second_text)
            if second > 59:
                rumps.alert("入力エラー", "秒は0〜59で入力してください。")
                return (True, None)

            total_sec = minute * 60 + second
            if total_sec <= 0:
                rumps.alert("入力エラー", "1秒以上を指定してください。")
                return (True, None)
            return (True, total_sec)

        def on_set_custom_interval(self, _):
            cfg = load_config()
            shown, total_sec = self._prompt_interval_single_dialog(cfg.interval_sec)
            if shown:
                if total_sec is None:
                    return
                self._set_interval(total_sec)
                return

            # Fallback when native dialog is unavailable.
            cur_min, cur_sec = divmod(max(0, int(cfg.interval_sec)), 60)
            minute = self._prompt_int("キャプチャ間隔を指定", "分を入力してください（0以上の整数）", cur_min)
            if minute is None:
                return
            second = self._prompt_int("キャプチャ間隔を指定", "秒を入力してください（0〜59の整数）", cur_sec)
            if second is None:
                return
            if second > 59:
                rumps.alert("入力エラー", "秒は0〜59で入力してください。")
                return
            total_sec = minute * 60 + second
            if total_sec <= 0:
                rumps.alert("入力エラー", "1秒以上を指定してください。")
                return
            self._set_interval(total_sec)

        def _sync_autostart_menu(self):
            enabled = _autostart_enabled()
            self.autostart_item.title = "自動起動: 有効" if enabled else "自動起動: 無効"

        def on_toggle_autostart(self, _):
            if _autostart_enabled():
                rumps.notification("everlog", "自動起動", "自動起動を無効化します")
                _run_cli(["launchd", "menubar", "uninstall"])
            else:
                rumps.notification("everlog", "自動起動", "自動起動を有効化します")
                _run_cli(["launchd", "menubar", "install"])
            self._sync_autostart_menu()

        def _exclusions_default_text(self, cfg) -> str:
            return "\n".join(
                [
                    "=== [apps] ===",
                    *cfg.exclude.apps,
                    "",
                    "=== [domain_keywords] ===",
                    *cfg.exclude.domain_keywords,
                    "",
                    "=== [text_keywords] ===",
                    *cfg.exclude.text_keywords,
                ]
            )

        def _parse_exclusions_text(self, text: str):
            sections = {"apps": [], "domain_keywords": [], "text_keywords": []}
            current = None
            seen = set()
            for raw in text.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if "[" in line and "]" in line:
                    key = line[line.find("[") + 1 : line.find("]")].strip()
                    if key in sections:
                        current = key
                        seen.add(key)
                    else:
                        current = None
                    continue
                if current:
                    sections[current].append(line)
            if seen != set(sections.keys()):
                return None
            return sections

        def _edit_exclusions_single(self, cfg):
            win = rumps.Window(
                message="1画面で編集します。セクションは [apps] / [domain_keywords] / [text_keywords]。",
                title="除外設定",
                default_text=self._exclusions_default_text(cfg),
                ok="保存",
                cancel="キャンセル",
                dimensions=(520, 420),
            )
            res = win.run()
            if not res.clicked:
                return None
            parsed = self._parse_exclusions_text(res.text)
            if parsed is None:
                rumps.alert(
                    "除外設定の形式が不正です",
                    "セクション名は [apps] / [domain_keywords] / [text_keywords] の3つが必要です。",
                )
                return None
            return parsed

        def on_open_exclusions(self, _):
            cfg = load_config()
            parsed = self._edit_exclusions_single(cfg)
            if parsed is None:
                return
            cfg.exclude.apps = parsed["apps"]
            cfg.exclude.domain_keywords = parsed["domain_keywords"]
            cfg.exclude.text_keywords = parsed["text_keywords"]
            save_config(cfg)
            rumps.notification("everlog", "除外設定", "除外設定を更新しました")

        def on_capture_now(self, _):
            _debug("capture_now: clicked")
            rumps.notification("everlog", "キャプチャ", "開始します")
            try:
                cfg = load_config()
                args = capture_program_args(cfg)
                if "--force" not in args:
                    args = [*args, "--force"]
                _debug(f"capture_now: args={args}")
                _debug(f"capture_now: log_home={log_home}")
                proc = subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                _debug(f"capture_now: rc={proc.returncode}")
                if proc.stdout:
                    _debug(f"capture_now: stdout={proc.stdout.strip()[:400]}")
                if proc.stderr:
                    _debug(f"capture_now: stderr={proc.stderr.strip()[:400]}")
                if proc.returncode != 0:
                    msg = proc.stderr.strip().split("\n")[-1] if proc.stderr else f"exit {proc.returncode}"
                    rumps.notification("everlog", "キャプチャ失敗", msg[:80])
                else:
                    rumps.notification("everlog", "キャプチャ", "1回キャプチャしました")
            except Exception as e:
                _debug(f"capture_now: exception={e}")
                rumps.notification("everlog", "キャプチャ失敗", str(e)[:80])
            finally:
                self.on_tick(None)

        def _run_summarize(self, target_date: str, date_str: str):
            """Run summarize for the specified date with progress display.

            Args:
                target_date: Date string to pass to CLI (e.g. "today", "2026-02-09")
                date_str: Date in YYYY-MM-DD format for output directory
            """
            # 進捗パネルを作成
            panel = ProgressPanel(f"マークダウン生成中: {date_str}")
            panel.show()
            panel.update(0, "準備中...")

            def progress_callback(percent: int, stage: str):
                """Callback for progress updates from summarize."""
                panel.update(percent, stage)

            def run_summarize_thread():
                """Background thread to run summarize."""
                out_path = None
                error_msg = None
                try:
                    # Load .env for API keys
                    _load_dotenv_if_needed()

                    # Set up environment
                    run_id = make_run_id()
                    os.environ["EVERLOG_TRACE"] = "1"
                    os.environ["EVERLOG_TRACE_RUN_ID"] = run_id
                    os.environ["EVERLOG_OUTPUT_RUN_ID"] = run_id
                    os.environ["EVERLOG_HOURLY_LLM"] = "1"
                    os.environ["EVERLOG_DAILY_LLM"] = "1"

                    try:
                        run_dir = get_paths().trace_dir / date_str / run_id
                        run_dir.mkdir(parents=True, exist_ok=True)
                        (run_dir / "run.json").write_text(
                            json.dumps(
                                {
                                    "run_id": run_id,
                                    "source": "menubar",
                                    "started_at": datetime.now().astimezone().isoformat(),
                                },
                                ensure_ascii=False,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                    except Exception as e:
                        _debug(f"trace_run_dir: failed: {e}")

                    # Import and run summarize directly with progress callback
                    from .summarize import summarize_day_to_markdown

                    out_path = summarize_day_to_markdown(
                        target_date,
                        progress_callback=progress_callback,
                    )

                except Exception as e:
                    _debug(f"summarize_thread: exception={e}")
                    error_msg = str(e)

                # 完了処理をメインスレッドで
                def show_completion():
                    panel.hide()
                    if error_msg:
                        rumps.notification("everlog", "エラー", f"マークダウン生成失敗: {error_msg[:50]}")
                    elif out_path and out_path.exists():
                        rumps.notification("everlog", "生成完了", "マークダウンを開きます")
                        _run(["open", str(out_path)])
                    else:
                        rumps.notification("everlog", "エラー", "マークダウンが見つかりません")

                try:
                    from PyObjCTools import AppHelper
                    AppHelper.callAfter(show_completion)
                except Exception:
                    rumps.Timer(show_completion, 0).start()

            # バックグラウンドスレッドで実行
            thread = threading.Thread(target=run_summarize_thread, daemon=True)
            thread.start()

        def on_summarize_today(self, _):
            self._run_summarize("today", _today())

        def on_summarize_date(self, _):
            # Show date input dialog
            win = rumps.Window(
                message="日付を YYYY-MM-DD 形式で入力してください（例: 2026-02-09）",
                title="日付を指定してマークダウン生成",
                default_text=_today(),
                ok="生成",
                cancel="キャンセル",
                dimensions=(300, 24),
            )
            res = win.run()
            if not res.clicked:
                return
            date_input = res.text.strip()
            # Validate date format
            try:
                parsed_date = datetime.strptime(date_input, "%Y-%m-%d").date()
                date_str = parsed_date.isoformat()
            except ValueError:
                rumps.alert(
                    "日付形式が不正です",
                    "YYYY-MM-DD 形式で入力してください（例: 2026-02-09）",
                )
                return
            # Check if log file exists for the date
            log_path = get_paths().logs_dir / f"{date_str}.jsonl"
            if not log_path.exists():
                rumps.alert(
                    "ログが見つかりません",
                    f"{date_str} のログファイルが存在しません。",
                )
                return
            self._run_summarize(date_str, date_str)

        def on_quit(self, _):
            _run_cli(["launchd", "capture", "uninstall"])
            _run_cli(["launchd", "menubar", "uninstall"])
            rumps.quit_application()

    App().run()
