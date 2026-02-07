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
import urllib.parse


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
    r"(?<![\w/.-])[\w./-]*\.?(?:[\w./-]+)\.(?:"
    r"py|md|txt|json|jsonl|toml|ya?ml|sh|zsh|bash|ts|js|tsx|jsx|go|rs|swift|java|kt|rb|php|"
    r"csv|tsv|log|env|ini|cfg|conf|sql|sqlite|db|png|jpe?g|gif|webp|pdf|zip|gz|tgz|tar|"
    r"app"
    r")\b",
    flags=re.IGNORECASE,
)
_WORD_RE = re.compile(r"[A-Za-z0-9_./-]{4,}")
_JA_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9faf]{2,}")

_URL_RE = re.compile(r"\bhttps?://[^\s<>()]+", flags=re.IGNORECASE)
_DOMAIN_PATH_RE = re.compile(
    r"\b(?:[A-Za-z0-9-]+\.)+(?:com|net|org|io|ai|app|dev|co|jp)(?:/[^\s<>()]+)?\b",
    flags=re.IGNORECASE,
)
_POSIX_PATH_RE = re.compile(
    r"(?:(?<=\s)|^)(/(?:Users|Applications|System|Volumes|opt|etc|var|tmp|private|Library)/[^\s]+)",
    flags=re.IGNORECASE,
)

# Avoid leaking secrets from OCR in derived summaries/snippets.
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _redact_derived_text(s: str) -> str:
    if not s:
        return ""
    s = _API_KEY_RE.sub("sk-…", s)
    s = _EMAIL_RE.sub("[REDACTED_EMAIL]", s)
    return s


def _normalize_for_entities(text: str) -> str:
    """
    Normalize OCR text for entity extraction.
    - flatten whitespace to help extract URLs/paths split by OCR line breaks
    - fix a few common OCR confusions seen in paths (e.g. `DEVl` instead of `DEV/`)
    """
    t = (text or "").replace("\x00", " ")
    t = re.sub(r"\s+", " ", t).strip()
    # Common OCR: "/" read as "l" or "|" in paths.
    t = re.sub(r"\bDEV[l|](?=[A-Za-z])", "DEV/", t)
    return t


def _extract_posix_paths(text: str) -> list[str]:
    """
    Extract POSIX paths.

    OCR often breaks tokens with newlines (e.g. `Con\\ntents`), so we try both:
    - normalized (whitespace collapsed)
    - newline-stripped (more aggressive, but only used for path extraction)
    """
    t1 = _normalize_for_entities(text)
    t2 = (text or "").replace("\x00", " ").replace("\n", "")
    t2 = re.sub(r"\bDEV[l|](?=[A-Za-z])", "DEV/", t2)
    hits = _POSIX_PATH_RE.findall(t1) + _POSIX_PATH_RE.findall(t2)
    out: list[str] = []
    for p in hits:
        if p and p not in out:
            out.append(p)
    # Drop clearly-truncated prefixes when a longer path exists (common with OCR line breaks).
    filtered: list[str] = []
    for p in out:
        longer_exists = any((q != p and q.startswith(p) and len(q) > len(p)) for q in out)
        last = p.rsplit("/", 1)[-1]
        if longer_exists and (len(last) <= 3 or last in {"Con", "Cont", "Conte"}):
            continue
        filtered.append(p)
    return filtered


def extract_event_features(
    text: str,
    *,
    keyword_limit: int = 8,
    snippet_limit: int = 3,
    snippet_max_len: int = 120,
) -> dict[str, Any]:
    """
    Event-level features derived from OCR text.
    """
    normalized = _normalize_for_entities(text)
    urls = _extract_url_like(normalized)
    paths = _extract_posix_paths(text)
    return {
        "urls": urls,
        "paths": paths,
    }


def _shorten_token(s: str, max_len: int = 80) -> str:
    s = " ".join((s or "").split())
    s = _redact_derived_text(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _shorten_path(p: str, max_len: int = 80) -> str:
    s = _redact_derived_text(p)
    s = re.sub(r"^/Users/[^/]+/", "~/", s)
    if len(s) <= max_len:
        return s
    # Keep the tail since it's usually the most meaningful for paths.
    return "…" + s[-(max_len - 1) :]


def _extract_url_like(text_flat: str) -> list[str]:
    hits: list[str] = []
    for u in _URL_RE.findall(text_flat):
        hits.append(u)
    for dp in _DOMAIN_PATH_RE.findall(text_flat):
        # Avoid treating `.app/...` inside filesystem paths as a web domain.
        if f"/{dp}" in text_flat:
            continue
        hits.append(dp)

    out: list[str] = []
    for raw in hits:
        s = raw.rstrip(").,;:】】】】")
        # Fix common OCR concatenations like `Gplatform.openai.com...`
        for anchor in ("platform.openai.com", "calendar.google.com", "chatgpt.com", "github.com"):
            if anchor in s and not s.startswith(anchor):
                s = s[s.index(anchor) :]
                break
        # Trim a few common "must end here" anchors to avoid trailing UI noise.
        if "platform.openai.com" in s and "/api-keys" in s:
            s = s[: s.index("/api-keys") + len("/api-keys")]
        # Prefer showing host/path for URLs to avoid noisy params.
        try:
            if s.lower().startswith(("http://", "https://")):
                parsed = urllib.parse.urlparse(s)
                host = parsed.netloc
                path = parsed.path or ""
                if host:
                    s2 = host + path
                    if s2 and s2 not in out:
                        out.append(s2)
                    continue
        except Exception:
            pass
        if s and s not in out:
            out.append(s)
    return out


def _score_snippet_candidate(s: str) -> int:
    if not s:
        return -10
    s = s.strip()
    if not s:
        return -10
    if re.fullmatch(r"[•\s←→-]+", s):
        return -10
    if re.fullmatch(r"[A-Z0-9]{1,3}", s):
        return -6
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return -6

    score = 0
    if _URL_RE.search(s) or _DOMAIN_PATH_RE.search(s):
        score += 6
    if _POSIX_PATH_RE.search(s):
        score += 7
    if _FILE_TOKEN_RE.search(s):
        score += 4
    if "2>&1" in s or "stderr" in s or "stdout" in s:
        score += 2
    if "github.com" in s:
        score += 3
    if "openai.com" in s:
        score += 3
    if _JA_RE.search(s):
        score += 1

    # Prefer moderately-sized lines.
    if 16 <= len(s) <= 180:
        score += 1
    if len(s) > 220:
        score -= 2
    return score


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


def _event_display_texts(e: dict[str, Any]) -> tuple[str, str, bool]:
    """
    Return (primary_text, reference_text) based on active display first.
    - primary_text: active display OCR if available and not excluded.
    - reference_text: OCR from other non-excluded displays.
    Fallbacks:
    - If no active display OCR, primary_text falls back to all non-excluded displays.
    - If no ocr_by_display, falls back to legacy ocr_text.
    """
    active_display = e.get("active_display")
    active_display_text = str(e.get("ocr_active_display_text") or "")
    active_display_excluded = e.get("ocr_active_display_excluded")

    ocr_by_display = e.get("ocr_by_display") or []
    active_text = ""
    other_texts: list[str] = []
    if isinstance(ocr_by_display, list) and ocr_by_display:
        for item in ocr_by_display:
            if not isinstance(item, dict):
                continue
            if item.get("excluded", False):
                continue
            t = str(item.get("ocr_text") or "")
            if not t:
                continue
            try:
                d = int(item.get("display", 0) or 0)
            except Exception:
                d = 0
            if active_display is not None and d == int(active_display):
                active_text = t
            else:
                other_texts.append(t)

    # Prefer explicit active display OCR if not excluded.
    used_active_display = False
    if active_display_text and active_display_excluded is False:
        primary_text = active_display_text
        used_active_display = True
    elif active_text and active_display_excluded is not True:
        primary_text = active_text
        used_active_display = True
    else:
        # fallback: all non-excluded display texts
        primary_text = "\n\n".join(other_texts).strip()

    reference_text = "\n\n".join(other_texts).strip()
    if not primary_text:
        # legacy fallback
        legacy = str(e.get("ocr_text") or "").strip()
        if legacy:
            primary_text = legacy
            reference_text = ""

    return primary_text, reference_text, used_active_display


def _event_display_entries(e: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return per-display OCR entries with normalized text.
    Each entry contains display number, normalized ocr_text, and active display flag.
    """
    active_display_raw = e.get("active_display")
    try:
        active_display = int(active_display_raw)
    except Exception:
        active_display = None

    entries: list[dict[str, Any]] = []
    ocr_by_display = e.get("ocr_by_display") or []
    if isinstance(ocr_by_display, list):
        for item in ocr_by_display:
            if not isinstance(item, dict):
                continue
            display_raw = item.get("display")
            try:
                display_num = int(display_raw)
                display_out: Any = display_num
            except Exception:
                display_num = None
                display_out = display_raw

            text = str(item.get("ocr_text") or "")
            normalized = _normalize_for_entities(text)
            if display_out is None and not normalized:
                continue
            is_active = bool(display_num is not None and active_display is not None and display_num == active_display)
            entries.append(
                {
                    "display": display_out,
                    "ocr_text": normalized,
                    "is_active_display": is_active,
                }
            )
    return entries


def _event_primary_source(
    e: dict[str, Any],
    primary_text: str,
    reference_text: str,
    used_active_display: bool,
) -> str:
    """
    Returns one of: active_display | fallback_all_displays | fallback_empty
    """
    if primary_text:
        if used_active_display:
            return "active_display"
        if reference_text:
            return "fallback_all_displays"
        legacy = str(e.get("ocr_text") or "").strip()
        if legacy:
            return "fallback_all_displays"
    return "fallback_empty"


def _event_ocr_text(e: dict[str, Any]) -> str:
    # Backward-compatible helper: primary text only.
    primary_text, _reference_text, _used_active = _event_display_texts(e)
    return primary_text


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
    text = _normalize_for_entities(text)
    hits: list[str] = []
    hits.extend(_FILE_TOKEN_RE.findall(text))
    hits.extend(_extract_posix_paths(text))
    hits.extend(_extract_url_like(text))
    # Useful non-file tokens frequently present in logs.
    if "2>&1" in text:
        hits.append("2>&1")
    if "platform.openai.com" in text:
        hits.append("platform.openai.com")
    if "chatgpt.com" in text:
        hits.append("chatgpt.com")
    if "github.com" in text:
        hits.append("github.com")

    if not hits:
        hits.extend(_WORD_RE.findall(text))
        hits.extend(_JA_RE.findall(text))
    if not hits:
        return []
    c = Counter(_shorten_token(h, max_len=80) for h in hits if h)
    return [k for k, _ in c.most_common(limit) if k]


def _extract_snippets(text: str, limit: int = 3, max_len: int = 120) -> list[str]:
    if not text:
        return []
    norm = _normalize_for_entities(text)

    out: list[str] = []
    # First: directly extract high-value entities (robust against OCR line breaks).
    paths = [_shorten_path(p, max_len=max_len) for p in _extract_posix_paths(text)]
    urls = [_shorten_token(u, max_len=max_len) for u in _extract_url_like(norm)]

    def push(item: str) -> None:
        if not item:
            return
        if item in out:
            return
        out.append(item)

    # Diversity first: URL/context + local path, then fill.
    if urls:
        push(urls[0])
    if paths:
        push(paths[0])
    for item in (urls[1:] + paths[1:]):
        push(item)
        if len(out) >= limit:
            break

    # Then: pick informative lines.
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    scored: list[tuple[int, int, str]] = []
    for idx, ln in enumerate(lines):
        score = _score_snippet_candidate(ln)
        if score <= 0:
            continue
        scored.append((score, idx, ln))
    scored.sort(key=lambda x: (-x[0], x[1]))

    picked_idx: set[int] = set()
    for _score, idx, ln in scored:
        if idx in picked_idx:
            continue
        s = _shorten_token(ln, max_len=max_len)
        if not s or s in out:
            continue
        out.append(s)
        picked_idx.add(idx)
        if len(out) >= limit:
            break

    if not out:
        # Last resort: pick the first non-noise line.
        for ln in lines:
            if _score_snippet_candidate(ln) < 0:
                continue
            s = _shorten_token(ln, max_len=max_len)
            if s:
                out.append(s)
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
        primary_text, _reference_text, _used_active = _event_display_texts(e)
        feats = extract_event_features(primary_text)
        kws = feats.get("keywords") or []
        snips = feats.get("snippets") or []

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


def build_segments_with_event_trace(
    events: list[dict[str, Any]], default_interval_sec: int
) -> tuple[list[Segment], list[dict[str, Any]]]:
    """
    Build segments and return per-event trace records with segment_id mapping.
    """
    # error/excluded は除外
    valid = [
        e for e in events if (not bool(e.get("excluded"))) and (not bool(e.get("error")))
    ]

    interval_sec = int(default_interval_sec or 0) or 300
    gap_threshold = max(120, int(interval_sec * 2.5))

    # sort by ts
    valid_sorted: list[tuple[datetime, dict[str, Any]]] = []
    for e in valid:
        dt = _parse_ts(str(e.get("ts") or ""))
        if dt:
            valid_sorted.append((dt, e))
    valid_sorted.sort(key=lambda x: x[0])

    segs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None

    # event traces collected with placeholder segment index
    event_traces: list[dict[str, Any]] = []

    for dt, e in valid_sorted:
        dur = int(e.get("interval_sec") or 0) or interval_sec
        app = str(e.get("active_app") or "").strip()
        dom = _domain_from_event(e).strip()
        title = _shorten(_title_from_event(e), max_len=80)
        label = _label_from_event(e)
        primary_text, reference_text, used_active = _event_display_texts(e)
        feats = extract_event_features(primary_text)
        primary_source = _event_primary_source(e, primary_text, reference_text, used_active)
        display_entries = _event_display_entries(e)

        key = (app, dom, title or "")

        new_segment = (
            cur is None
            or key != cur["key"]
            or (dt - cur["last_dt"]).total_seconds() > gap_threshold
        )
        if new_segment:
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
                "keywords": Counter(feats.get("keywords") or []),
                "snippets": Counter(feats.get("snippets") or []),
                "event_ids": [str(e.get("id") or "")],
            }
        else:
            cur["last_dt"] = dt
            cur["last_dur"] = dur
            cur["duration_sec"] += dur
            cur["captures"] += 1
            cur["keywords"].update(feats.get("keywords") or [])
            cur["snippets"].update(feats.get("snippets") or [])
            cur["event_ids"].append(str(e.get("id") or ""))

        event_traces.append(
            {
                "event_id": str(e.get("id") or ""),
                "ts": str(e.get("ts") or ""),
                "active_app": app,
                "window_title": str(e.get("window_title") or ""),
                "domain": dom,
                "segment_key": key,
                "primary_source": primary_source,
                "ocr_by_display": display_entries,
                **feats,
            }
        )

    if cur is not None:
        segs.append(cur)

    segments: list[Segment] = []
    seg_id_by_event: dict[str, int] = {}
    for i, s in enumerate(segs):
        end_dt = s["last_dt"] + timedelta(seconds=int(s["last_dur"] or interval_sec))
        kws = [k for k, _ in s["keywords"].most_common(8) if k]
        snips = [k for k, _ in s["snippets"].most_common(3) if k]
        segments.append(
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
        for eid in s["event_ids"]:
            if eid:
                seg_id_by_event[eid] = i

    # attach segment_id and label to each event trace
    labeled_event_traces: list[dict[str, Any]] = []
    for ev in event_traces:
        seg_id = seg_id_by_event.get(ev.get("event_id", ""))
        label = ""
        if seg_id is not None and seg_id < len(segments):
            label = segments[seg_id].label
        labeled_event_traces.append(
            {
                **ev,
                "segment_id": seg_id,
                "segment_label": label,
            }
        )

    return segments, labeled_event_traces
