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

from .jsonl import read_jsonl
from .paths import ensure_dirs, get_paths
from .segments import build_segments
from .timeutil import normalize_date_arg


def _day_paths(date: str) -> tuple[Path, Path]:
    p = get_paths()
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
    segments = build_segments(events, interval_sec)
    total_sec = sum(int(s.duration_sec) for s in segments)

    llm_map = _load_llm_map(date)

    # 集計（メイン作業）: LLMの task_title があればそれを優先
    by_task_sec: Counter[str] = Counter()
    task_summary: dict[str, str] = {}
    for s in segments:
        llm = llm_map.get(s.segment_id, {})
        title = str(llm.get("task_title") or "").strip() or s.label
        by_task_sec[title] += int(s.duration_sec)
        if title not in task_summary:
            summ = str(llm.get("task_summary") or "").strip()
            if summ:
                task_summary[title] = summ

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
            if summary:
                line += f" — {summary}"
            lines.append(line)

    lines.append("")
    lines.append("## 参考（データ品質）")
    lines.append("")
    lines.append(f"- 除外: {len(excluded_events)}回")
    lines.append(f"- スクショ/OCR失敗: {len(error_events)}回")

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_path
