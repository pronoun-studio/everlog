# Role: launchd LaunchAgent（定期キャプチャ/メニューバー常駐）のplist生成と `launchctl` 操作を行う。
# How: 設定から間隔を読み取り、StartInterval（短命）/KeepAlive（常駐）それぞれのplistを書き出し、bootstrap/bootout/kickstart/print を呼ぶ。
# Key functions: `launchd_capture_install/start/stop/restart/status`, `launchd_menubar_install/start/stop/restart/status`
# Collaboration: capture側plistは `everlog/cli.py capture` を定期実行する。menubar側plistは `everlog/cli.py menubar` を常駐起動する。間隔は `everlog/config.py` を参照する。
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import load_config
from .paths import get_paths, _project_root


CAPTURE_LABEL = "com.everlog.capture"
MENUBAR_LABEL = "com.everlog.menubar"
DAILY_LABEL = "com.everlog.daily"

LEGACY_CAPTURE_LABEL = "com.everytimecapture.capture"
LEGACY_MENUBAR_LABEL = "com.everytimecapture.menubar"
LEGACY_DAILY_LABEL = "com.everytimecapture.daily"


def _uid() -> str:
    return str(os.getuid())


def _launchagents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str) -> Path:
    return _launchagents_dir() / f"{label}.plist"


def _python_executable() -> str:
    return sys.executable


def _env_dict_xml(*, include_pythonpath: bool = True) -> str:
    """launchd 実行に必要な環境変数をplistに埋め込む。"""
    # ログ保存先を固定（CWDや配置に依存しないようにする）
    log_home = str(get_paths().home)

    log_home_xml = (
        f"\n    <key>EVERLOG_LOG_HOME</key>\n    <string>{log_home}</string>"
        f"\n    <key>EVERYTIMECAPTURE_LOG_HOME</key>\n    <string>{log_home}</string>"
    )
    pythonpath_xml = ""
    if include_pythonpath:
        # プロジェクトルートを PYTHONPATH に追加（launchd 経由で everlog モジュールを見つけるため）
        # .app 配下でも pyproject/.git を辿れるように _project_root を使う
        project_root = _project_root()
        pythonpath_xml = f"\n    <key>PYTHONPATH</key>\n    <string>{project_root}</string>"
    return f"""  <key>EnvironmentVariables</key>
  <dict>
    <key>LANG</key>
    <string>ja_JP.UTF-8</string>
    <key>PYTHONIOENCODING</key>
    <string>utf-8</string>
    <key>PYTHONNOUSERSITE</key>
    <string>1</string>{pythonpath_xml}
{log_home_xml}
  </dict>"""


def _bundle_executable(app_path: str) -> str | None:
    """
    Resolve a .app bundle to its executable path.
    Falls back to Contents/MacOS/applet when plist lookup fails (common for osacompile applets).
    """
    try:
        app = Path(app_path).expanduser()
        info = app / "Contents" / "Info.plist"
        if info.exists():
            data = plistlib.loads(info.read_bytes())
            exe = data.get("CFBundleExecutable")
            if isinstance(exe, str) and exe:
                candidate = app / "Contents" / "MacOS" / exe
                if candidate.exists():
                    return str(candidate)
        fallback = app / "Contents" / "MacOS" / "applet"
        if fallback.exists():
            return str(fallback)
    except Exception:
        return None
    return None


def _capture_program_args(cfg) -> list[str]:
    """
    Prefer a built macOS app (py2app) when configured.
    This avoids TCC/Screen Recording issues where adding a raw Python binary isn't possible in System Settings.
    """
    app_path = (
        os.environ.get("EVERLOG_CAPTURE_APP")
        or os.environ.get("EVERYTIMECAPTURE_CAPTURE_APP")
        or getattr(cfg, "capture_app_path", None)
    )
    if app_path:
        exe = _bundle_executable(str(app_path))
        if exe:
            # Run the bundle executable directly (works even when `open -a` fails in some environments).
            return [exe, "capture"]
        return ["/usr/bin/open", "-gj", "-a", str(app_path), "--args", "capture"]
    return [_python_executable(), "-m", "everlog.cli", "capture"]


def capture_program_args(cfg) -> list[str]:
    """Public wrapper for manual capture (menubar) and launchd."""
    return _capture_program_args(cfg)


def _write_plist_capture(interval_sec: int) -> None:
    agents = _launchagents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    plist = _plist_path(CAPTURE_LABEL)
    cfg = load_config()
    args = _capture_program_args(cfg)
    args_xml = "\n".join([f"      <string>{a}</string>" for a in args])
    using_app = bool(getattr(cfg, "capture_app_path", None))
    env_xml = _env_dict_xml(include_pythonpath=not using_app)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{CAPTURE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
{env_xml}
  <key>StartInterval</key>
  <integer>{int(interval_sec)}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{Path.home()}/.everlog/capture.out.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/.everlog/capture.err.log</string>
</dict>
</plist>
"""
    plist.write_text(content, encoding="utf-8")


def _write_plist_daily() -> None:
    """毎日23:55に summarize を実行するplistを生成（enrichは任意）"""
    agents = _launchagents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    plist = _plist_path(DAILY_LABEL)
    cfg = load_config()
    python = _python_executable()
    # Default: summarize only. To include enrich, set EVERLOG_RUN_ENRICH=1 and customize your schedule.
    # EVERLOG_HOURLY_LLM と EVERLOG_DAILY_LLM を有効化してフル機能のMarkdown生成を行う
    script = f'EVERLOG_HOURLY_LLM=1 EVERLOG_DAILY_LLM=1 {python} -m everlog.cli summarize --date today'
    env_xml = _env_dict_xml()
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{DAILY_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>{script}</string>
  </array>
{env_xml}
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>23</integer>
    <key>Minute</key>
    <integer>55</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{Path.home()}/.everlog/daily.out.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/.everlog/daily.err.log</string>
</dict>
</plist>
"""
    plist.write_text(content, encoding="utf-8")


def _write_plist_menubar(keep_alive: bool = True) -> None:
    agents = _launchagents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    plist = _plist_path(MENUBAR_LABEL)
    cfg = load_config()
    args = [_python_executable(), "-m", "everlog.cli", "menubar"]
    args_xml = "\n".join([f"      <string>{a}</string>" for a in args])
    env_xml = _env_dict_xml()
    keep_alive_xml = "  <key>KeepAlive</key>\n  <true/>" if keep_alive else ""
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{MENUBAR_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
{env_xml}
  <key>RunAtLoad</key>
  <true/>
{keep_alive_xml}
  <key>StandardOutPath</key>
  <string>{Path.home()}/.everlog/menubar.out.log</string>
  <key>StandardErrorPath</key>
  <string>{Path.home()}/.everlog/menubar.err.log</string>
</dict>
</plist>
"""
    plist.write_text(content, encoding="utf-8")


def _run(cmd: list[str], quiet: bool = False) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, check=False, capture_output=quiet)


def _start(label: str) -> subprocess.CompletedProcess[bytes]:
    return _run(["/bin/launchctl", "bootstrap", f"gui/{_uid()}", str(_plist_path(label))])


def _stop(label: str, quiet: bool = False) -> subprocess.CompletedProcess[bytes]:
    # bootout は gui/{uid}/{label} 形式で指定する必要がある
    return _run(["/bin/launchctl", "bootout", f"gui/{_uid()}/{label}"], quiet=quiet)


def _restart(label: str) -> subprocess.CompletedProcess[bytes]:
    return _run(["/bin/launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"])


def _status(label: str) -> subprocess.CompletedProcess[bytes]:
    return _run(["/bin/launchctl", "print", f"gui/{_uid()}/{label}"])


def _uninstall(label: str) -> None:
    _stop(label, quiet=True)
    try:
        _plist_path(label).unlink(missing_ok=True)
    except Exception:
        pass


def _legacy_labels_for(label: str) -> list[str]:
    if label == CAPTURE_LABEL:
        return [CAPTURE_LABEL, LEGACY_CAPTURE_LABEL]
    if label == MENUBAR_LABEL:
        return [MENUBAR_LABEL, LEGACY_MENUBAR_LABEL]
    if label == DAILY_LABEL:
        return [DAILY_LABEL, LEGACY_DAILY_LABEL]
    return [label]


def _stop_all(label: str) -> None:
    for lab in _legacy_labels_for(label):
        _stop(lab, quiet=True)


def _restart_any(label: str) -> None:
    labels = _legacy_labels_for(label)
    p = _restart(labels[0])
    if p.returncode != 0 and len(labels) > 1:
        _restart(labels[1])


def _status_any(label: str) -> None:
    labels = _legacy_labels_for(label)
    p = _status(labels[0])
    if p.returncode != 0 and len(labels) > 1:
        _status(labels[1])


def launchd_capture_install() -> None:
    cfg = load_config()
    _write_plist_capture(cfg.interval_sec)
    _stop_all(CAPTURE_LABEL)  # 既に停止済みでもエラーを表示しない
    launchd_capture_start()


def launchd_capture_start() -> None:
    _start(CAPTURE_LABEL)


def launchd_capture_stop() -> None:
    _stop_all(CAPTURE_LABEL)


def launchd_capture_restart() -> None:
    _restart_any(CAPTURE_LABEL)


def launchd_capture_status() -> None:
    _status_any(CAPTURE_LABEL)

def launchd_capture_uninstall() -> None:
    _uninstall(CAPTURE_LABEL)
    _uninstall(LEGACY_CAPTURE_LABEL)


def launchd_menubar_install() -> None:
    _write_plist_menubar()
    _stop_all(MENUBAR_LABEL)  # 既に停止済みでもエラーを表示しない
    launchd_menubar_start()


def launchd_menubar_start() -> None:
    _start(MENUBAR_LABEL)


def launchd_menubar_stop() -> None:
    _stop_all(MENUBAR_LABEL)


def launchd_menubar_stop_for_quit() -> None:
    """終了用: メニューバーを停止し、自動起動を無効化する。"""
    launchd_menubar_uninstall()


def launchd_menubar_restart() -> None:
    _restart_any(MENUBAR_LABEL)


def launchd_menubar_status() -> None:
    _status_any(MENUBAR_LABEL)


def launchd_menubar_uninstall() -> None:
    _uninstall(MENUBAR_LABEL)
    _uninstall(LEGACY_MENUBAR_LABEL)


# --- Daily (enrich + summarize at 23:55) ---

def launchd_daily_install() -> None:
    _write_plist_daily()
    _stop_all(DAILY_LABEL)
    launchd_daily_start()


def launchd_daily_start() -> None:
    _start(DAILY_LABEL)


def launchd_daily_stop() -> None:
    _stop_all(DAILY_LABEL)


def launchd_daily_restart() -> None:
    _restart_any(DAILY_LABEL)


def launchd_daily_status() -> None:
    _status_any(DAILY_LABEL)


def launchd_daily_uninstall() -> None:
    _uninstall(DAILY_LABEL)
    _uninstall(LEGACY_DAILY_LABEL)
