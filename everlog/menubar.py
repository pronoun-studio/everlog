# Role: メニューバー常駐UI（rumps）で、収集の開始/停止・設定変更・手動実行・日次生成・状態表示を提供する。
# How: rumpsのメニュー項目から「同じPython実行環境でCLIをサブプロセス実行」して処理を統一し、当日のJSONLを読んで回数/最終時刻を表示する。
# Key functions: `run_menubar()`（UI本体）
# Collaboration: launchd操作は `everlog/launchd.py`（を呼ぶCLI）に委譲し、設定は `everlog/config.py` を更新する。状態表示と日次生成は `everlog/jsonl.py` / `everlog/summarize.py` の成果物を参照する。
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .config import load_config, save_config
from .jsonl import read_jsonl
from .launchd import capture_program_args
from .llm import _load_dotenv_if_needed
from .paths import get_paths
from .timeutil import make_run_id


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
                (600, "10分"),
                (1800, "30分"),
            ]:
                item = rumps.MenuItem(f"間隔: {label}", callback=lambda _, s=sec: self._set_interval(s))
                self.interval_items[sec] = item

            self.menu = [
                self.count_item,
                self.last_item,
                self.status_item,
                None,
                self.autostart_item,
                rumps.MenuItem("everlogを再起動", callback=self.on_install_reload),
                self.start_item,
                self.stop_item,
                rumps.MenuItem("再起動（Restart）", callback=self.on_restart),
                None,
                self.interval_items[10],
                self.interval_items[60],
                self.interval_items[300],
                self.interval_items[600],
                self.interval_items[1800],
                rumps.MenuItem("除外設定を開く", callback=self.on_open_exclusions),
                None,
                rumps.MenuItem("今すぐ1回キャプチャ", callback=self.on_capture_now),
                rumps.MenuItem("今日のマークダウン生成", callback=self.on_summarize_today),
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

        def on_install_reload(self, _):
            _run_cli(["launchd", "capture", "install"])
            self.on_tick(None)
            rumps.notification("everlog", "Install/Reload", "定期キャプチャを（再）インストールしました")

        def on_start(self, _):
            _run_cli(["launchd", "capture", "install"])
            self.on_tick(None)
            rumps.notification("everlog", "Start", "定期キャプチャを開始しました")

        def on_stop(self, _):
            _run_cli(["launchd", "capture", "stop"])
            self.on_tick(None)
            rumps.notification("everlog", "Stop", "定期キャプチャを停止しました")

        def on_restart(self, _):
            _run_cli(["launchd", "capture", "restart"])
            self.on_tick(None)
            rumps.notification("everlog", "Restart", "定期キャプチャを再起動しました")

        def _set_interval(self, sec: int):
            cfg = load_config()
            cfg.interval_sec = sec
            save_config(cfg)
            if _agent_running():
                _run_cli(["launchd", "capture", "install"])
            self._sync_interval_menu()
            if sec < 60:
                interval_str = f"{sec}秒"
            else:
                interval_str = f"{sec // 60}分"
            rumps.notification("everlog", "間隔変更", f"キャプチャ間隔を {interval_str} に変更しました")

        def _sync_interval_menu(self):
            cfg = load_config()
            current = cfg.interval_sec
            for sec, item in self.interval_items.items():
                item.state = 1 if sec == current else 0

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

        def on_summarize_today(self, _):
            rumps.notification("everlog", "生成開始", "今日のマークダウンを生成中...")
            trace_env = os.environ.copy()
            trace_env["EVERLOG_TRACE"] = "1"
            trace_env["EVERLOG_TRACE_RUN_ID"] = make_run_id()
            trace_env["EVERLOG_OUTPUT_RUN_ID"] = trace_env["EVERLOG_TRACE_RUN_ID"]
            # Hourly LLM is required for "complete" timeline output.
            trace_env["EVERLOG_HOURLY_LLM"] = "1"
            # Daily LLM summary (uses hour summaries, low incremental cost).
            trace_env["EVERLOG_DAILY_LLM"] = "1"
            try:
                run_id = trace_env["EVERLOG_TRACE_RUN_ID"]
                run_dir = get_paths().trace_dir / _today() / run_id
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
            # enrich (segment-llm) is optional and disabled by default.
            run_enrich = str(trace_env.get("EVERLOG_RUN_ENRICH") or "").strip() in {
                "1",
                "true",
                "TRUE",
                "yes",
                "YES",
            }
            enrich_failed = False
            if run_enrich:
                # enrichが失敗してもsummarizeは実行する（LLMなしでもMarkdownは生成可能）
                enrich_result = subprocess.run(
                    [sys.executable, "-m", "everlog.cli", "enrich", "--date", "today"],
                    env=trace_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                enrich_failed = enrich_result.returncode != 0
                if enrich_failed:
                    # LLM解析失敗時は警告を出すが、summarizeは続行
                    err_msg = enrich_result.stderr.strip().split("\n")[-1] if enrich_result.stderr else "不明なエラー"
                    rumps.notification("everlog", "LLM解析スキップ", f"LLM解析に失敗: {err_msg[:50]}")

            summarize_result = subprocess.run(
                [sys.executable, "-m", "everlog.cli", "summarize", "--date", "today"],
                env=trace_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            run_id = trace_env.get("EVERLOG_OUTPUT_RUN_ID") or trace_env.get("EVERLOG_TRACE_RUN_ID") or ""
            if run_id:
                out_dir = get_paths().out_dir / _today() / run_id
            else:
                out_dir = get_paths().out_dir
            out = _latest_md_in_dir(out_dir)
            if summarize_result.returncode == 0 and out and out.exists():
                if run_enrich and enrich_failed:
                    rumps.notification("everlog", "生成完了", "マークダウンを開きます（segment-llm失敗）")
                else:
                    rumps.notification("everlog", "生成完了", "マークダウンを開きます")
                _run(["open", str(out)])
            else:
                if summarize_result.returncode == 0:
                    err_msg = "マークダウンが見つかりません"
                else:
                    err_msg = summarize_result.stderr.strip().split("\n")[-1] if summarize_result.stderr else "不明なエラー"
                rumps.notification("everlog", "エラー", f"マークダウン生成失敗: {err_msg[:50]}")

        def on_quit(self, _):
            _run_cli(["launchd", "capture", "uninstall"])
            _run_cli(["launchd", "menubar", "uninstall"])
            rumps.quit_application()

    App().run()
