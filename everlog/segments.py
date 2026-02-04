# Role: JSONLイベントを「作業セグメント」にまとめ、LLM入力に使える特徴量を抽出する。
# How: 連続するイベントを app/domain/title の近似キーでまとめ、OCRから短いキーワード/スニペットを抽出する。
# Key functions: `build_segments()`
# Collaboration: `summarize.py` と `enrich.py` が共通利用する。
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import re


@dataclass(frozen=True)
class Segment:
    segment_id: int
    start_dt: datetime
    end_dt: datetime
    duration_sec: int
    captures: int
    active_app: str
    domain: str
    window_title: str
    label: str
    keywords: list[str]
    ocr_snippets: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_ts": self.start_dt.isoformat(),
            "end_ts": self.end_dt.isoformat(),
            "duration_sec": self.duration_sec,
            "captures": self.captures,
            "active_app": self.active_app,
            "domain": self.domain,
            "window_title": self.window_title,
            "label": self.label,
            "keywords": self.keywords,
            "ocr_snippets": self.ocr_snippets,
        }


_FILE_TOKEN_RE = re.compile(
    r"\b[\w./-]+\.(?:py|md|txt|json|toml|ya?ml|sh|zsh|bash|ts|js|tsx|jsx|go|rs|swift|java|kt|rb|php)\b",
    flags=re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9_./-]{4,}")
_JA_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf]{2,}")


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _domain_from_event(e: dict[str, Any]) -> str:
    b = e.get("browser") or {}
    if isinstance(b, dict):
        return str(b.get("domain") or "")
    return ""


def _title_from_event(e: dict[str, Any]) -> str:
    return str(e.get("window_title") or "").strip()


def _shorten(s: str, max_len: int = 80) -> str:
    s = " ".join((s or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _label_from_event(e: dict[str, Any]) -> str:
    app = str(e.get("active_app") or "").strip()
    dom = _domain_from_event(e).strip()
    title = _shorten(_title_from_event(e), max_len=80)
    parts: list[str] = []
    if app:
        parts.append(app)
    if dom and dom not in parts:
        parts.append(dom)
    if title and title not in parts:
        parts.append(title)
    return " / ".join(parts) if parts else "(unknown)"


def _extract_keywords(text: str, limit: int = 8) -> list[str]:
    if not text:
        return []
    text = text.replace("\x00", " ")
    hits: list[str] = []
    hits.extend(_FILE_TOKEN_RE.findall(text))
    if not hits:
        hits.extend(_WORD_RE.findall(text))
        hits.extend(_JA_RE.findall(text))
    if not hits:
        return []
    c = Counter(hits)
    return [k for k, _ in c.most_common(limit)]


def _extract_snippets(text: str, limit: int = 3, max_len: int = 120) -> list[str]:
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[str] = []
    for ln in lines[: limit * 2]:
        s = _shorten(ln, max_len=max_len)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def build_segments(events: list[dict[str, Any]], default_interval_sec: int) -> list[Segment]:
    # error/excluded は除外
    valid = [
        e for e in events if (not bool(e.get("excluded"))) and (not bool(e.get("error")))
    ]

    interval_sec = int(default_interval_sec or 0) or 300
    gap_threshold = max(120, int(interval_sec * 2.5))

    # sort by ts
    valid_sorted = []
    for e in valid:
        dt = _parse_ts(str(e.get("ts") or ""))
        if dt:
            valid_sorted.append((dt, e))
    valid_sorted.sort(key=lambda x: x[0])

    segs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    for dt, e in valid_sorted:
        dur = int(e.get("interval_sec") or 0) or interval_sec
        app = str(e.get("active_app") or "").strip()
        dom = _domain_from_event(e).strip()
        title = _shorten(_title_from_event(e), max_len=80)
        label = _label_from_event(e)
        ocr_text = str(e.get("ocr_text") or "")
        kws = _extract_keywords(ocr_text)
        snips = _extract_snippets(ocr_text)

        key = (app, dom, title or "")

        if (
            cur is None
            or key != cur["key"]
            or (dt - cur["last_dt"]).total_seconds() > gap_threshold
        ):
            if cur is not None:
                segs.append(cur)
            cur = {
                "key": key,
                "start_dt": dt,
                "last_dt": dt,
                "last_dur": dur,
                "duration_sec": dur,
                "captures": 1,
                "active_app": app,
                "domain": dom,
                "window_title": title,
                "label": label,
                "keywords": Counter(kws),
                "snippets": Counter(snips),
            }
        else:
            cur["last_dt"] = dt
            cur["last_dur"] = dur
            cur["duration_sec"] += dur
            cur["captures"] += 1
            cur["keywords"].update(kws)
            cur["snippets"].update(snips)

    if cur is not None:
        segs.append(cur)

    out: list[Segment] = []
    for i, s in enumerate(segs):
        end_dt = s["last_dt"] + timedelta(seconds=int(s["last_dur"] or interval_sec))
        kws = [k for k, _ in s["keywords"].most_common(8) if k]
        snips = [k for k, _ in s["snippets"].most_common(3) if k]
        out.append(
            Segment(
                segment_id=i,
                start_dt=s["start_dt"],
                end_dt=end_dt,
                duration_sec=int(s["duration_sec"]),
                captures=int(s["captures"]),
                active_app=s["active_app"] or "(unknown)",
                domain=s["domain"] or "",
                window_title=s["window_title"] or "",
                label=s["label"] or "(unknown)",
                keywords=kws,
                ocr_snippets=snips,
            )
        )
    return out
