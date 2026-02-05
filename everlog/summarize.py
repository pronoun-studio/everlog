# Role: 1日分のJSONLを読み取り、サマリ＋タイムラインのMarkdownを生成する。
# How: セグメント化して推定時間を算出し、必要に応じてLLM付与のラベル/要約を反映する。
# Key functions: `summarize_day_to_markdown()`（外部呼び出し用）
# Collaboration: 入力は `everlog/jsonl.py`、出力先は `everlog/paths.py` を使う。起動は `everlog/cli.py` と `everlog/menubar.py` から行う。
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os
import re

from .jsonl import read_jsonl
from .paths import ensure_dirs, get_paths
from .segments import build_segments, build_segments_with_event_trace
from .timeutil import normalize_date_arg


_CONCRETE_TOKEN_RE = re.compile(
    r"(~\/|/Users/|github\.com|openai\.com|chatgpt\.com|\.py|\.md|\.jsonl?|\.app|2>&1)",
    flags=re.IGNORECASE,
)


def _shorten_token(s: str, max_len: int = 120) -> str:
    s = " ".join((s or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _is_abstract_text(s: str) -> bool:
    """
    Heuristic: treat summaries like 「確認した」「探索した」だけ as abstract
    unless they include concrete anchors (path/url/filename/etc).
    """
    t = (s or "").strip()
    if not t:
        return True
    if _CONCRETE_TOKEN_RE.search(t):
        return False
    abstract_markers = [
        "確認",
        "探索",
        "参照",
        "閲覧",
        "整理",
        "操作",
        "作業",
        "調査",
        "対応",
        "実施",
        "短時間",
        "長時間",
        "データ",
        "情報",
        "関連",
        "追加",
        "小規模",
        "簡易",
        "前段",
        "継続",
    ]
    hits = sum(1 for m in abstract_markers if m in t)
    return hits >= 2


def _evidence_hint(seg_keywords: list[str], seg_snips: list[str], limit: int = 3) -> str:
    """
    Build a short, concrete hint string from OCR-derived tokens/snippets.
    Prioritize paths/URLs/filenames over generic nouns.
    """
    def score(item: str) -> int:
        t = (item or "").strip()
        if not t:
            return -10
        if len(t) <= 2:
            return -10
        if re.fullmatch(r"[•\s←→-]+", t):
            return -10
        if re.fullmatch(r"[A-Z0-9]{1,3}", t):
            return -6

        s = 0
        if "platform.openai.com" in t:
            s += 8
        if "openai.com" in t:
            s += 5
        if "github.com" in t:
            s += 7
        if "chatgpt.com" in t:
            s += 5
        if "2>&1" in t:
            s += 3
        if t.startswith(("/","~/")) or re.search(
            r"(github\.com|openai\.com|chatgpt\.com|calendar\.google\.com)/",
            t,
            flags=re.IGNORECASE,
        ):
            s += 4
        if re.search(r"\.[A-Za-z0-9]{2,5}\b", t):
            s += 2
        if re.search(r"\bREADME\.md\b", t, flags=re.IGNORECASE):
            s += 2

        # Penalize UI-noise heavy strings (many 'x' tabs etc).
        if t.count("x") >= 6 or t.count("✕") >= 3:
            s -= 4
        if len(t) > 140:
            s -= 2
        return s

    def normalize(tok: str) -> str:
        t = (tok or "").strip()
        t = re.sub(r"^[•\s]+", "", t)
        t = re.sub(
            r"^\d+\s+(?=(github\.com|platform\.openai\.com|chatgpt\.com|calendar\.google\.com))",
            "",
            t,
            flags=re.IGNORECASE,
        )
        return t

    candidates: list[tuple[int, int, str]] = []
    seq = list(seg_snips or []) + list(seg_keywords or [])
    for i, raw in enumerate(seq):
        tok = normalize(_shorten_token(raw, max_len=110))
        sc = score(tok)
        if sc <= 0:
            continue
        candidates.append((sc, i, tok))
    candidates.sort(key=lambda x: (-x[0], x[1]))

    out: list[str] = []
    for _sc, _i, tok in candidates:
        if tok in out:
            continue
        out.append(tok)
        if len(out) >= limit:
            break
    return " / ".join(out)


def _day_paths(date: str) -> tuple[Path, Path]:
    p = get_paths()
    run_id = (
        os.environ.get("EVERLOG_OUTPUT_RUN_ID")
        or os.environ.get("EVERYTIMECAPTURE_OUTPUT_RUN_ID")
        or os.environ.get("EVERLOG_TRACE_RUN_ID")
        or os.environ.get("EVERYTIMECAPTURE_TRACE_RUN_ID")
        or ""
    ).strip()
    if run_id:
        out_dir = p.out_dir / date / run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        return p.logs_dir / f"{date}.jsonl", out_dir / f"{date}.md"
    return p.logs_dir / f"{date}.jsonl", p.out_dir / f"{date}.md"


def _llm_path(date: str) -> Path:
    p = get_paths()
    return p.out_dir / f"{date}.llm.json"


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _fmt_hm(total_sec: int) -> str:
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    return f"{h}時間{m}分" if h else f"{m}分"


def _usage_tier(sec: int, max_sec: int) -> str:
    # A simple, stable tier like the screenshot: "最多/多/中/少"
    if max_sec <= 0:
        return "少"
    if sec == max_sec and sec > 0:
        return "最多"
    r = sec / max_sec
    if r >= 0.66:
        return "多"
    if r >= 0.33:
        return "中"
    return "少"


def _load_llm_map(date: str) -> dict[int, dict[str, Any]]:
    path = _llm_path(date)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("segments") or []
    out: dict[int, dict[str, Any]] = {}
    for it in items:
        try:
            sid = int(it.get("segment_id"))
        except Exception:
            continue
        out[sid] = it
    return out


def _trace_dir(date: str, run_id: str) -> Path:
    p = get_paths()
    return p.trace_dir / date / run_id


def _write_trace_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def summarize_day_to_markdown(date_arg: str) -> Path:
    date = normalize_date_arg(date_arg)
    ensure_dirs()
    log_path, out_path = _day_paths(date)

    events = read_jsonl(log_path)
    if not events:
        out_path.write_text(f"# 作業ログ {date}\n\n（ログがありません）\n", encoding="utf-8")
        return out_path

    capture_count = len(events)
    excluded_events = [e for e in events if bool(e.get("excluded"))]
    error_events = [e for e in events if bool(e.get("error"))]
    valid_events = [e for e in events if (not bool(e.get("excluded"))) and (not bool(e.get("error")))]

    dt_list = [d for d in (_parse_ts(str(e.get("ts") or "")) for e in events) if d is not None]
    dt_list_sorted = sorted(dt_list)
    start = dt_list_sorted[0] if dt_list_sorted else None
    end = dt_list_sorted[-1] if dt_list_sorted else None

    interval_sec = int((events[-1].get("interval_sec") if events else 0) or 0) or 300
    trace_enabled = str(os.environ.get("EVERLOG_TRACE") or os.environ.get("EVERYTIMECAPTURE_TRACE") or "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    }
    if trace_enabled:
        run_id = (
            os.environ.get("EVERLOG_TRACE_RUN_ID")
            or os.environ.get("EVERYTIMECAPTURE_TRACE_RUN_ID")
            or datetime.now().astimezone().strftime("%H%M%S-%f")
        )
        segments, event_trace = build_segments_with_event_trace(events, interval_sec)
        _write_trace_jsonl(
            _trace_dir(date, run_id) / "stage-00.raw.jsonl",
            [{"event": e} for e in events],
        )
        _write_trace_jsonl(
            _trace_dir(date, run_id) / "stage-01.entities.jsonl",
            event_trace,
        )
    else:
        segments = build_segments(events, interval_sec)
    total_sec = sum(int(s.duration_sec) for s in segments)

    llm_map = _load_llm_map(date)

    # 集計（メイン作業）: LLMの task_title があればそれを優先
    by_task_sec: Counter[str] = Counter()
    task_summary: dict[str, str] = {}
    task_hint: dict[str, str] = {}
    for s in segments:
        llm = llm_map.get(s.segment_id, {})
        title = str(llm.get("task_title") or "").strip() or s.label
        by_task_sec[title] += int(s.duration_sec)
        if title not in task_summary:
            summ = str(llm.get("task_summary") or "").strip()
            if summ:
                task_summary[title] = summ
        if title not in task_hint:
            hint = _evidence_hint(s.keywords, s.ocr_snippets, limit=2)
            if hint:
                task_hint[title] = hint

    top3 = by_task_sec.most_common(3)

    # 集計（アプリ使用状況）
    by_app_sec: Counter[str] = Counter()
    by_app_caps: Counter[str] = Counter()
    by_app_labels: dict[str, Counter[str]] = defaultdict(Counter)
    for s in segments:
        app = s.active_app or "(unknown)"
        by_app_sec[app] += int(s.duration_sec)
        by_app_caps[app] += int(s.captures)
        by_app_labels[app][s.label] += int(s.duration_sec)

    lines: list[str] = []
    lines.append(f"# 作業ログ {date}")
    lines.append("")
    if start and end:
        try:
            wall = int((end - start).total_seconds())
        except Exception:
            wall = 0
        wall_note = f"（約{_fmt_hm(wall)}）" if wall > 0 else ""
        lines.append(f"- 記録期間: {start.strftime('%H:%M')} 〜 {end.strftime('%H:%M')}{wall_note}")
    else:
        lines.append("- 記録期間: （不明: tsのパースに失敗）")
    lines.append(
        f"- キャプチャ数: {capture_count}回（有効 {len(valid_events)} / 除外 {len(excluded_events)} / 失敗 {len(error_events)}）"
    )
    lines.append(f"- 推定総時間: {_fmt_hm(total_sec)}（有効分のみ）")
    lines.append("")

    lines.append("## 本日のメイン作業（推定）")
    if not top3:
        lines.append("")
        lines.append("（有効なログがありません。スクショ/OCR失敗が多い場合は、Screen Recording権限の付与先を確認してください）")
    else:
        lines.append("")
        for i, (title, sec) in enumerate(top3, start=1):
            mins = sec // 60
            summary = task_summary.get(title, "")
            if summary:
                hint = task_hint.get(title, "")
                if hint and _is_abstract_text(summary):
                    lines.append(f"{i}. {title}（約{mins}分） — {summary}（例: {hint}）")
                else:
                    lines.append(f"{i}. {title}（約{mins}分） — {summary}")
            else:
                lines.append(f"{i}. {title}（約{mins}分）")

    lines.append("")
    lines.append("## アプリ使用状況（推定）")
    lines.append("")
    lines.append("| アプリ | 推定時間 | 使用回数 | 使用傾向 | 主な用途（近似） |")
    lines.append("|---|---:|---:|---|---|")

    max_app_sec = max(by_app_sec.values()) if by_app_sec else 0
    for app, sec in by_app_sec.most_common():
        caps = int(by_app_caps[app])
        tier = _usage_tier(int(sec), int(max_app_sec))
        uses: list[str] = []
        for lbl, _lbl_sec in by_app_labels[app].most_common(2):
            s = str(lbl)
            if s.startswith(app + " / "):
                s = s[len(app) + 3 :]
            uses.append(s)
        lines.append(f"| {app} | {_fmt_hm(int(sec))} | {caps} | {tier} | {' / '.join(uses)} |")

    lines.append("")
    lines.append("## タイムライン（推定）")
    if not segments:
        lines.append("")
        lines.append("（有効なログがありません）")
    else:
        lines.append("")
        for s in segments:
            llm = llm_map.get(s.segment_id, {})
            title = str(llm.get("task_title") or "").strip() or s.label
            summary = str(llm.get("task_summary") or "").strip()
            cat = str(llm.get("category") or "").strip()
            mins = int(s.duration_sec) // 60
            line = f"- {s.start_dt.strftime('%H:%M')}〜{s.end_dt.strftime('%H:%M')} {title}（{mins}分）"
            if cat:
                line += f" [{cat}]"
            hint = _evidence_hint(s.keywords, s.ocr_snippets, limit=3)
            if summary:
                if hint and _is_abstract_text(summary):
                    line += f" — {summary}（例: {hint}）"
                else:
                    line += f" — {summary}"
            elif hint:
                line += f" — {hint}"
            lines.append(line)

    if trace_enabled:
        # stage-02: attach segment_id/label to each event trace
        event_trace_with_segments: list[dict[str, Any]] = []
        for ev in event_trace:
            event_trace_with_segments.append(
                {
                    **ev,
                    "segment_id": ev.get("segment_id"),
                    "segment_label": ev.get("segment_label"),
                }
            )
        _write_trace_jsonl(
            _trace_dir(date, run_id) / "stage-02.segment.jsonl",
            event_trace_with_segments,
        )

        # stage-03: event-level LLM enrichment
        llm_rows: list[dict[str, Any]] = []
        for ev in event_trace_with_segments:
            sid = ev.get("segment_id")
            llm = (llm_map.get(int(sid)) if isinstance(sid, int) else {}) or {}
            llm_rows.append(
                {
                    "event_id": ev.get("event_id"),
                    "segment_id": sid,
                    "task_title": str(llm.get("task_title") or ""),
                    "task_summary": str(llm.get("task_summary") or ""),
                    "category": str(llm.get("category") or ""),
                    "confidence": float(llm.get("confidence") or 0.0),
                }
            )
        _write_trace_jsonl(_trace_dir(date, run_id) / "stage-03.llm.jsonl", llm_rows)

        # stage-04: timeline lines with segment mapping
        timeline_rows: list[dict[str, Any]] = []
        for s in segments:
            llm = llm_map.get(s.segment_id, {})
            title = str(llm.get("task_title") or "").strip() or s.label
            summary = str(llm.get("task_summary") or "").strip()
            cat = str(llm.get("category") or "").strip()
            mins = int(s.duration_sec) // 60
            line = f"- {s.start_dt.strftime('%H:%M')}〜{s.end_dt.strftime('%H:%M')} {title}（{mins}分）"
            if cat:
                line += f" [{cat}]"
            hint = _evidence_hint(s.keywords, s.ocr_snippets, limit=3)
            if summary:
                if hint and _is_abstract_text(summary):
                    line += f" — {summary}（例: {hint}）"
                else:
                    line += f" — {summary}"
            elif hint:
                line += f" — {hint}"
            timeline_rows.append(
                {
                    "segment_id": s.segment_id,
                    "line": line,
                }
            )
        _write_trace_jsonl(_trace_dir(date, run_id) / "stage-04.timeline.jsonl", timeline_rows)

    lines.append("")
    lines.append("## 参考（データ品質）")
    lines.append("")
    lines.append(f"- 除外: {len(excluded_events)}回")
    lines.append(f"- スクショ/OCR失敗: {len(error_events)}回")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path
