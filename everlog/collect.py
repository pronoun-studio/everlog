# Role: 前面アプリ/ウィンドウタイトル/（Chromeなら）アクティブタブURLを取得して「作業コンテキスト」を組み立てる。
# How: System Events / Google Chrome のAppleScriptを `everlog/apple.py` 経由で実行し、URLはドメインに正規化して返す。
# Key functions: `collect_active_context()`, `_get_frontmost_app()`, `_get_front_window_title()`, `_get_chrome_active_url_and_title()`
# Collaboration: `everlog/capture.py` がこの結果を使って除外判定・JSONL記録・日次サマリの材料にする。設定（Chromeのみ取得）は `everlog/config.py` を参照する。
from __future__ import annotations

import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from .apple import run_osascript
from .config import AppConfig


@dataclass(frozen=True)
class BrowserInfo:
    name: str
    url: str
    domain: str


@dataclass(frozen=True)
class ActiveContext:
    active_app: str
    window_title: str
    browser: BrowserInfo | None
    # osascript呼び出しで発生したエラー（権限不足など）
    osascript_error: str | None = None


def _get_frontmost_app() -> str:
    return run_osascript(
        'tell application "System Events" to name of (first application process whose frontmost is true)'
    )


def _get_front_window_title(app_name: str) -> str:
    # Accessibility権限が必要なことがある
    script = f'''
tell application "System Events"
  set frontApp to first application process whose frontmost is true
  set appName to name of frontApp
  if appName is "{app_name}" then
    try
      return name of front window of frontApp
    on error
      return ""
    end try
  else
    return ""
  end if
end tell
'''
    return run_osascript(script)


def _get_chrome_active_url_and_title() -> tuple[str, str]:
    script = r'''
tell application "Google Chrome"
  if (count of windows) = 0 then
    return ""
  end if
  set theUrl to URL of active tab of front window
  set theTitle to title of active tab of front window
  return theUrl & "\n" & theTitle
end tell
'''
    out = run_osascript(script)
    if not out:
        return "", ""
    parts = out.split("\n", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def collect_active_context(cfg: AppConfig) -> ActiveContext:
    """
    前面アプリ/ウィンドウタイトル/ブラウザ情報を収集する。

    System Events へのApple Events送信権限がない場合（-1743エラー）は、
    例外を投げずに空の情報で続行し、エラー内容を osascript_error に格納する。
    これによりキャプチャ処理全体が落ちることを防ぎ、ログには記録される。
    """
    osascript_error: str | None = None

    try:
        app = _get_frontmost_app()
    except RuntimeError as e:
        # System Events権限エラー (-1743) などをキャッチ
        osascript_error = str(e)
        print(f"[collect] osascript error (frontmost app): {e}", file=sys.stderr)
        app = "(unknown)"

    browser: BrowserInfo | None = None
    window_title = ""

    if app != "(unknown)":
        try:
            window_title = _get_front_window_title(app)
        except RuntimeError as e:
            if osascript_error is None:
                osascript_error = str(e)
            print(f"[collect] osascript error (window title): {e}", file=sys.stderr)

        if cfg.browser.lower() == "chrome" and app == "Google Chrome":
            try:
                url, title = _get_chrome_active_url_and_title()
                if title:
                    window_title = title
                if url:
                    domain = urlparse(url).netloc or ""
                    browser = BrowserInfo(name="Chrome", url=url, domain=domain)
            except RuntimeError as e:
                if osascript_error is None:
                    osascript_error = str(e)
                print(f"[collect] osascript error (chrome): {e}", file=sys.stderr)

    return ActiveContext(
        active_app=app,
        window_title=window_title or "",
        browser=browser,
        osascript_error=osascript_error,
    )
