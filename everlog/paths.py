# Role: ログ保存先（logs/out/tmp/bin/config）を一箇所で定義し、必要なディレクトリを作る。
# How: 実行時にhomeディレクトリを基準にパスを組み立て、初回実行でも破綻しないように `mkdir(exist_ok=True)` で作成する。
# Key functions: `get_paths()`, `ensure_dirs()`, `AppPaths`
# Collaboration: capture/summarize/ocr/menubar/config などが同じ保存先規約を使うために参照する（パスの分散を防ぐ）。
# Note: 互換性のため、旧ディレクトリ `EVERYTIME-LOG/` も自動検出/利用する。
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    home: Path
    logs_dir: Path
    out_dir: Path
    tmp_dir: Path
    bin_dir: Path
    config_path: Path


def _find_project_log_dir() -> Path | None:
    """開発時にプロジェクト直下のログディレクトリを探す。"""
    # まずは実行時のカレントディレクトリ直下を優先（最も分かりやすい）
    for dirname in ("EVERLOG-LOG", "EVERYTIME-LOG"):
        cwd_candidate = Path.cwd() / dirname
        if cwd_candidate.is_dir():
            return cwd_candidate
    # このファイルの位置から上位に遡って探索（app bundle内でも見つけるため）
    for parent in Path(__file__).resolve().parents:
        for dirname in ("EVERLOG-LOG", "EVERYTIME-LOG"):
            candidate = parent / dirname
            if candidate.is_dir():
                return candidate
    return None


def _find_known_dev_log_dir() -> Path | None:
    """既知の開発用ディレクトリ配下からログディレクトリを探す。"""
    home = Path.home()
    for base in (home / "DEV", home / "dev"):
        for repo in ("everytimecapture", "everlog"):
            for dirname in ("EVERLOG-LOG", "EVERYTIME-LOG"):
                candidate = base / repo / dirname
                if candidate.is_dir():
                    return candidate
    return None


def get_paths() -> AppPaths:
    # 優先順位: 1) プロジェクト直下 2) 既知の開発パス
    project_log = _find_project_log_dir()
    if project_log:
        home = project_log
    else:
        dev_log = _find_known_dev_log_dir()
        if dev_log:
            home = dev_log
        else:
            raise RuntimeError(
                "ログ保存先が見つかりません。"
                "プロジェクト直下に `EVERYTIME-LOG/`（または `EVERLOG-LOG/`）を作成するか、"
                "リポジトリ内から実行してください。"
            )
    return AppPaths(
        home=home,
        logs_dir=home / "logs",
        out_dir=home / "out",
        tmp_dir=home / "tmp",
        bin_dir=home / "bin",
        config_path=home / "config.json",
    )


def ensure_dirs() -> AppPaths:
    paths = get_paths()
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.out_dir.mkdir(parents=True, exist_ok=True)
    paths.tmp_dir.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    return paths
