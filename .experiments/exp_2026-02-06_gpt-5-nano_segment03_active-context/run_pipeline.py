"""
Experiment pipeline (isolated) for:

0) pick latest stage-03 for 2026-02-06
1) extract segment-level OCR (active display + inactive reference)
2) summarize each segment with gpt-5-nano (full OCR)
3) hour-llm beta (segment summaries + hour-pack clusters)
4) daily-llm
5) render final Markdown (3 sections + cost comparison)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import argparse
import json
import os
import re
import sys
import time
import urllib.request


_WS_RE = re.compile(r"\s+")


def _project_root() -> Path:
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "pyproject.toml").exists() or (p / ".git").exists():
            return p
    return here.parents[2]


_ROOT = _project_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            yield json.loads(line)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _append_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _collapse_ws(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").replace("\x00", " ")).strip()


@dataclass(frozen=True)
class Stage03Pick:
    date: str
    run_id: str
    path: Path


@dataclass(frozen=True)
class Stage04Pick:
    date: str
    run_id: str
    path: Path


def find_stage03(date: str, *, run_id: str | None, prefer: str = "mtime") -> Stage03Pick:
    base = _ROOT / "EVERYTIME-LOG" / "trace" / date
    if run_id:
        p = base / run_id / "stage-03.segment.jsonl"
        if not p.exists():
            raise SystemExit(f"stage-03 not found: {p}")
        return Stage03Pick(date=date, run_id=run_id, path=p)

    files = list(base.glob("*/stage-03.segment.jsonl"))
    if not files:
        raise SystemExit(f"No stage-03 files found under: {base}")

    if prefer == "run_id":
        files.sort(key=lambda x: x.parent.name)
        picked = files[-1]
    else:
        files.sort(key=lambda x: x.stat().st_mtime)
        picked = files[-1]

    return Stage03Pick(date=date, run_id=picked.parent.name, path=picked)


def find_stage04(date: str, run_id: str) -> Stage04Pick:
    p = _ROOT / "EVERYTIME-LOG" / "trace" / date / run_id / "stage-04.hour-pack.jsonl"
    if not p.exists():
        raise SystemExit(f"stage-04 not found: {p}")
    return Stage04Pick(date=date, run_id=run_id, path=p)


def _exp_outputs_dir() -> Path:
    return Path(__file__).resolve().parent / "outputs"


def _out_paths(date: str, run_id: str) -> dict[str, Path]:
    base = _exp_outputs_dir() / date / run_id
    return {
        "base": base,
        "segment_ocr": base / "01.segment_ocr.jsonl",
        "segment_summaries": base / "02.segment_summaries.jsonl",
        "usage_calls": base / "02.usage_calls.jsonl",
        "hourly_llm": base / "03.hourly_llm.json",
        "daily_llm": base / "04.daily_llm.json",
        "report_md": base / "05.report.md",
        "meta": base / "meta.json",
    }


class LlmError(RuntimeError):
    pass


def _load_dotenv_like_everlog() -> None:
    try:
        from everlog.llm import _load_dotenv_if_needed  # type: ignore

        _load_dotenv_if_needed()
    except Exception:
        return


def _extract_output_text(responses_payload: dict[str, Any]) -> str:
    output = responses_payload.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if isinstance(c, dict) and c.get("type") in {"output_text", "text"}:
                text = c.get("text") or ""
                if isinstance(text, str) and text:
                    chunks.append(text)
    return "\n".join(chunks).strip()


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise LlmError("LLM output is not valid JSON.")
    try:
        return json.loads(m.group(0))
    except Exception as e:
        raise LlmError(f"Failed to parse JSON from LLM output: {e}") from e


def _openai_responses(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    json_schema: dict[str, Any],
    schema_name: str,
    api_key: str,
    timeout_sec: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/responses"

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": json_schema,
            }
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        raise LlmError(f"OpenAI API request failed: {e}") from e

    try:
        data = json.loads(raw)
    except Exception as e:
        raise LlmError(f"Failed to parse API response JSON: {e}") from e

    usage = data.get("usage") if isinstance(data, dict) else None
    usage_dict = usage if isinstance(usage, dict) else {}

    raw_text = _extract_output_text(data)
    if not raw_text:
        raise LlmError("OpenAI API returned empty content.")

    parsed = _extract_json(raw_text)
    return parsed, usage_dict, raw_text


def _flatten_segment_ocr(ocr_by_display: Any) -> tuple[str, str]:
    entries: list[tuple[str, bool, str]] = []
    if not isinstance(ocr_by_display, list):
        return "", ""
    for item in ocr_by_display:
        if not isinstance(item, dict):
            continue
        for ev in item.get("events") or []:
            if not isinstance(ev, dict):
                continue
            text = _collapse_ws(str(ev.get("ocr_text") or ""))
            if not text:
                continue
            ts = str(ev.get("ts") or "")
            is_active = bool(ev.get("is_active_display") is True)
            entries.append((ts, is_active, text))
    if not entries:
        return "", ""
    entries.sort(key=lambda x: x[0])
    active = [t for _ts, is_active, t in entries if is_active]
    inactive = [t for _ts, is_active, t in entries if not is_active]
    return "\n\n".join(active), "\n\n".join(inactive)


def step_extract_segment_ocr(date: str, *, run_id: str | None, prefer: str) -> Stage03Pick:
    pick = find_stage03(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)
    out["base"].mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    total = 0
    nonempty_active = 0

    for seg in _read_jsonl(pick.path):
        total += 1
        active_text, inactive_text = _flatten_segment_ocr(seg.get("ocr_by_display"))
        if active_text:
            nonempty_active += 1
        rows.append(
            {
                "segment_id": seg.get("segment_id"),
                "segment_key": seg.get("segment_key") or [],
                "hour_start_ts": seg.get("hour_start_ts") or "",
                "hour_end_ts": seg.get("hour_end_ts") or "",
                "active_display_ocr_text": active_text,
                "inactive_display_ocr_text": inactive_text,
            }
        )

    _write_jsonl(out["segment_ocr"], rows)

    meta = {
        "generated_at": _now_ts(),
        "date": date,
        "run_id": pick.run_id,
        "stage_03_path": str(pick.path),
        "segments_total": total,
        "segments_active_ocr_nonempty": nonempty_active,
    }
    out["meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return pick


_SEGMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segments"],
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["segment_id", "task_title", "task_summary", "evidence", "confidence"],
                "properties": {
                    "segment_id": {"type": "integer", "minimum": 0},
                    "task_title": {"type": "string"},
                    "task_summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


def _build_segment_user_prompt(date: str, segments: list[dict[str, Any]]) -> str:
    payload = json.dumps({"date": date, "segments": segments}, ensure_ascii=False, indent=2)
    return (
        "あなたはPC作業ログの解析者です。以下のセグメント一覧から、各セグメントを要約してください。\n"
        "\n"
        "重要:\n"
        "- 出力は必ずJSONのみ（余計な説明文は一切禁止）\n"
        "- **active_display_ocr_text が主作業**。inactive_display_ocr_text は参照情報として補助的に使う\n"
        "- 非アクティブなディスプレイを参照しながらアクティブなディスプレイの作業を進めている文脈で解釈する\n"
        "- task_title は短く具体的（行動+対象）。アプリ名の羅列は禁止\n"
        "- task_summary は日本語1-2文。何をしていたか/目的を明示\n"
        "- evidence は根拠の短いアンカー（最大4）。URL/パス/ファイル名/画面タイトルなど\n"
        "- 推測は控えめ（OCRに根拠がある範囲）\n"
        "- 秘密情報は出さない（APIキー/トークン/メール/パスワードっぽいものは伏せる。既に [REDACTED_*] があれば尊重）\n"
        "\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "segments": [\n'
        "    {\n"
        '      "segment_id": 0,\n'
        '      "task_title": "短い作業名",\n'
        '      "task_summary": "1-2文の要約",\n'
        '      "evidence": ["host/path", "~/path/file", "cmd ..."],\n'
        '      "confidence": 0.0\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "入力:\n"
        f"{payload}\n"
    )


def _already_done_segment_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    out: set[int] = set()
    for row in _read_jsonl(path):
        sid = row.get("segment_id")
        if isinstance(sid, int):
            out.add(sid)
    return out


def _get_usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(usage, dict):
        return None
    if "input_tokens" in usage or "output_tokens" in usage:
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return None


def step_summarize_segments(
    date: str,
    *,
    run_id: str | None,
    prefer: str,
    model: str,
    batch_size: int,
    max_chars: int,
    max_new_segments: int,
    timeout_sec: int,
    sleep_sec: float,
) -> Stage03Pick:
    pick = find_stage03(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)

    if not out["segment_ocr"].exists():
        step_extract_segment_ocr(date, run_id=pick.run_id, prefer=prefer)

    _load_dotenv_like_everlog()
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (also tried .env auto-load).")

    from everlog.llm import calc_cost_usd  # local import to keep this experiment isolated

    done = _already_done_segment_ids(out["segment_summaries"])
    to_process: list[dict[str, Any]] = []

    for row in _read_jsonl(out["segment_ocr"]):
        sid = row.get("segment_id")
        if not isinstance(sid, int) or sid in done:
            continue
        active_text = str(row.get("active_display_ocr_text") or "")
        if not active_text.strip():
            continue  # skip empty OCR
        inactive_text = str(row.get("inactive_display_ocr_text") or "")
        if max_chars and max_chars > 0:
            active_text = active_text[:max_chars]
            inactive_text = inactive_text[:max_chars]
        to_process.append(
            {
                "segment_id": sid,
                "segment_key": row.get("segment_key") or [],
                "hour_start_ts": row.get("hour_start_ts") or "",
                "hour_end_ts": row.get("hour_end_ts") or "",
                "active_display_ocr_text": active_text,
                "inactive_display_ocr_text": inactive_text,
            }
        )

    total_target = len(to_process)
    if max_new_segments and max_new_segments > 0:
        to_process = to_process[:max_new_segments]

    print(
        f"[{date} {pick.run_id}] summarize-segments: already_done={len(done)} "
        f"pending={len(to_process)} (target_total_pending={total_target}) model={model} batch_size={batch_size}",
        flush=True,
    )

    system_prompt = "You are a precise assistant that returns JSON only. Do not include any extra text."
    call_idx = 0
    if out["usage_calls"].exists():
        call_idx = sum(1 for _ in out["usage_calls"].open("r", encoding="utf-8") if _.strip())

    def call_llm(payload_segments: list[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[str, Any], float]:
        user_prompt = _build_segment_user_prompt(date, payload_segments)
        started = time.time()
        parsed, usage, _raw_text = _openai_responses(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=_SEGMENTS_SCHEMA,
            schema_name="segment_summaries",
            api_key=api_key,
            timeout_sec=timeout_sec,
        )
        elapsed = time.time() - started

        segments_out = parsed.get("segments")
        if not isinstance(segments_out, list):
            raise LlmError("Bad response: 'segments' is not a list.")

        by_id: dict[int, dict[str, Any]] = {}
        for item in segments_out:
            if not isinstance(item, dict):
                continue
            sid = item.get("segment_id")
            if isinstance(sid, int):
                by_id[sid] = item
        return by_id, usage, elapsed

    out_rows: list[dict[str, Any]] = []
    usage_rows: list[dict[str, Any]] = []

    for i in range(0, len(to_process), max(1, batch_size)):
        batch = to_process[i : i + max(1, batch_size)]
        by_id, usage, elapsed = call_llm(batch)

        for seg in batch:
            sid = seg.get("segment_id")
            if not isinstance(sid, int):
                continue
            item = by_id.get(sid)
            if not item:
                continue
            out_rows.append(
                {
                    "segment_id": sid,
                    "segment_key": seg.get("segment_key") or [],
                    "hour_start_ts": seg.get("hour_start_ts") or "",
                    "hour_end_ts": seg.get("hour_end_ts") or "",
                    "task_title": str(item.get("task_title") or "").strip(),
                    "task_summary": str(item.get("task_summary") or "").strip(),
                    "evidence": item.get("evidence") or [],
                    "confidence": float(item.get("confidence") or 0.0),
                }
            )

        cost = calc_cost_usd(model, usage)
        usage_rows.append(
            {
                "call_idx": call_idx,
                "batch_segments": len(batch),
                "elapsed_sec": round(elapsed, 3),
                "model": model,
                "usage": usage,
                "cost_usd": cost,
                "generated_at": _now_ts(),
            }
        )
        call_idx += 1

        if sleep_sec > 0:
            time.sleep(sleep_sec)

    if out_rows:
        _append_jsonl(out["segment_summaries"], out_rows)
    if usage_rows:
        _append_jsonl(out["usage_calls"], usage_rows)

    return pick


_HOURS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hours"],
    "properties": {
        "hours": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "hour_start_ts",
                    "hour_end_ts",
                    "hour_title",
                    "hour_summary",
                    "hour_detail",
                    "confidence",
                    "evidence",
                ],
                "properties": {
                    "hour_start_ts": {"type": "string"},
                    "hour_end_ts": {"type": "string"},
                    "hour_title": {"type": "string"},
                    "hour_summary": {"type": "string"},
                    "hour_detail": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


def _build_hourly_beta_prompt(date: str, hours: list[dict[str, Any]], segment_summaries: dict[str, Any]) -> str:
    payload = json.dumps(
        {"date": date, "segment_summaries": segment_summaries, "hours": hours},
        ensure_ascii=False,
        indent=2,
    )
    return (
        "あなたはPC作業ログの解析者です。以下の1時間パッケージ一覧から、各時間帯に"
        "「hour_title」「hour_summary」「hour_detail」「confidence」「evidence」を付与してください。\n"
        "\n"
        "重要:\n"
        "- 出力は必ずJSONのみ（余計な説明文は一切禁止）\n"
        "- **segment_summaries が主入力**。hours[].clusters はセグメントの集合と文脈ヒント\n"
        "- segment_summaries は『アクティブ画面の作業』を中心に、非アクティブ画面を参照しながら進めている文脈で要約済み\n"
        "- hour_title: 短く具体的（行動+目的）。アプリ名だけは禁止\n"
        "- hour_summary: 日本語1〜2文。何をしていたか/目的を明示。URL/パスの列挙は禁止\n"
        "- hour_detail: 8〜14文。観測→解釈→推測（1つだけ）の順\n"
        "  - 推測は **1つだけ**（" "推測:" " を1回だけ）。列挙は禁止\n"
        "  - 断定寄りでよい（ただし入力に根拠がある範囲）\n"
        "- evidence: URL/ファイル/短いフレーズなど根拠（最大8）\n"
        "- 秘密情報は出さない（APIキー/トークン/メール/パスワードっぽいものは伏せる。既に [REDACTED_*] があれば尊重）\n"
        "\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "hours": [\n'
        "    {\n"
        '      "hour_start_ts": "2026-02-05T10:00:00+09:00",\n'
        '      "hour_end_ts": "2026-02-05T10:59:59+09:00",\n'
        '      "hour_title": "短い作業名",\n'
        '      "hour_summary": "1-2文の要約",\n'
        '      "hour_detail": "8-14文の詳細",\n'
        '      "confidence": 0.0,\n'
        '      "evidence": ["host/path", "~/path/file", "cmd ..."]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "入力:\n"
        f"{payload}\n"
    )


def _read_segment_summaries(path: Path) -> dict[str, dict[str, Any]]:
    seg_map: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        sid = row.get("segment_id")
        if isinstance(sid, int):
            seg_map[str(sid)] = {
                "task_title": row.get("task_title") or "",
                "task_summary": row.get("task_summary") or "",
            }
    return seg_map


def step_hourly_llm_beta(
    date: str,
    *,
    run_id: str | None,
    prefer: str,
    model: str,
    timeout_sec: int,
) -> Stage03Pick:
    pick = find_stage03(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)

    if not out["segment_summaries"].exists():
        step_summarize_segments(
            date,
            run_id=pick.run_id,
            prefer=prefer,
            model=model,
            batch_size=1,
            max_chars=0,
            max_new_segments=0,
            timeout_sec=timeout_sec,
            sleep_sec=0,
        )

    seg_map = _read_segment_summaries(out["segment_summaries"])
    if not seg_map:
        raise SystemExit("segment summaries are empty; run summarize-segments first")

    stage04 = find_stage04(date, pick.run_id)
    hours_in: list[dict[str, Any]] = []
    for row in _read_jsonl(stage04.path):
        hours_in.append(row)

    hours_compact: list[dict[str, Any]] = []
    for h in hours_in:
        ch: dict[str, Any] = {
            "hour_start_ts": h.get("hour_start_ts") or "",
            "hour_end_ts": h.get("hour_end_ts") or "",
        }
        clusters = []
        for c in h.get("clusters") or []:
            if not isinstance(c, dict):
                continue
            clusters.append(
                {
                    "segment_ids": c.get("segment_ids") or [],
                    "segment_key": c.get("segment_key") or [],
                }
            )
        ch["clusters"] = clusters
        hours_compact.append(ch)

    _load_dotenv_like_everlog()
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (also tried .env auto-load).")

    system_prompt = "You are a precise assistant that returns JSON only. Do not include any extra text."
    user_prompt = _build_hourly_beta_prompt(date, hours_compact, seg_map)

    parsed, usage, _raw_text = _openai_responses(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=_HOURS_SCHEMA,
        schema_name="hourly_summaries",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )

    hours_out = parsed.get("hours")
    if not isinstance(hours_out, list):
        raise LlmError("Bad response: 'hours' is not a list.")

    payload = {
        "date": date,
        "model": model,
        "generated_at": _now_ts(),
        "usage": usage,
        "hours": hours_out,
    }
    out["hourly_llm"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return pick


_DAILY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["daily_title", "daily_summary", "daily_detail", "highlights", "confidence", "evidence"],
    "properties": {
        "daily_title": {"type": "string"},
        "daily_summary": {"type": "string"},
        "daily_detail": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}


def _build_daily_prompt(date: str, hours: list[dict[str, Any]]) -> str:
    payload = json.dumps({"date": date, "hours": hours}, ensure_ascii=False, indent=2)
    return (
        "あなたはPC作業ログの解析者です。以下の1時間要約（hours）を踏まえて、1日全体の総括を作ってください。\n"
        "\n"
        "重要: 1日の総括は、hoursを全部俯瞰して「何をやった日か」を自然文でまとめる。\n"
        "- daily_title: 短く具体的（行動+目的）。アプリ名だけは禁止\n"
        "- daily_summary: 2〜3文。今日の主目的と流れを要約（断定寄りでよい）\n"
        "- daily_detail: 4〜6文。時系列の大きな流れ→山場→結果/次の課題。推測は **1つだけ**（" "推測:" " を1回だけ）\n"
        "- highlights: 箇条書きの短文（3〜6個）。成果/進捗/意思決定を中心に\n"
        "- evidence: 根拠の短いアンカー（最大10）\n"
        "\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "daily_title": "短い総括タイトル",\n'
        '  "daily_summary": "2-3文の要約",\n'
        '  "daily_detail": "4-6文の詳細",\n'
        '  "highlights": ["...", "..."],\n'
        '  "confidence": 0.0,\n'
        '  "evidence": ["..."]\n'
        "}\n"
        "\n"
        "入力:\n"
        f"{payload}\n"
    )


def step_daily_llm(
    date: str,
    *,
    run_id: str | None,
    prefer: str,
    model: str,
    timeout_sec: int,
) -> Stage03Pick:
    pick = find_stage03(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)

    if not out["hourly_llm"].exists():
        step_hourly_llm_beta(date, run_id=pick.run_id, prefer=prefer, model=model, timeout_sec=timeout_sec)

    hourly_payload = json.loads(out["hourly_llm"].read_text(encoding="utf-8"))
    hours = hourly_payload.get("hours")
    if not isinstance(hours, list):
        raise SystemExit("hourly_llm output is invalid")

    _load_dotenv_like_everlog()
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (also tried .env auto-load).")

    system_prompt = "You are a precise assistant that returns JSON only. Do not include any extra text."
    user_prompt = _build_daily_prompt(date, hours)

    parsed, usage, _raw_text = _openai_responses(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=_DAILY_SCHEMA,
        schema_name="daily_summary",
        api_key=api_key,
        timeout_sec=timeout_sec,
    )

    payload = {
        "date": date,
        "model": model,
        "generated_at": _now_ts(),
        "usage": usage,
        "daily": parsed,
    }
    out["daily_llm"].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return pick


def _fmt_int(n: int) -> str:
    return f"{int(n):,}"


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v:.6f}"


def _sum_usage_calls(path: Path) -> tuple[int, int, float | None, int]:
    total_in = 0
    total_out = 0
    total_cost = 0.0
    calls = 0
    any_cost = False
    if not path.exists():
        return 0, 0, None, 0
    for row in _read_jsonl(path):
        calls += 1
        usage = row.get("usage") if isinstance(row, dict) else None
        tokens = _get_usage_tokens(usage)
        if tokens:
            total_in += int(tokens[0])
            total_out += int(tokens[1])
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
            any_cost = True
    return total_in, total_out, (total_cost if any_cost else None), calls


def _load_usage_from_payload(path: Path) -> tuple[int, int, float | None]:
    if not path.exists():
        return 0, 0, None
    data = json.loads(path.read_text(encoding="utf-8"))
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens = _get_usage_tokens(usage)
    if not tokens:
        return 0, 0, None
    from everlog.llm import calc_cost_usd

    model = data.get("model") or ""
    return int(tokens[0]), int(tokens[1]), calc_cost_usd(str(model), usage)


def render_md(date: str, *, run_id: str | None, prefer: str) -> Stage03Pick:
    pick = find_stage03(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)

    if not out["daily_llm"].exists():
        step_daily_llm(date, run_id=pick.run_id, prefer=prefer, model="gpt-5-nano", timeout_sec=180)

    seg_in, seg_out, seg_cost, seg_calls = _sum_usage_calls(out["usage_calls"])
    hour_in, hour_out, hour_cost = _load_usage_from_payload(out["hourly_llm"])
    day_in, day_out, day_cost = _load_usage_from_payload(out["daily_llm"])

    total_in = seg_in + hour_in + day_in
    total_out = seg_out + hour_out + day_out
    total_cost = 0.0
    any_cost = False
    for c in (seg_cost, hour_cost, day_cost):
        if isinstance(c, (int, float)):
            total_cost += float(c)
            any_cost = True

    # Baseline (existing pipeline) for comparison
    baseline_hour = _ROOT / "EVERYTIME-LOG" / "out" / date / pick.run_id / f"{date}.hourly.llm.json"
    baseline_day = _ROOT / "EVERYTIME-LOG" / "out" / date / pick.run_id / f"{date}.daily.llm.json"
    base_in = base_out = 0
    base_cost = 0.0
    base_any = False
    for p in (baseline_hour, baseline_day):
        bi, bo, bc = _load_usage_from_payload(p)
        base_in += bi
        base_out += bo
        if isinstance(bc, (int, float)):
            base_cost += float(bc)
            base_any = True

    daily_payload = json.loads(out["daily_llm"].read_text(encoding="utf-8"))
    daily = daily_payload.get("daily") if isinstance(daily_payload, dict) else None

    hourly_payload = json.loads(out["hourly_llm"].read_text(encoding="utf-8"))
    hours = hourly_payload.get("hours") if isinstance(hourly_payload, dict) else None
    if not isinstance(hours, list):
        hours = []

    lines: list[str] = []
    lines.append("## 処理コスト")
    lines.append(f"- **入力**: `{_ROOT / 'EVERYTIME-LOG' / 'trace' / date / pick.run_id / 'stage-03.segment.jsonl'}`")
    lines.append(f"- **モデル**: `gpt-5-nano`")
    lines.append(f"- **segment-llm**: input {_fmt_int(seg_in)} / output {_fmt_int(seg_out)} tokens（cost: {_fmt_usd(seg_cost)} / calls: {seg_calls}）")
    lines.append(f"- **hour-llm-β**: input {_fmt_int(hour_in)} / output {_fmt_int(hour_out)} tokens（cost: {_fmt_usd(hour_cost)}）")
    lines.append(f"- **daily-llm**: input {_fmt_int(day_in)} / output {_fmt_int(day_out)} tokens（cost: {_fmt_usd(day_cost)}）")
    lines.append(
        f"- **合計**: input {_fmt_int(total_in)} / output {_fmt_int(total_out)} tokens（cost: {_fmt_usd(total_cost if any_cost else None)}）"
    )
    if base_in or base_out or base_any:
        lines.append(
            f"- **比較（既存 hour+daily）**: input {_fmt_int(base_in)} / output {_fmt_int(base_out)} tokens（cost: {_fmt_usd(base_cost if base_any else None)}）"
        )

    lines.append("")
    lines.append("## 本日のメイン作業")
    if isinstance(daily, dict):
        title = str(daily.get("daily_title") or "").strip()
        summary = str(daily.get("daily_summary") or "").strip()
        highlights = [str(s or "").strip() for s in (daily.get("highlights") or []) if str(s or "").strip()]
        if title:
            lines.append(f"- 推定: {title}")
        if summary:
            lines.append(f"- 概要: {summary}")
        if highlights:
            lines.append("")
            lines.append("### ハイライト")
            lines.append("")
            for h in highlights[:6]:
                lines.append(f"- {h}")
    else:
        lines.append("（日次要約がありません）")

    lines.append("")
    lines.append("## 1時間ごとのタイムライン")
    if not hours:
        lines.append("（hour-llm出力がありません）")
    else:
        for h in hours:
            hs = str(h.get("hour_start_ts") or "")
            he = str(h.get("hour_end_ts") or "")
            title = str(h.get("hour_title") or "").strip()
            summary = str(h.get("hour_summary") or "").strip()
            detail = str(h.get("hour_detail") or "").strip()
            if hs and he:
                lines.append(f"### {hs[-14:-9]}〜{he[-14:-9]}")
            else:
                lines.append("### (hour)")
            if title:
                lines.append(f"- 推定: {title}")
            if summary:
                lines.append(f"- 概要: {summary}")
            if detail:
                lines.append(f"- 詳細: {detail}")
            lines.append("")

    out["report_md"].write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return pick


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_find = sub.add_parser("find-stage03")
    p_find.add_argument("--date", required=True)
    p_find.add_argument("--run-id")
    p_find.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])

    p_extract = sub.add_parser("extract-segment-ocr")
    p_extract.add_argument("--date", required=True)
    p_extract.add_argument("--run-id")
    p_extract.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])

    p_sum = sub.add_parser("summarize-segments")
    p_sum.add_argument("--date", required=True)
    p_sum.add_argument("--run-id")
    p_sum.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])
    p_sum.add_argument("--model", default="gpt-5-nano")
    p_sum.add_argument("--batch-size", type=int, default=1)
    p_sum.add_argument("--max-chars", type=int, default=0)
    p_sum.add_argument("--max-new-segments", type=int, default=0)
    p_sum.add_argument("--timeout-sec", type=int, default=180)
    p_sum.add_argument("--sleep-sec", type=float, default=0.0)

    p_hour = sub.add_parser("hourly-llm-beta")
    p_hour.add_argument("--date", required=True)
    p_hour.add_argument("--run-id")
    p_hour.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])
    p_hour.add_argument("--model", default="gpt-5-nano")
    p_hour.add_argument("--timeout-sec", type=int, default=180)

    p_day = sub.add_parser("daily-llm")
    p_day.add_argument("--date", required=True)
    p_day.add_argument("--run-id")
    p_day.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])
    p_day.add_argument("--model", default="gpt-5-nano")
    p_day.add_argument("--timeout-sec", type=int, default=180)

    p_md = sub.add_parser("render-md")
    p_md.add_argument("--date", required=True)
    p_md.add_argument("--run-id")
    p_md.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])

    p_all = sub.add_parser("all")
    p_all.add_argument("--date", required=True)
    p_all.add_argument("--run-id")
    p_all.add_argument("--prefer", default="mtime", choices=["mtime", "run_id"])
    p_all.add_argument("--model", default="gpt-5-nano")
    p_all.add_argument("--timeout-sec", type=int, default=180)

    args = p.parse_args()

    if args.cmd == "find-stage03":
        pick = find_stage03(args.date, run_id=args.run_id, prefer=args.prefer)
        print(json.dumps({"date": pick.date, "run_id": pick.run_id, "path": str(pick.path)}, ensure_ascii=False))
        return

    if args.cmd == "extract-segment-ocr":
        pick = step_extract_segment_ocr(args.date, run_id=args.run_id, prefer=args.prefer)
        out = _out_paths(args.date, pick.run_id)
        print(f"OK: {out['segment_ocr']}")
        return

    if args.cmd == "summarize-segments":
        pick = step_summarize_segments(
            args.date,
            run_id=args.run_id,
            prefer=args.prefer,
            model=args.model,
            batch_size=args.batch_size,
            max_chars=args.max_chars,
            max_new_segments=args.max_new_segments,
            timeout_sec=args.timeout_sec,
            sleep_sec=args.sleep_sec,
        )
        out = _out_paths(args.date, pick.run_id)
        print(f"OK: {out['segment_summaries']}")
        return

    if args.cmd == "hourly-llm-beta":
        pick = step_hourly_llm_beta(
            args.date,
            run_id=args.run_id,
            prefer=args.prefer,
            model=args.model,
            timeout_sec=args.timeout_sec,
        )
        out = _out_paths(args.date, pick.run_id)
        print(f"OK: {out['hourly_llm']}")
        return

    if args.cmd == "daily-llm":
        pick = step_daily_llm(
            args.date,
            run_id=args.run_id,
            prefer=args.prefer,
            model=args.model,
            timeout_sec=args.timeout_sec,
        )
        out = _out_paths(args.date, pick.run_id)
        print(f"OK: {out['daily_llm']}")
        return

    if args.cmd == "render-md":
        pick = render_md(args.date, run_id=args.run_id, prefer=args.prefer)
        out = _out_paths(args.date, pick.run_id)
        print(f"OK: {out['report_md']}")
        return

    if args.cmd == "all":
        pick = step_extract_segment_ocr(args.date, run_id=args.run_id, prefer=args.prefer)
        step_summarize_segments(
            args.date,
            run_id=pick.run_id,
            prefer=args.prefer,
            model=args.model,
            batch_size=1,
            max_chars=0,
            max_new_segments=0,
            timeout_sec=args.timeout_sec,
            sleep_sec=0,
        )
        step_hourly_llm_beta(
            args.date,
            run_id=pick.run_id,
            prefer=args.prefer,
            model=args.model,
            timeout_sec=args.timeout_sec,
        )
        step_daily_llm(
            args.date,
            run_id=pick.run_id,
            prefer=args.prefer,
            model=args.model,
            timeout_sec=args.timeout_sec,
        )
        render_md(args.date, run_id=pick.run_id, prefer=args.prefer)
        out = _out_paths(args.date, pick.run_id)
        print(f"OK: {out['report_md']}")
        return


if __name__ == "__main__":
    main()
