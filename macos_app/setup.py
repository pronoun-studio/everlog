from __future__ import annotations

import sys
from pathlib import Path

from setuptools import setup


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# This app uses `rumps` for the menu bar UI and the project targets Python 3.10+.
# If you build with a different Python (e.g. `/usr/bin/python3`), py2app can succeed
# but the resulting `.app` will fail to launch at runtime.
if sys.version_info < (3, 10):
    raise SystemExit(
        "Python 3.10+ が必要です。`../.venv/bin/python setup.py py2app` でビルドしてください。"
    )
try:
    import rumps  # noqa: F401
except Exception as e:  # pragma: no cover
    raise SystemExit(
        "rumps が見つかりません。`../.venv/bin/python -m pip install -r ../requirements.txt` を実行してください。"
    ) from e

# Add the parent directory to sys.path so py2app can find packages
sys.path.insert(0, str(ROOT))

APP = [str(HERE / "EverlogApp.py")]

# NOTE: We keep package discovery explicit because this setup.py lives under macos_app/.
OPTIONS = {
    "argv_emulation": False,
    # Force-bundle the menu bar dependency (and friends) even if the import is indirect/optional.
    "packages": ["everlog", "rumps"],
    "includes": [
        "everlog.cli",
        "everlog.menubar",
        "everlog.capture",
        "everlog.config",
        "everlog.paths",
        "everlog.ocr",
        "everlog.llm",
        "everlog.jsonl",
        "everlog.timeutil",
        "everlog.exclusions",
        "everlog.redact",
        "everlog.apple",
        "everlog.collect",
        "everlog.enrich",
        "everlog.segments",
        "everlog.summarize",
        "everlog.launchd",
    ],
    "iconfile": "Everlog.icns",
    "plist": {
        "CFBundleName": "everlog",
        "CFBundleDisplayName": "everlog",
        "CFBundleIdentifier": "com.everlog.app",
        # Hide from Dock (menubar-only app)
        "LSUIElement": True,
    },
}


setup(
    name="everlog-macos-app",
    version="0.1.0",
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
