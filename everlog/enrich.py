# Role: 1日分のJSONLをLLMで要約して、セグメント単位のラベル/要約を生成する。
# How: JSONLを読み込みセグメント化→OpenAI APIで解析→結果を out/ に保存する。
# Key functions: `enrich_day_with_llm()`
# Collaboration: `everlog/cli.py` から呼ばれる。`summarize.py` が結果を参照する。
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os

from .jsonl import read_jsonl
from .llm import analyze_segments
from .paths import ensure_dirs, get_paths
from .segments import build_segments
from .timeutil import normalize_date_arg


def _day_paths(date: str) -> tuple[Path, Path]:
    p = get_paths()
    return p.logs_dir / f"{date}.jsonl", p.out_dir / f"{date}.llm.json"


def _merge_llm_output(
    segments: list[dict[str, Any]], llm_data: dict[str, Any]
) -> list[dict[str, Any]]:
    items = llm_data.get("segments") or []
    by_id: dict[int, dict[str, Any]] = {}
    for it in items:
        try:
            sid = int(it.get("segment_id"))
        except Exception:
            continue
        by_id[sid] = it

    merged: list[dict[str, Any]] = []
    for seg in segments:
        sid = int(seg.get("segment_id", -1))
        add = by_id.get(sid) or {}
        merged.append(
            {
                **seg,
                "task_title": str(add.get("task_title") or ""),
                "task_summary": str(add.get("task_summary") or ""),
                "category": str(add.get("category") or ""),
                "confidence": float(add.get("confidence") or 0.0),
            }
        )
    return merged


def enrich_day_with_llm(
    date_arg: str,
    model: str | None = None,
    max_segments: int = 80,
) -> Path:
    date = normalize_date_arg(date_arg)
    ensure_dirs()
    log_path, out_path = _day_paths(date)

    events = read_jsonl(log_path)
    if not events:
        out_path.write_text(
            f'{{"date":"{date}","error":"no events"}}\n', encoding="utf-8"
        )
        return out_path

    interval_sec = int((events[-1].get("interval_sec") if events else 0) or 0) or 300
    segs = build_segments(events, interval_sec)
    seg_dicts = [s.to_dict() for s in segs]

    truncated = False
    if max_segments > 0 and len(seg_dicts) > max_segments:
        seg_dicts = seg_dicts[:max_segments]
        truncated = True

    api_key = os.environ.get("OPENAI_API_KEY", "")
    model_name = model or os.environ.get("EVERLOG_LLM_MODEL") or os.environ.get("EVERYTIMECAPTURE_LLM_MODEL", "gpt-5-nano")

    llm_result = analyze_segments(date, seg_dicts, model_name, api_key)
    merged = _merge_llm_output(seg_dicts, llm_result.data)

    out = {
        "date": date,
        "model": llm_result.model,
        "generated_at": datetime.now().astimezone().isoformat(),
        "segment_count": len(seg_dicts),
        "truncated": truncated,
        "segments": merged,
    }
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_path
