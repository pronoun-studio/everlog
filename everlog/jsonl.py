# Role: JSONL（1行=1イベント）の追記と読み取りを提供する。
# How: 追記はappendで常に末尾に書き、読み取りは行ごとにJSONとしてパースして配列化する（壊れた行はスキップ）。
# Key functions: `append_jsonl()`, `read_jsonl()`
# Collaboration: `everlog/capture.py` が追記に使い、`everlog/summarize.py` と `everlog/menubar.py` が読み取りに使う。
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
