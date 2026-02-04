# Role: Pythonパッケージ `everlog` のメタ情報（バージョン等）を提供する。
# How: 依存先が `__version__` を参照できるように、最小限の定数だけを公開する。
# Key functions: なし（定数のみ）。
# Collaboration: `pyproject.toml` のCLIエントリポイントから読み込まれ、ログやUI表示のバージョン表示などに使える。
__all__ = ["__version__"]

__version__ = "0.1.0"
