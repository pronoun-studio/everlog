# Role: ログ保存先（logs/out/tmp/bin/config）を一箇所で定義し、必要なディレクトリを作る。
# How: 実行時にhomeディレクトリを基準にパスを組み立て、初回実行でも破綻しないように `mkdir(exist_ok=True)` で作成する。
# Key functions: `get_paths()`, `ensure_dirs()`, `AppPaths`
# Collaboration: capture/summarize/ocr/menubar/config などが同じ保存先規約を使うために参照する（パスの分散を防ぐ）。
# Note: ログ保存先はプロジェクト直下の `EVERYTIME-LOG/` に固定する
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import shutil
import time
import os


@dataclass(frozen=True)
class AppPaths:
    home: Path
    logs_dir: Path
    out_dir: Path
    tmp_dir: Path
    bin_dir: Path
    trace_dir: Path
    config_path: Path


def _project_root_with_marker() -> tuple[Path, bool]:
    """
    プロジェクトルート（pyproject.toml または .git を含むディレクトリ）を推定する。
    見つからない場合は CWD を返す。
    """
    try:
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
                return parent, True
    except Exception:
        pass
    return Path.cwd(), False


def _project_root() -> Path:
    return _project_root_with_marker()[0]


def _log_home_pref_path() -> Path:
    return Path.home() / ".everlog" / "log_home.txt"


def _read_log_home_pref() -> Path | None:
    try:
        path = _log_home_pref_path()
        if not path.exists():
            return None
        val = path.read_text(encoding="utf-8").strip()
        if not val:
            return None
        pref = Path(val).expanduser()
        return pref if pref.exists() else None
    except Exception:
        return None


def _write_log_home_pref(home: Path) -> None:
    try:
        pref = _log_home_pref_path()
        pref.parent.mkdir(parents=True, exist_ok=True)
        pref.write_text(str(home), encoding="utf-8")
    except Exception:
        pass


def _log_home_override() -> Path | None:
    val = (
        os.environ.get("EVERLOG_LOG_HOME")
        or os.environ.get("EVERYTIMECAPTURE_LOG_HOME")
        or ""
    ).strip()
    if not val:
        return None
    return Path(val).expanduser()


_OUT_DAY_DIR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-\d+)?$")
_LAST_OUT_CLEANUP_AT = 0.0


def _out_retention_days() -> int:
    raw = (
        os.environ.get("EVERLOG_OUT_RETENTION_DAYS")
        or os.environ.get("EVERYTIMECAPTURE_OUT_RETENTION_DAYS")
        or ""
    ).strip()
    try:
        v = int(raw) if raw else 7
    except Exception:
        v = 7
    return max(1, v)


def _out_cleanup_interval_sec() -> int:
    raw = (
        os.environ.get("EVERLOG_OUT_CLEANUP_INTERVAL_SEC")
        or os.environ.get("EVERYTIMECAPTURE_OUT_CLEANUP_INTERVAL_SEC")
        or ""
    ).strip()
    try:
        v = int(raw) if raw else 3600
    except Exception:
        v = 3600
    return max(0, v)


def _cleanup_old_out_dirs(paths: AppPaths) -> None:
    """
    Remove dated output directories under out/ when they are older than retention days.
    Target names:
      - YYYY-MM-DD
      - YYYY-MM-DD-<n>
    """
    out_dir = paths.out_dir
    if not out_dir.exists():
        return
    retention_days = _out_retention_days()
    today = datetime.now().astimezone().date()
    for child in out_dir.iterdir():
        if not child.is_dir():
            continue
        m = _OUT_DAY_DIR_RE.match(child.name)
        if not m:
            continue
        try:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except Exception:
            continue
        age_days = (today - day).days
        if age_days >= retention_days:
            shutil.rmtree(child, ignore_errors=True)


def _maybe_cleanup_old_out_dirs(paths: AppPaths) -> None:
    global _LAST_OUT_CLEANUP_AT
    now = time.time()
    interval = _out_cleanup_interval_sec()
    if interval > 0 and (now - _LAST_OUT_CLEANUP_AT) < interval:
        return
    _LAST_OUT_CLEANUP_AT = now
    try:
        _cleanup_old_out_dirs(paths)
    except Exception:
        # Cleanup should never break regular capture/summarize flow.
        pass


def get_paths() -> AppPaths:
    override = _log_home_override()
    if override:
        home = override
        _write_log_home_pref(home)
        return AppPaths(
            home=home,
            logs_dir=home / "logs",
            out_dir=home / "out",
            tmp_dir=home / "tmp",
            bin_dir=home / "bin",
            trace_dir=home / "trace",
            config_path=home / "config.json",
        )

    pref = _read_log_home_pref()
    if pref:
        home = pref
        return AppPaths(
            home=home,
            logs_dir=home / "logs",
            out_dir=home / "out",
            tmp_dir=home / "tmp",
            bin_dir=home / "bin",
            trace_dir=home / "trace",
            config_path=home / "config.json",
        )

    # デフォルトは常にプロジェクト直下の `EVERYTIME-LOG/` に固定する
    root, found = _project_root_with_marker()
    home = root / "EVERYTIME-LOG"
    if found:
        _write_log_home_pref(home)
    return AppPaths(
        home=home,
        logs_dir=home / "logs",
        out_dir=home / "out",
        tmp_dir=home / "tmp",
        bin_dir=home / "bin",
        trace_dir=home / "trace",
        config_path=home / "config.json",
    )


def ensure_dirs() -> AppPaths:
    paths = get_paths()
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.trace_dir.mkdir(parents=True, exist_ok=True)
    _maybe_cleanup_old_out_dirs(paths)
    return paths
