"""
Experiment pipeline (isolated) for:

0) pick latest stage-02 for 2026-02-06
1) extract active-display OCR text only
2) summarize each event (batched) with gpt-5-nano
3) group summaries by hour (timeline)
4) render final Markdown (3 sections)

This is intentionally independent from the main everlog summarize pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
    # Fallback: 2 levels up from experiments/<exp>
    return here.parents[2]


# Ensure local imports (e.g., `import everlog`) work even when executed from outside repo root.
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


def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _hour_start(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _collapse_ws(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").replace("\x00", " ")).strip()


def _active_display_ocr(ocr_by_display: Any) -> str:
    if not isinstance(ocr_by_display, list):
        return ""
    for item in ocr_by_display:
        if not isinstance(item, dict):
            continue
        if item.get("is_active_display") is True:
            return _collapse_ws(str(item.get("ocr_text") or ""))
    return ""


@dataclass(frozen=True)
class Stage02Pick:
    date: str
    run_id: str
    path: Path


def find_stage02(date: str, *, run_id: str | None, prefer: str = "mtime") -> Stage02Pick:
    root = _project_root()
    base = root / "EVERYTIME-LOG" / "trace" / date
    if run_id:
        p = base / run_id / "stage-02.segment.jsonl"
        if not p.exists():
            raise SystemExit(f"stage-02 not found: {p}")
        return Stage02Pick(date=date, run_id=run_id, path=p)

    files = list(base.glob("*/stage-02.segment.jsonl"))
    if not files:
        raise SystemExit(f"No stage-02 files found under: {base}")

    if prefer == "run_id":
        files.sort(key=lambda x: x.parent.name)
        picked = files[-1]
    else:
        files.sort(key=lambda x: x.stat().st_mtime)
        picked = files[-1]

    return Stage02Pick(date=date, run_id=picked.parent.name, path=picked)


def _exp_outputs_dir() -> Path:
    return Path(__file__).resolve().parent / "outputs"


def _out_paths(date: str, run_id: str) -> dict[str, Path]:
    base = _exp_outputs_dir() / date / run_id
    return {
        "base": base,
        "active_ocr": base / "01.active_ocr.jsonl",
        "event_summaries": base / "02.event_summaries.jsonl",
        "usage_calls": base / "02.usage_calls.jsonl",
        "hourly": base / "03.hourly.jsonl",
        "report_md": base / "04.report.md",
        "meta": base / "meta.json",
    }


def step_extract_active_ocr(date: str, *, run_id: str | None, prefer: str) -> Stage02Pick:
    pick = find_stage02(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)
    out["base"].mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    total = 0
    nonempty = 0

    for e in _read_jsonl(pick.path):
        total += 1
        text = _active_display_ocr(e.get("ocr_by_display"))
        if text:
            nonempty += 1
        dt = _parse_dt(str(e.get("ts") or "1970-01-01T00:00:00+00:00"))
        rows.append(
            {
                "event_id": e.get("event_id"),
                "ts": e.get("ts"),
                "hour_start_ts": _hour_start(dt).isoformat(),
                "active_app": e.get("active_app") or "",
                "domain": e.get("domain") or "",
                "window_title": e.get("window_title") or "",
                "segment_id": e.get("segment_id"),
                "segment_label": e.get("segment_label") or "",
                "active_display_ocr_text": text,
            }
        )

    _write_jsonl(out["active_ocr"], rows)

    meta = {
        "generated_at": _now_ts(),
        "date": date,
        "run_id": pick.run_id,
        "stage_02_path": str(pick.path),
        "events_total": total,
        "events_active_ocr_nonempty": nonempty,
    }
    out["meta"].write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return pick


class LlmError(RuntimeError):
    pass


def _load_dotenv_like_everlog() -> None:
    # Reuse everlog's .env search behavior (without importing lots of pipeline code).
    try:
        from everlog.llm import _load_dotenv_if_needed  # type: ignore

        _load_dotenv_if_needed()
    except Exception:
        return


def _extract_output_text(responses_payload: dict[str, Any]) -> str:
    out: list[str] = []
    for item in (responses_payload.get("output") or []):
        if not isinstance(item, dict):
            continue
        for part in (item.get("content") or []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                t = str(part.get("text") or "")
                if t:
                    out.append(t)
    if out:
        return "".join(out).strip()

    choices = responses_payload.get("choices") or []
    if isinstance(choices, list) and choices:
        raw_text = (
            (choices[0] or {}).get("message", {}).get("content", "") if isinstance(choices[0], dict) else ""
        )
        return str(raw_text or "").strip()
    return ""


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise LlmError("LLM output is not valid JSON.")
    return json.loads(m.group(0))


def _openai_responses(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    json_schema: dict[str, Any],
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
                "name": "event_summaries",
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


_EVENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id", "ts", "summary", "evidence", "confidence"],
                "properties": {
                    "event_id": {"type": "string"},
                    "ts": {"type": "string"},
                    "summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


def _build_event_user_prompt(date: str, events: list[dict[str, Any]]) -> str:
    payload = json.dumps({"date": date, "events": events}, ensure_ascii=False, indent=2)
    return (
        "あなたはPC作業ログの解析者です。以下のイベント一覧（active display OCRのみ）から、各イベントを短く要約してください。\n"
        "\n"
        "重要:\n"
        "- 出力は必ずJSONのみ（余計な説明文は一切禁止）\n"
        "- summary は日本語1文（最大でも2文）。「何をしていたか」を行動+対象で書く（アプリ名の羅列は禁止）\n"
        "- evidence は根拠の短いアンカー（最大3）。URL/パス/ファイル名/画面タイトルなど。羅列しすぎない\n"
        "- 推測は控えめ（OCRに根拠がある範囲）\n"
        "- 秘密情報は出さない（APIキー/トークン/メール/パスワードっぽいものは伏せる。既に [REDACTED_*] があれば尊重）\n"
        "\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "events": [\n'
        "    {\n"
        '      "event_id": "uuid",\n'
        '      "ts": "2026-02-06T00:00:00+09:00",\n'
        '      "summary": "1-2文の要約",\n'
        '      "evidence": ["host/path", "~/path/file", "cmd ..."],\n'
        '      "confidence": 0.0\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "\n"
        "入力:\n"
        f"{payload}\n"
    )


def _already_done_event_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for row in _read_jsonl(path):
        eid = row.get("event_id")
        if isinstance(eid, str) and eid:
            out.add(eid)
    return out


def step_summarize_events(
    date: str,
    *,
    run_id: str | None,
    prefer: str,
    model: str,
    batch_size: int,
    max_chars: int,
    max_new_events: int,
    timeout_sec: int,
    sleep_sec: float,
) -> Stage02Pick:
    pick = find_stage02(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)

    if not out["active_ocr"].exists():
        step_extract_active_ocr(date, run_id=pick.run_id, prefer=prefer)

    _load_dotenv_like_everlog()
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is not set (also tried .env auto-load).")

    from everlog.llm import calc_cost_usd  # local import to keep this experiment isolated

    done = _already_done_event_ids(out["event_summaries"])
    to_process: list[dict[str, Any]] = []

    for row in _read_jsonl(out["active_ocr"]):
        eid = str(row.get("event_id") or "").strip()
        if not eid or eid in done:
            continue
        text = str(row.get("active_display_ocr_text") or "")
        if not text.strip():
            continue  # skip empty OCR to control cost
        # Keep minimal context, but still include anchors.
        to_process.append(
            {
                "event_id": eid,
                "ts": row.get("ts") or "",
                "active_app": row.get("active_app") or "",
                "domain": row.get("domain") or "",
                "window_title": row.get("window_title") or "",
                "segment_id": row.get("segment_id"),
                "segment_label": row.get("segment_label") or "",
                "active_display_ocr_text": text,
            }
        )

    total_target = len(to_process)
    if max_new_events and max_new_events > 0:
        to_process = to_process[:max_new_events]

    print(
        f"[{date} {pick.run_id}] summarize-events: already_done={len(done)} "
        f"pending={len(to_process)} (target_total_pending={total_target}) model={model} batch_size={batch_size}",
        flush=True,
    )

    system_prompt = "You are a precise assistant that returns JSON only. Do not include any extra text."
    call_idx = 0
    if out["usage_calls"].exists():
        # Continue numbering for clarity.
        call_idx = sum(1 for _ in out["usage_calls"].open("r", encoding="utf-8") if _.strip())

    def call_llm_for_events(payload_events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any], float]:
        """
        Call LLM and return (by_event_id, usage, elapsed_sec).
        """
        user_prompt = _build_event_user_prompt(date, payload_events)
        started = time.time()
        parsed, usage, _raw_text = _openai_responses(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_schema=_EVENTS_SCHEMA,
            api_key=api_key,
            timeout_sec=timeout_sec,
        )
        elapsed = time.time() - started

        events_out = parsed.get("events")
        if not isinstance(events_out, list):
            raise LlmError("Bad response: 'events' is not a list.")

        by_id: dict[str, dict[str, Any]] = {}
        for item in events_out:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("event_id") or "").strip()
            if eid:
                by_id[eid] = item
        return by_id, usage, elapsed

    i = 0
    while i < len(to_process):
        batch = to_process[i : i + batch_size]
        i += batch_size
        print(
            f"[{date} {pick.run_id}] batch {i - len(batch) + 1}-{min(i, len(to_process))}/{len(to_process)}",
            flush=True,
        )

        payload_events: list[dict[str, Any]] = []
        payload_by_id: dict[str, dict[str, Any]] = {}
        for ev in batch:
            pe = {
                "event_id": ev["event_id"],
                "ts": ev["ts"],
                "context": _collapse_ws(
                    " / ".join(
                        x
                        for x in [
                            str(ev.get("active_app") or "").strip(),
                            str(ev.get("domain") or "").strip(),
                            str(ev.get("window_title") or "").strip(),
                        ]
                        if x
                    )
                ),
                "ocr": (str(ev.get("active_display_ocr_text") or "")[:max_chars]).strip(),
            }
            payload_events.append(pe)
            payload_by_id[str(ev["event_id"])] = pe

        # Robust collection: retry missing ids a few times, then per-event, then fallback.
        want_ids = [str(ev["event_id"]) for ev in batch]
        pending: set[str] = set(want_ids)
        got: dict[str, dict[str, Any]] = {}

        def record_usage(usage: dict[str, Any], elapsed: float, batch_events: int) -> None:
            nonlocal call_idx
            cost_usd = calc_cost_usd(usage, model)
            usage_row = {
                "call_idx": call_idx,
                "batch_events": batch_events,
                "elapsed_sec": round(elapsed, 3),
                "model": model,
                "usage": usage,
                "cost_usd": cost_usd,
                "generated_at": _now_ts(),
            }
            _append_jsonl(out["usage_calls"], [usage_row])
            call_idx += 1

        # 1) Initial batch call
        by_id, usage, elapsed = call_llm_for_events(payload_events)
        record_usage(usage, elapsed, len(payload_events))
        for eid, item in by_id.items():
            if eid in pending:
                got[eid] = item
                pending.discard(eid)

        # 2) Retry missing as a smaller batch (up to 2 times)
        for _attempt in range(2):
            if not pending:
                break
            subset = [payload_by_id[eid] for eid in want_ids if eid in pending]
            by_id2, usage2, elapsed2 = call_llm_for_events(subset)
            record_usage(usage2, elapsed2, len(subset))
            for eid, item in by_id2.items():
                if eid in pending:
                    got[eid] = item
                    pending.discard(eid)
            if sleep_sec > 0:
                time.sleep(sleep_sec)

        # 3) Per-event retry (last resort, still cheap-ish)
        if pending:
            for eid in list(pending):
                pe = payload_by_id.get(eid)
                if not pe:
                    pending.discard(eid)
                    continue
                by_id3, usage3, elapsed3 = call_llm_for_events([pe])
                record_usage(usage3, elapsed3, 1)
                if eid in by_id3:
                    got[eid] = by_id3[eid]
                    pending.discard(eid)
                if sleep_sec > 0:
                    time.sleep(sleep_sec)

        # 4) Fallback: never fail the whole run; fill any remaining missing with rule-based text.
        if pending:
            for eid in list(pending):
                pe = payload_by_id.get(eid) or {}
                ctx = str(pe.get("context") or "").strip()
                ocr = str(pe.get("ocr") or "").strip()
                short = (ocr[:120] + "…") if len(ocr) > 120 else ocr
                got[eid] = {
                    "event_id": eid,
                    "ts": str(pe.get("ts") or ""),
                    "summary": f"{ctx} を確認（要約失敗・OCR抜粋: {short}）".strip(),
                    "evidence": [x for x in [ctx] if x][:1],
                    "confidence": 0.0,
                }
                pending.discard(eid)
            print(
                f"[{date} {pick.run_id}] fallback summaries used for missing events in this batch",
                flush=True,
            )

        # Write event summaries
        out_rows: list[dict[str, Any]] = []
        for ev in batch:
            item = got[str(ev["event_id"])]
            out_rows.append(
                {
                    "event_id": ev["event_id"],
                    "ts": ev["ts"],
                    "hour_start_ts": _hour_start(_parse_dt(ev["ts"])).isoformat() if ev["ts"] else "",
                    "segment_id": ev.get("segment_id"),
                    "segment_label": ev.get("segment_label") or "",
                    "context": _collapse_ws(
                        " / ".join(
                            x
                            for x in [
                                str(ev.get("active_app") or "").strip(),
                                str(ev.get("domain") or "").strip(),
                                str(ev.get("window_title") or "").strip(),
                            ]
                            if x
                        )
                    ),
                    "summary": str(item.get("summary") or "").strip(),
                    "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
                    "confidence": float(item.get("confidence") or 0.0),
                    "model": model,
                }
            )
        _append_jsonl(out["event_summaries"], out_rows)

        # gentle throttle
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    return pick


def step_build_hourly(date: str, *, run_id: str | None, prefer: str) -> Stage02Pick:
    pick = find_stage02(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)
    if not out["event_summaries"].exists():
        raise SystemExit(
            f"Missing event summaries. Run summarize-events first: {out['event_summaries']}"
        )

    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in _read_jsonl(out["event_summaries"]):
        hs = str(row.get("hour_start_ts") or "").strip()
        if not hs:
            try:
                hs = _hour_start(_parse_dt(str(row.get("ts") or ""))).isoformat()
            except Exception:
                continue
        buckets.setdefault(hs, []).append(row)

    hourly_rows: list[dict[str, Any]] = []
    for hs in sorted(buckets.keys()):
        items = sorted(buckets[hs], key=lambda x: str(x.get("ts") or ""))
        hdt = _parse_dt(hs)
        hed = hdt + timedelta(hours=1) - timedelta(seconds=1)
        hourly_rows.append(
            {
                "hour_start_ts": hs,
                "hour_end_ts": hed.isoformat(),
                "items": [
                    {
                        "ts": it.get("ts") or "",
                        "event_id": it.get("event_id") or "",
                        "summary": it.get("summary") or "",
                        "evidence": it.get("evidence") or [],
                        "context": it.get("context") or "",
                        "segment_id": it.get("segment_id"),
                        "segment_label": it.get("segment_label") or "",
                        "confidence": it.get("confidence") or 0.0,
                    }
                    for it in items
                    if str(it.get("summary") or "").strip()
                ],
            }
        )

    _write_jsonl(out["hourly"], hourly_rows)
    return pick


def _fmt_hhmm(ts: str) -> str:
    try:
        dt = _parse_dt(ts)
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def _estimate_segment_minutes(items: list[dict[str, Any]]) -> int:
    dts: list[datetime] = []
    for it in items:
        try:
            dts.append(_parse_dt(str(it.get("ts") or "")))
        except Exception:
            continue
    if not dts:
        return 0
    dts.sort()
    # Approx: (last-first) + 60sec padding; clamp to >= 1min
    sec = int((dts[-1] - dts[0]).total_seconds()) + 60
    if sec < 60:
        sec = 60
    return sec // 60


def step_render_md(
    date: str,
    *,
    run_id: str | None,
    prefer: str,
    usd_jpy: float,
) -> Stage02Pick:
    pick = find_stage02(date, run_id=run_id, prefer=prefer)
    out = _out_paths(date, pick.run_id)
    if not out["hourly"].exists():
        raise SystemExit(f"Missing hourly timeline. Run build-hourly first: {out['hourly']}")

    # Cost summary (from usage calls)
    input_tokens = 0
    output_tokens = 0
    total_usd = 0.0
    calls = 0
    if out["usage_calls"].exists():
        for row in _read_jsonl(out["usage_calls"]):
            calls += 1
            usage = row.get("usage") or {}
            if isinstance(usage, dict):
                input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            c = row.get("cost_usd")
            if isinstance(c, (int, float)):
                total_usd += float(c)

    total_jpy = total_usd * usd_jpy

    # Main work (rule-based): top segments by estimated minutes
    seg_map: dict[str, list[dict[str, Any]]] = {}
    all_items: list[dict[str, Any]] = []
    for h in _read_jsonl(out["hourly"]):
        items = h.get("items") or []
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            all_items.append(it)
            seg_key = str(it.get("segment_label") or it.get("segment_id") or "").strip() or "(unknown)"
            seg_map.setdefault(seg_key, []).append(it)

    seg_rank: list[tuple[int, int, str]] = []
    for seg_key, items in seg_map.items():
        minutes = _estimate_segment_minutes(items)
        seg_rank.append((minutes, len(items), seg_key))
    seg_rank.sort(key=lambda x: (-x[0], -x[1], x[2]))
    top_segments = seg_rank[:3]

    # Hourly timeline rendering
    hourly_blocks = list(_read_jsonl(out["hourly"]))

    lines: list[str] = []

    # 1) Cost
    lines.append("## 処理コスト")
    lines.append(f"- **入力**: `{pick.path}`")
    lines.append(f"- **モデル**: `gpt-5-nano`")
    if calls:
        lines.append(f"- **API呼び出し回数**: {calls}")
    if input_tokens or output_tokens:
        lines.append(f"- **トークン**: input {input_tokens:,} / output {output_tokens:,}")
    if total_usd:
        lines.append(f"- **推定コスト**: ${total_usd:.4f}（約 {total_jpy:.0f} 円, 1USD={usd_jpy:g}円換算）")
    else:
        lines.append("- **推定コスト**: （usageが取れていないため未算出）")
    lines.append("")

    # 2) Main work
    lines.append("## 本日のメイン作業")
    if not top_segments:
        lines.append("- （イベント要約が無いため算出不可）")
    else:
        for idx, (minutes, n, seg_key) in enumerate(top_segments, start=1):
            sample = seg_map.get(seg_key) or []
            sample = sorted(sample, key=lambda x: str(x.get("ts") or ""))
            sample_summaries = [str(x.get("summary") or "").strip() for x in sample if str(x.get("summary") or "").strip()]
            sample_summaries = sample_summaries[:2]
            s = f"{idx}. **{seg_key}**（推定 {minutes} 分 / {n} events）"
            lines.append(s)
            for sm in sample_summaries:
                lines.append(f"   - {sm}")
    lines.append("")

    # 3) Hourly timeline
    lines.append("## 1時間ごとのタイムライン")
    if not hourly_blocks:
        lines.append("- （データなし）")
    else:
        for hb in hourly_blocks:
            hs = str(hb.get("hour_start_ts") or "")
            try:
                hdt = _parse_dt(hs)
                label = f"{hdt.strftime('%H:%M')}〜{(hdt + timedelta(hours=1) - timedelta(minutes=1)).strftime('%H:%M')}"
            except Exception:
                label = hs
            lines.append(f"### {label}")
            items = hb.get("items") or []
            if not isinstance(items, list) or not items:
                lines.append("- （イベントなし）")
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                t = _fmt_hhmm(str(it.get("ts") or ""))
                summary = str(it.get("summary") or "").strip()
                if not summary:
                    continue
                ev = it.get("evidence") or []
                ev0 = ""
                if isinstance(ev, list) and ev:
                    ev0 = str(ev[0] or "").strip()
                suffix = f"（{ev0}）" if ev0 else ""
                prefix = f"**{t}**: " if t else ""
                lines.append(f"- {prefix}{summary}{suffix}")
            lines.append("")

    out["report_md"].write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return pick


def _cmd_find_stage02(args: argparse.Namespace) -> None:
    pick = find_stage02(args.date, run_id=args.run_id, prefer=args.prefer)
    print(json.dumps({"date": pick.date, "run_id": pick.run_id, "path": str(pick.path)}, ensure_ascii=False))


def _cmd_extract(args: argparse.Namespace) -> None:
    pick = step_extract_active_ocr(args.date, run_id=args.run_id, prefer=args.prefer)
    out = _out_paths(args.date, pick.run_id)
    print(f"OK: {out['active_ocr']}")


def _cmd_summarize(args: argparse.Namespace) -> None:
    pick = step_summarize_events(
        args.date,
        run_id=args.run_id,
        prefer=args.prefer,
        model=args.model,
        batch_size=args.batch_size,
        max_chars=args.max_chars,
        max_new_events=args.max_new_events,
        timeout_sec=args.timeout_sec,
        sleep_sec=args.sleep_sec,
    )
    out = _out_paths(args.date, pick.run_id)
    print(f"OK: {out['event_summaries']}")


def _cmd_hourly(args: argparse.Namespace) -> None:
    pick = step_build_hourly(args.date, run_id=args.run_id, prefer=args.prefer)
    out = _out_paths(args.date, pick.run_id)
    print(f"OK: {out['hourly']}")


def _cmd_render(args: argparse.Namespace) -> None:
    pick = step_render_md(args.date, run_id=args.run_id, prefer=args.prefer, usd_jpy=args.usd_jpy)
    out = _out_paths(args.date, pick.run_id)
    print(f"OK: {out['report_md']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--date", default="2026-02-06")
        p.add_argument("--run-id", default=None)
        p.add_argument("--prefer", choices=["mtime", "run_id"], default="mtime")

    p0 = sub.add_parser("find-stage02")
    add_common(p0)
    p0.set_defaults(func=_cmd_find_stage02)

    p1 = sub.add_parser("extract-active-ocr")
    add_common(p1)
    p1.set_defaults(func=_cmd_extract)

    p2 = sub.add_parser("summarize-events")
    add_common(p2)
    p2.add_argument("--model", default="gpt-5-nano")
    p2.add_argument("--batch-size", type=int, default=20)
    p2.add_argument("--max-chars", type=int, default=1200)
    p2.add_argument("--max-new-events", type=int, default=0, help="process at most N new events then exit (0=all)")
    p2.add_argument("--timeout-sec", type=int, default=180)
    p2.add_argument("--sleep-sec", type=float, default=0.2)
    p2.set_defaults(func=_cmd_summarize)

    p3 = sub.add_parser("build-hourly")
    add_common(p3)
    p3.set_defaults(func=_cmd_hourly)

    p4 = sub.add_parser("render-md")
    add_common(p4)
    p4.add_argument("--usd-jpy", type=float, default=float(os.environ.get("EVERLOG_USD_JPY") or 150))
    p4.set_defaults(func=_cmd_render)

    args = ap.parse_args(argv)
    try:
        args.func(args)
        return 0
    except BrokenPipeError:
        return 141
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

