# Role: 1日分のJSONLを読み取り、サマリ＋タイムラインのMarkdownを生成する。
# How: セグメント化して推定時間を算出し、必要に応じてLLM付与のラベル/要約を反映する。
# Key functions: `summarize_day_to_markdown()`（外部呼び出し用）
# Collaboration: 入力は `everlog/jsonl.py`、出力先は `everlog/paths.py` を使う。起動は `everlog/cli.py` と `everlog/menubar.py` から行う。
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
import difflib
import json
import os
import re
import socket
import time
from urllib.parse import urlparse

from typing import Callable

from .jsonl import read_jsonl
from .config import load_config
from .paths import ensure_dirs, get_paths
from .segments import build_segments, build_segments_with_event_trace
from .llm import LlmError, analyze_day_summary, analyze_hour_blocks, calc_cost_usd
from .safety import sanitize_markdown_for_sharing, sanitize_text_for_sharing
from .timeutil import make_run_id, normalize_date_arg

# Progress callback type: (percent: int, stage: str) -> None
ProgressCallback = Callable[[int, str], None]


_CONCRETE_TOKEN_RE = re.compile(
    r"(~\/|/Users/|github\.com|openai\.com|chatgpt\.com|\.py|\.md|\.jsonl?|\.app|2>&1)",
    flags=re.IGNORECASE,
)
_FILENAME_FORBIDDEN_RE = re.compile(r'[\\/:*?"<>|]')


def _shorten_token(s: str, max_len: int = 120) -> str:
    s = " ".join((s or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def _shorten_text(s: str, max_len: int = 80) -> str:
    s = " ".join((s or "").split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


_SIMILARITY_STRIP_RE = re.compile(r"[\s\u3000\u3001\u3002.,!?！？:：;；\-–—/\\()（）「」『』【】\[\]{}<>＜＞\"'・]")


def _normalize_for_similarity(s: str) -> str:
    return _SIMILARITY_STRIP_RE.sub("", (s or "").strip().lower())


def _is_near_duplicate(a: str, b: str, threshold: float = 0.88) -> bool:
    na = _normalize_for_similarity(a)
    nb = _normalize_for_similarity(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 12 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def _sanitize_title_for_filename(title: str, max_len: int = 80) -> str:
    t = " ".join((title or "").split())
    if not t:
        return ""
    t = _FILENAME_FORBIDDEN_RE.sub("-", t)
    t = re.sub(r"\s+", " ", t).strip().strip(" .")
    if len(t) > max_len:
        t = t[:max_len].rstrip()
    return t


def _md_output_name(date: str, title: str, out_suffix: str) -> str:
    short_date = date[2:] if len(date) >= 8 else date
    safe_title = _sanitize_title_for_filename(title) or "作業ログ"
    base = f"{short_date}_{safe_title}"
    return f"{base}{out_suffix}.md"


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
    if not run_id:
        run_id = make_run_id()
    out_suffix = (
        os.environ.get("EVERLOG_OUTPUT_MD_SUFFIX")
        or os.environ.get("EVERYTIMECAPTURE_OUTPUT_MD_SUFFIX")
        or ""
    ).strip()
    out_dir = p.out_dir / date / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = _md_output_name(date, "作業ログ", out_suffix)
    return p.logs_dir / f"{date}.jsonl", out_dir / out_name


def _llm_path(date: str, *, run_id: str | None = None) -> Path:
    """Segment-LLM output path. Always store under out/<date>/<run_id>/."""
    p = get_paths()
    rid = (run_id if run_id is not None else _current_run_id_for_outputs()).strip()
    if not rid:
        rid = make_run_id()
    out_dir = p.out_dir / date / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{date}.llm.json"


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


def _context_label_from_event(e: dict[str, Any]) -> str:
    app = str(e.get("active_app") or "").strip()
    dom = _domain_from_event(e).strip()
    title = _shorten_text(str(e.get("window_title") or ""), max_len=80)
    parts: list[str] = []
    if app:
        parts.append(app)
    if dom and dom not in parts:
        parts.append(dom)
    if title and title not in parts:
        parts.append(title)
    return " / ".join(parts) if parts else "(unknown)"


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


def _extract_usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int] | None:
    if not isinstance(usage, dict):
        return None
    if "input_tokens" in usage or "output_tokens" in usage:
        try:
            return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        except Exception:
            return None
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        try:
            return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        except Exception:
            return None
    return None


def _extract_usage_tokens_full(usage: dict[str, Any] | None) -> tuple[int, int, int] | None:
    """
    Returns (input_tokens, output_tokens, cached_input_tokens).
    cached_input_tokens is 0 when not present.
    """
    base = _extract_usage_tokens(usage)
    if not base:
        return None
    input_tokens, output_tokens = base
    cached_tokens = 0
    try:
        details = usage.get("input_tokens_details") if isinstance(usage, dict) else None
        if isinstance(details, dict):
            cached_tokens = int(details.get("cached_tokens") or 0)
    except Exception:
        cached_tokens = 0
    if cached_tokens < 0:
        cached_tokens = 0
    if cached_tokens > input_tokens:
        cached_tokens = input_tokens
    return input_tokens, output_tokens, cached_tokens


def _fmt_int(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _fmt_usd(cost: float | None) -> str:
    if not isinstance(cost, (int, float)):
        return "n/a"
    return f"${float(cost):.6f}"


def _fmt_jpy_from_usd(cost: float | None, *, usd_to_jpy: float = 150.0) -> str:
    if not isinstance(cost, (int, float)):
        return "n/a"
    try:
        jpy = int(round(float(cost) * float(usd_to_jpy)))
    except Exception:
        return "n/a"
    return f"¥{jpy:,}"


def _fmt_cost(cost: float | None) -> str:
    # Display both USD and JPY for readability.
    return f"{_fmt_usd(cost)} / {_fmt_jpy_from_usd(cost)}"


def _load_llm_map(date: str, *, run_id: str | None = None) -> dict[int, dict[str, Any]]:
    path = _llm_path(date, run_id=run_id)
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


def _load_hourly_llm_map(date: str, *, run_id: str | None = None) -> dict[str, dict[str, Any]]:
    path = _hourly_llm_path(date, run_id=run_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data.get("hours") or []
    out: dict[str, dict[str, Any]] = {}
    for it in items:
        key = str(it.get("hour_start_ts") or "").strip()
        if key:
            out[key] = it
    return out


def _load_hourly_llm_meta(date: str, *, run_id: str | None = None) -> dict[str, Any]:
    path = _hourly_llm_path(date, run_id=run_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, Any] = {}
    usage = data.get("usage")
    if isinstance(usage, dict):
        out["usage"] = usage
    if "cost_usd" in data:
        out["cost_usd"] = data.get("cost_usd")
    model = data.get("model")
    if isinstance(model, str) and model:
        out["model"] = model
    return out


def _load_daily_llm(date: str, *, run_id: str | None = None) -> dict[str, Any]:
    path = _daily_llm_path(date, run_id=run_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _trace_dir(date: str, run_id: str) -> Path:
    p = get_paths()
    return p.trace_dir / date / run_id


def _trace_stage_max() -> int | None:
    raw = os.environ.get("EVERLOG_TRACE_STAGE_MAX") or os.environ.get(
        "EVERYTIMECAPTURE_TRACE_STAGE_MAX"
    )
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except Exception:
        return None


def _write_trace_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_trace_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _hourly_llm_enabled() -> bool:
    return str(
        os.environ.get("EVERLOG_HOURLY_LLM")
        or os.environ.get("EVERYTIMECAPTURE_HOURLY_LLM")
        or ""
    ).strip() in {"1", "true", "TRUE", "yes", "YES"}


def _hourly_llm_min_sec() -> int:
    raw = os.environ.get("EVERLOG_HOURLY_LLM_MIN_SEC") or os.environ.get(
        "EVERYTIMECAPTURE_HOURLY_LLM_MIN_SEC"
    )
    try:
        v = int(raw) if raw is not None else 120
    except Exception:
        v = 120
    return max(0, v)


def _hourly_llm_max_hours() -> int:
    raw = os.environ.get("EVERLOG_HOURLY_LLM_MAX_HOURS") or os.environ.get(
        "EVERYTIMECAPTURE_HOURLY_LLM_MAX_HOURS"
    )
    try:
        v = int(raw) if raw is not None else 24
    except Exception:
        v = 24
    return max(0, v)


def _daily_llm_enabled() -> bool:
    raw = str(
        os.environ.get("EVERLOG_DAILY_LLM")
        or os.environ.get("EVERYTIMECAPTURE_DAILY_LLM")
        or ""
    ).strip()
    if raw == "":
        # Default: daily summary follows hourly LLM toggle.
        return _hourly_llm_enabled()
    return raw in {"1", "true", "TRUE", "yes", "YES"}


def _normalize_common_text(text: str) -> str:
    t = " ".join((text or "").split()).lower().strip()
    if not t:
        return ""
    t = re.sub(r"[\"'“”‘’（）()【】\[\]<>]", "", t)
    t = re.sub(r"[、。.!?！？・|•▶→]+", "", t)
    t = re.sub(r"\s+", "", t)
    # Stronger near-dup for long OCR blocks: keep only a prefix so tiny diffs don't explode variants.
    if len(t) > 240:
        t = t[:240]
    return t


def _current_run_id_for_outputs() -> str:
    return (
        os.environ.get("EVERLOG_OUTPUT_RUN_ID")
        or os.environ.get("EVERYTIMECAPTURE_OUTPUT_RUN_ID")
        or os.environ.get("EVERLOG_TRACE_RUN_ID")
        or os.environ.get("EVERYTIMECAPTURE_TRACE_RUN_ID")
        or ""
    ).strip()


def _hourly_llm_path(date: str, *, run_id: str | None = None) -> Path:
    """
    Hour-level LLM output path.

    Always store under out/<date>/<run_id>/ to keep outputs versioned.
    If run_id is not provided, generate one.
    """
    p = get_paths()
    rid = (run_id if run_id is not None else _current_run_id_for_outputs()).strip()
    if not rid:
        rid = make_run_id()
    out_dir = p.out_dir / date / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{date}.hourly.llm.json"


def _daily_llm_path(date: str, *, run_id: str | None = None) -> Path:
    """Always store under out/<date>/<run_id>/."""
    p = get_paths()
    rid = (run_id if run_id is not None else _current_run_id_for_outputs()).strip()
    if not rid:
        rid = make_run_id()
    out_dir = p.out_dir / date / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{date}.daily.llm.json"


def _hour_enrich_llm_path(date: str, *, run_id: str | None = None) -> Path:
    """Hour-enrich LLM output path. Always store under out/<date>/<run_id>/."""
    p = get_paths()
    rid = (run_id if run_id is not None else _current_run_id_for_outputs()).strip()
    if not rid:
        rid = make_run_id()
    out_dir = p.out_dir / date / rid
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{date}.hour-enrich.llm.json"


def _hour_enrich_llm_enabled() -> bool:
    """Check if hour-enrich LLM is enabled (requires daily LLM to be enabled)."""
    # hour-enrich は daily_llm が有効な場合のみ動作
    # 追加で EVERLOG_HOUR_ENRICH_LLM=0 で無効化可能
    if not _daily_llm_enabled():
        return False
    v = str(
        os.environ.get("EVERLOG_HOUR_ENRICH_LLM")
        or os.environ.get("EVERYTIMECAPTURE_HOUR_ENRICH_LLM")
        or "1"  # デフォルトで有効
    ).strip()
    return v not in {"0", "false", "FALSE", "no", "NO"}


def _load_hour_enrich_llm(date: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Load hour-enrich LLM cache if available."""
    path = _hour_enrich_llm_path(date, run_id=run_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_hour_enrich_llm_map(date: str, *, run_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Load hour-enrich LLM results as a map keyed by hour_start_ts."""
    data = _load_hour_enrich_llm(date, run_id=run_id)
    hours = data.get("hours") or []
    if not isinstance(hours, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for h in hours:
        if not isinstance(h, dict):
            continue
        key = str(h.get("hour_start_ts") or "").strip()
        if key:
            out[key] = h
    return out


def _segment_key_label(segment_key: list[Any]) -> str:
    parts = [str(p).strip() for p in (segment_key or []) if str(p).strip()]
    return " / ".join(parts) if parts else "(unknown)"


def _hour_pack_cluster_labels(hour_pack: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for c in hour_pack.get("clusters") or []:
        label = _segment_key_label(c.get("segment_key") or [])
        if label and label not in out:
            out.append(label)
    return out


def _hour_pack_observation(hour_pack: dict[str, Any]) -> str:
    for c in hour_pack.get("clusters") or []:
        for ev in c.get("active_timeline") or []:
            t = str(ev.get("ocr_text") or "").strip()
            if t:
                return _shorten_text(t, max_len=80)
    for t in hour_pack.get("hour_common_texts") or []:
        s = str(t or "").strip()
        if s:
            return _shorten_text(s, max_len=80)
    return ""


def _build_hourly_llm_input(hour_packs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in hour_packs:
        clusters: list[dict[str, Any]] = []
        for c in h.get("clusters") or []:
            clusters.append(
                {
                    "segment_key": c.get("segment_key") or [],
                    "segment_ids": c.get("segment_ids") or [],
                    "active_timeline": c.get("active_timeline") or [],
                }
            )
        if not clusters and not (h.get("hour_common_texts") or []):
            continue
        out.append(
            {
                "hour_start_ts": h.get("hour_start_ts"),
                "hour_end_ts": h.get("hour_end_ts"),
                "hour_common_texts": h.get("hour_common_texts") or [],
                "clusters": clusters,
            }
        )
    return out


def _is_retryable_llm_error(err: LlmError) -> bool:
    """Return True for transient failures worth retrying once or twice."""
    msg = str(err or "").lower()
    retryable_tokens = (
        "timed out",
        "timeout",
        "tempor",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "service unavailable",
        "connection reset",
        "broken pipe",
    )
    return any(tok in msg for tok in retryable_tokens)


def _llm_timeout_sec() -> int:
    raw = (
        os.environ.get("EVERLOG_LLM_TIMEOUT_SEC")
        or os.environ.get("EVERYTIMECAPTURE_LLM_TIMEOUT_SEC")
        or ""
    ).strip()
    try:
        timeout = int(raw) if raw else 180
    except Exception:
        timeout = 180
    # Keep a sane lower bound to avoid too aggressive timeouts.
    return max(30, timeout)


def _llm_api_host_port() -> tuple[str, int]:
    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").strip()
    if not base:
        base = "https://api.openai.com/v1"
    parsed = urlparse(base)
    host = parsed.hostname or "api.openai.com"
    if parsed.port:
        return host, int(parsed.port)
    if parsed.scheme == "http":
        return host, 80
    return host, 443


def _llm_network_reachable(timeout_sec: float = 2.0) -> bool:
    host, port = _llm_api_host_port()
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except Exception:
        return False


def _log_llm_retry(stage: str, attempt: int, action: str, detail: str = "") -> None:
    msg = f"[summarize][{stage}] attempt {attempt}/3 {action}"
    if detail:
        msg += f": {detail}"
    print(msg)


def _maybe_run_hourly_llm(date: str, hour_packs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not _hourly_llm_enabled():
        return None
    min_sec = _hourly_llm_min_sec()
    max_hours = _hourly_llm_max_hours()

    eligible = [h for h in hour_packs if int(h.get("active_sec_est") or 0) >= min_sec]
    if not eligible:
        return None

    if max_hours > 0 and len(eligible) > max_hours:
        eligible = sorted(
            eligible,
            key=lambda h: (-(int(h.get("active_sec_est") or 0)), str(h.get("hour_start_ts") or "")),
        )[:max_hours]
        eligible = sorted(eligible, key=lambda h: str(h.get("hour_start_ts") or ""))

    api_key = os.environ.get("OPENAI_API_KEY", "")
    model_name = (
        os.environ.get("EVERLOG_HOURLY_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_HOURLY_LLM_MODEL")
        or os.environ.get("EVERLOG_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_LLM_MODEL")
        or "gpt-5-nano"
    )
    payload = _build_hourly_llm_input(eligible)
    if not payload:
        return None
    # Retry transient API/network failures so one flaky call does not disable timeline LLM.
    res = None
    for attempt in range(3):
        try:
            res = analyze_hour_blocks(
                date,
                payload,
                model_name,
                api_key,
                timeout_sec=_llm_timeout_sec(),
            )
            if attempt > 0:
                _log_llm_retry("hour-llm", attempt + 1, "succeeded")
            break
        except LlmError as e:
            err_msg = str(e)
            if attempt >= 2:
                _log_llm_retry("hour-llm", attempt + 1, "stopped", "max attempts reached")
                return None
            if not _is_retryable_llm_error(e):
                _log_llm_retry("hour-llm", attempt + 1, "stopped", f"non-retryable error ({err_msg})")
                return None
            if not _llm_network_reachable():
                _log_llm_retry("hour-llm", attempt + 1, "stopped", "network unreachable for retry")
                return None
            wait_sec = 1.0 + float(attempt)
            _log_llm_retry("hour-llm", attempt + 1, "retrying", f"wait {wait_sec:.1f}s ({err_msg})")
            time.sleep(wait_sec)
    if res is None:
        return None
    cost_usd = calc_cost_usd(res.usage, res.model)
    return {
        "date": date,
        "model": res.model,
        "generated_at": datetime.now().astimezone().isoformat(),
        "hour_count": len(payload),
        "usage": res.usage,
        "cost_usd": cost_usd,
        "hours": res.data.get("hours") or [],
    }


def _maybe_run_daily_llm(
    date: str,
    *,
    hour_packs: list[dict[str, Any]],
    hourly_llm_map: dict[str, dict[str, Any]],
    run_id: str,
) -> dict[str, Any] | None:
    if not _daily_llm_enabled():
        return None
    if not hour_packs:
        return None

    # Build a compact per-hour list (one row per hour) to summarize the whole day.
    hours_in: list[dict[str, Any]] = []
    for h in hour_packs:
        hour_key = str(h.get("hour_start_ts") or "").strip()
        if not hour_key:
            continue
        llm_h = hourly_llm_map.get(hour_key) or {}
        title = str(llm_h.get("hour_title") or "").strip()
        summary = str(llm_h.get("hour_summary") or "").strip()
        if not title:
            labels = _hour_pack_cluster_labels(h)
            title = labels[0] if labels else _hour_pack_observation(h)
        if not summary:
            summary = _hour_pack_observation(h)
        active_min = int(h.get("active_sec_est") or 0) // 60
        if not title and not summary:
            continue
        hours_in.append(
            {
                "hour_start_ts": hour_key,
                "hour_end_ts": str(h.get("hour_end_ts") or ""),
                "active_min_est": active_min,
                "hour_title": title,
                "hour_summary": _shorten_token(summary, max_len=240),
            }
        )

    if not hours_in:
        return None

    api_key = os.environ.get("OPENAI_API_KEY", "")
    model_name = (
        os.environ.get("EVERLOG_DAILY_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_DAILY_LLM_MODEL")
        or os.environ.get("EVERLOG_HOURLY_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_HOURLY_LLM_MODEL")
        or os.environ.get("EVERLOG_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_LLM_MODEL")
        or "gpt-5-nano"
    )

    res = None
    for attempt in range(3):
        try:
            res = analyze_day_summary(
                date,
                hours_in,
                model_name,
                api_key,
                timeout_sec=_llm_timeout_sec(),
            )
            if attempt > 0:
                _log_llm_retry("daily-llm", attempt + 1, "succeeded")
            break
        except LlmError as e:
            err_msg = str(e)
            if attempt >= 2:
                _log_llm_retry("daily-llm", attempt + 1, "stopped", "max attempts reached")
                return None
            if not _is_retryable_llm_error(e):
                _log_llm_retry("daily-llm", attempt + 1, "stopped", f"non-retryable error ({err_msg})")
                return None
            if not _llm_network_reachable():
                _log_llm_retry("daily-llm", attempt + 1, "stopped", "network unreachable for retry")
                return None
            wait_sec = 1.0 + float(attempt)
            _log_llm_retry("daily-llm", attempt + 1, "retrying", f"wait {wait_sec:.1f}s ({err_msg})")
            time.sleep(wait_sec)
    if res is None:
        return None
    cost_usd = calc_cost_usd(res.usage, res.model)
    data = res.data if isinstance(res.data, dict) else {}
    return {
        "date": date,
        "model": res.model,
        "generated_at": datetime.now().astimezone().isoformat(),
        "hour_count": len(hours_in),
        "usage": res.usage,
        "cost_usd": cost_usd,
        "daily": data,
        "run_id": run_id,
    }


def _maybe_run_hour_enrich_llm(
    date: str,
    *,
    daily_llm: dict[str, Any],
    hourly_llm_map: dict[str, dict[str, Any]],
    hour_packs: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any] | None:
    """
    daily contextを踏まえて各時間帯の目的・意味を再解釈する。
    daily_llm と hourly_llm_map が必要。
    """
    if not _hour_enrich_llm_enabled():
        return None
    if not daily_llm:
        return None
    if not hourly_llm_map:
        return None

    # daily context を抽出
    daily = daily_llm.get("daily") if isinstance(daily_llm, dict) else None
    if not isinstance(daily, dict):
        return None
    daily_title = str(daily.get("daily_title") or "").strip()
    daily_summary = str(daily.get("daily_summary") or "").strip()
    if not daily_title and not daily_summary:
        return None

    daily_context = {
        "daily_title": daily_title,
        "daily_summary": daily_summary,
    }

    # hours overview を構築
    hours_overview: list[dict[str, Any]] = []
    for h in hour_packs:
        hour_key = str(h.get("hour_start_ts") or "").strip()
        if not hour_key:
            continue
        llm_h = hourly_llm_map.get(hour_key) or {}
        title = str(llm_h.get("hour_title") or "").strip()
        summary = str(llm_h.get("hour_summary") or "").strip()
        if not title:
            labels = _hour_pack_cluster_labels(h)
            title = labels[0] if labels else ""
        if not title and not summary:
            continue
        hours_overview.append(
            {
                "hour_start_ts": hour_key,
                "hour_end_ts": str(h.get("hour_end_ts") or ""),
                "hour_title": title,
                "hour_summary": _shorten_token(summary, max_len=200),
            }
        )

    if not hours_overview:
        return None

    api_key = os.environ.get("OPENAI_API_KEY", "")
    model_name = (
        os.environ.get("EVERLOG_HOUR_ENRICH_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_HOUR_ENRICH_LLM_MODEL")
        or os.environ.get("EVERLOG_DAILY_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_DAILY_LLM_MODEL")
        or os.environ.get("EVERLOG_LLM_MODEL")
        or os.environ.get("EVERYTIMECAPTURE_LLM_MODEL")
        or "gpt-5-nano"
    )

    from .llm import enrich_hours_with_context

    res = None
    for attempt in range(3):
        try:
            res = enrich_hours_with_context(
                date,
                daily_context,
                hours_overview,
                model_name,
                api_key,
                timeout_sec=_llm_timeout_sec(),
            )
            if attempt > 0:
                _log_llm_retry("hour-enrich-llm", attempt + 1, "succeeded")
            break
        except LlmError as e:
            err_msg = str(e)
            if attempt >= 2:
                _log_llm_retry("hour-enrich-llm", attempt + 1, "stopped", "max attempts reached")
                return None
            if not _is_retryable_llm_error(e):
                _log_llm_retry(
                    "hour-enrich-llm", attempt + 1, "stopped", f"non-retryable error ({err_msg})"
                )
                return None
            if not _llm_network_reachable():
                _log_llm_retry("hour-enrich-llm", attempt + 1, "stopped", "network unreachable for retry")
                return None
            wait_sec = 1.0 + float(attempt)
            _log_llm_retry(
                "hour-enrich-llm", attempt + 1, "retrying", f"wait {wait_sec:.1f}s ({err_msg})"
            )
            time.sleep(wait_sec)
    if res is None:
        return None

    cost_usd = calc_cost_usd(res.usage, res.model)
    data = res.data if isinstance(res.data, dict) else {}
    return {
        "date": date,
        "model": res.model,
        "generated_at": datetime.now().astimezone().isoformat(),
        "hour_count": len(hours_overview),
        "usage": res.usage,
        "cost_usd": cost_usd,
        "hours": data.get("hours") or [],
        "run_id": run_id,
    }


def _build_hour_packs(
    events: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
    default_interval_sec: int,
) -> list[dict[str, Any]]:
    interval_sec = int(default_interval_sec or 0) or 300

    event_interval: dict[str, int] = {}
    for e in events:
        eid = str(e.get("id") or "")
        if not eid:
            continue
        dur = int(e.get("interval_sec") or 0) or interval_sec
        event_interval[eid] = dur

    def _bucket(dt: datetime) -> datetime:
        return dt.replace(minute=0, second=0, microsecond=0)

    buckets: dict[datetime, dict[str, Any]] = {}
    for e in events:
        dt = _parse_ts(str(e.get("ts") or ""))
        if dt is None:
            continue
        hour_start = _bucket(dt)
        bucket = buckets.setdefault(
            hour_start,
            {
                "hour_start": hour_start,
                "active_sec_est": 0,
                "common_counts": Counter(),
                "common_samples": {},
                "_common_seen": set(),
                "clusters": {},
            },
        )
        dur = int(e.get("interval_sec") or 0) or interval_sec
        # Active time estimation keeps excluded/error events (same behavior as before).
        bucket["active_sec_est"] += dur

    for seg in segment_rows:
        segment_id = seg.get("segment_id")
        segment_key = seg.get("segment_key") or []
        for disp in seg.get("ocr_by_display") or []:
            if not isinstance(disp, dict):
                continue
            display = disp.get("display")
            common_texts = [str(t or "").strip() for t in (disp.get("common_texts") or [])]
            for ev in disp.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                ts = str(ev.get("ts") or "")
                dt = _parse_ts(ts)
                if dt is None:
                    continue
                hour_start = _bucket(dt)
                bucket = buckets.setdefault(
                    hour_start,
                    {
                        "hour_start": hour_start,
                        "active_sec_est": 0,
                        "common_counts": Counter(),
                        "common_samples": {},
                        "_common_seen": set(),
                        "clusters": {},
                    },
                )
                seen = bucket.get("_common_seen")
                if not isinstance(seen, set):
                    seen = set()
                    bucket["_common_seen"] = seen
                # A1: count common texts once per hour for each (segment_id, display, norm_text)
                for text in common_texts:
                    norm = _normalize_common_text(text)
                    if not norm:
                        continue
                    key = (segment_id, display, norm)
                    if key in seen:
                        continue
                    seen.add(key)
                    bucket["common_counts"][norm] += 1
                    if norm not in bucket["common_samples"]:
                        bucket["common_samples"][norm] = text

                if not bool(ev.get("is_active_display")):
                    continue
                key = tuple(segment_key)
                cluster = bucket["clusters"].get(key)
                if cluster is None:
                    cluster = {
                        "segment_key": list(segment_key),
                        "segment_ids": set(),
                        "active_events": [],
                        "active_event_ids": set(),
                        "active_sec": 0,
                    }
                    bucket["clusters"][key] = cluster
                if isinstance(segment_id, int):
                    cluster["segment_ids"].add(segment_id)
                event_id = str(ev.get("event_id") or "")
                ocr_text = str(ev.get("ocr_text") or "").strip()
                cluster["active_events"].append((dt, event_id, segment_id, ts, ocr_text))
                if event_id and event_id not in cluster["active_event_ids"]:
                    cluster["active_event_ids"].add(event_id)
                    cluster["active_sec"] += int(event_interval.get(event_id) or interval_sec)

    rows: list[dict[str, Any]] = []
    for hour_start, b in sorted(buckets.items(), key=lambda x: x[0]):
        hour_end = hour_start.replace(minute=59, second=59, microsecond=0)

        common_items = list(b["common_counts"].items())
        common_items.sort(
            key=lambda kv: (-kv[1], -len(str(b["common_samples"].get(kv[0], ""))))
        )
        hour_common_texts = [
            str(b["common_samples"].get(norm, "")).strip()
            for norm, _ in common_items
            if str(b["common_samples"].get(norm, "")).strip()
        ]
        if len(hour_common_texts) > 20:
            hour_common_texts = hour_common_texts[:20]

        clusters_out: list[dict[str, Any]] = []
        clusters_sorted = sorted(
            b["clusters"].values(),
            key=lambda c: (-int(c.get("active_sec") or 0), _segment_key_label(c.get("segment_key") or [])),
        )
        for cluster in clusters_sorted[:3]:
            active_events = cluster.get("active_events") or []
            active_events.sort(key=lambda x: (x[0], x[1]))
            seen_eids: set[str] = set()
            timeline: list[dict[str, Any]] = []
            for _dt, eid, sid, ts, text in active_events:
                if eid and eid in seen_eids:
                    continue
                if eid:
                    seen_eids.add(eid)
                if not str(text or "").strip():
                    continue
                timeline.append({"ts": ts, "segment_id": sid, "ocr_text": text})
            if not timeline:
                continue
            clusters_out.append(
                {
                    "segment_key": cluster.get("segment_key") or [],
                    "segment_ids": sorted([sid for sid in (cluster.get("segment_ids") or []) if isinstance(sid, int)]),
                    "active_timeline": timeline,
                }
            )

        rows.append(
            {
                "hour_start_ts": hour_start.isoformat(),
                "hour_end_ts": hour_end.isoformat(),
                "active_sec_est": int(b.get("active_sec_est") or 0),
                "hour_common_texts": hour_common_texts,
                "clusters": clusters_out,
            }
        )
    return rows


def _build_segment_groups(event_trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _split_ocr_sentences(text: str) -> list[str]:
        t = " ".join((text or "").split())
        if not t:
            return []
        for ch in ("▶", "→", "・", "|", "•"):
            t = t.replace(ch, "。")
        parts = re.split(r"(?<=[。.!?！？])\s+", t)
        out = [p.strip() for p in parts if p.strip()]
        if not out:
            out = [c for c in t.split(" ") if c]
        return out

    def _normalize_sentence_for_dedupe(text: str) -> str:
        t = " ".join((text or "").split()).lower().strip()
        if not t:
            return ""
        t = re.sub(r"[\"'“”‘’（）()【】\[\]<>]", "", t)
        t = re.sub(r"[、。.!?！？・|•▶→]+", "", t)
        return t

    grouped: dict[int, dict[str, Any]] = {}
    for ev in event_trace:
        sid = ev.get("segment_id")
        if not isinstance(sid, int):
            continue
        ts = str(ev.get("ts") or "")
        dt = _parse_ts(ts)
        seg = grouped.get(sid)
        if seg is None:
            seg = {
                "segment_id": sid,
                "segment_key": ev.get("segment_key") or [],
                "hour_start_ts": ts,
                "hour_end_ts": ts,
                "_start_dt": dt,
                "_end_dt": dt,
                "_events": [],
            }
            grouped[sid] = seg
        if dt is not None:
            if seg["_start_dt"] is None or dt < seg["_start_dt"]:
                seg["_start_dt"] = dt
                seg["hour_start_ts"] = ts
            if seg["_end_dt"] is None or dt > seg["_end_dt"]:
                seg["_end_dt"] = dt
                seg["hour_end_ts"] = ts
        seg["_events"].append(ev)

    rows: list[dict[str, Any]] = []
    for sid, seg in sorted(grouped.items(), key=lambda x: x[0]):
        display_info: dict[Any, dict[str, Any]] = {}
        for ev in seg["_events"]:
            for disp in ev.get("ocr_by_display") or []:
                if not isinstance(disp, dict):
                    continue
                display = disp.get("display")
                text = str(disp.get("ocr_text") or "").strip()
                if not text:
                    continue
                sentences = _split_ocr_sentences(text)
                if not sentences:
                    continue
                info = display_info.get(display)
                if info is None:
                    info = {
                        "display": display,
                        "_sent_counts": Counter(),
                        "_sent_example": {},
                    }
                    display_info[display] = info
                norm_set: set[str] = set()
                for s in sentences:
                    norm = _normalize_sentence_for_dedupe(s)
                    if not norm:
                        continue
                    if norm in norm_set:
                        continue
                    norm_set.add(norm)
                    info["_sent_counts"][norm] += 1
                    if norm not in info["_sent_example"]:
                        info["_sent_example"][norm] = s

        displays: list[dict[str, Any]] = []
        for display, info in display_info.items():
            freq_norms = {n for n, c in info["_sent_counts"].items() if c >= 2}
            common_texts = [
                info["_sent_example"][n]
                for n, _ in info["_sent_counts"].most_common()
                if n in freq_norms
            ]
            seen_global: set[str] = set()
            events_out: list[dict[str, Any]] = []
            for ev in seg["_events"]:
                disp_match: dict[str, Any] | None = None
                for disp in ev.get("ocr_by_display") or []:
                    if not isinstance(disp, dict):
                        continue
                    if disp.get("display") == display:
                        disp_match = disp
                        break
                if disp_match is None:
                    continue
                text = str(disp_match.get("ocr_text") or "").strip()
                if not text:
                    continue
                sentences = _split_ocr_sentences(text)
                new_sentences: list[str] = []
                for s in sentences:
                    norm = _normalize_sentence_for_dedupe(s)
                    if not norm:
                        continue
                    if norm in freq_norms:
                        continue
                    if norm in seen_global:
                        continue
                    seen_global.add(norm)
                    new_sentences.append(s)
                if not new_sentences and sentences:
                    fallback = sentences[0]
                    new_sentences = [fallback]
                    norm = _normalize_sentence_for_dedupe(fallback)
                    if norm:
                        seen_global.add(norm)
                if new_sentences:
                    events_out.append(
                        {
                            "event_id": str(ev.get("event_id") or ""),
                            "ts": str(ev.get("ts") or ""),
                            "is_active_display": bool(disp_match.get("is_active_display")),
                            "ocr_text": " / ".join(new_sentences),
                        }
                    )
            display_entry: dict[str, Any] = {"display": display, "events": events_out}
            if common_texts:
                display_entry["common_texts"] = common_texts[:20]
            if events_out or common_texts:
                displays.append(display_entry)

        rows.append(
            {
                "segment_id": sid,
                "segment_key": seg.get("segment_key") or [],
                "hour_start_ts": seg.get("hour_start_ts") or "",
                "hour_end_ts": seg.get("hour_end_ts") or "",
                "ocr_by_display": displays,
            }
        )
    return rows


def summarize_day_to_markdown(
    date_arg: str,
    *,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Generate a markdown report for the specified date.

    Args:
        date_arg: Date string (e.g. "today", "2026-02-09")
        progress_callback: Optional callback (percent, stage_name) for progress updates.
            - 0%: stage-00 開始（raw data読込）
            - 10%: stage-01 完了（entities抽出）
            - 20%: stage-03 完了（segment化）
            - 30%: stage-04 完了（hour-pack）
            - 50%: stage-05 hour-llm 実行中
            - 70%: stage-06 daily-llm 実行中
            - 85%: stage-07 hour-enrich-llm 実行中
            - 100%: マークダウン生成完了
    """

    def _progress(percent: int, stage: str) -> None:
        if progress_callback:
            progress_callback(percent, stage)

    date = normalize_date_arg(date_arg)
    ensure_dirs()
    p = get_paths()
    log_path = p.logs_dir / f"{date}.jsonl"

    cfg = load_config()
    safe_md_raw = str(
        os.environ.get("EVERLOG_SAFE_MARKDOWN")
        or os.environ.get("EVERYTIMECAPTURE_SAFE_MARKDOWN")
        or ""
    ).strip()
    safe_md_enabled = safe_md_raw not in {"0", "false", "FALSE", "no", "NO"}

    def _safe(s: str) -> str:
        if not safe_md_enabled:
            return s
        return sanitize_text_for_sharing(s, cfg)

    trace_enabled = str(
        os.environ.get("EVERLOG_TRACE") or os.environ.get("EVERYTIMECAPTURE_TRACE") or ""
    ).strip() in {"1", "true", "TRUE", "yes", "YES"}
    trace_stage_max = _trace_stage_max() if trace_enabled else None
    trace_run_id = (
        (
            os.environ.get("EVERLOG_TRACE_RUN_ID")
            or os.environ.get("EVERYTIMECAPTURE_TRACE_RUN_ID")
            or ""
        ).strip()
        if trace_enabled
        else ""
    )
    if trace_enabled and not trace_run_id:
        trace_run_id = make_run_id()

    # Output run-id: prefer explicit OUTPUT_RUN_ID; fallback to trace_run_id for trace runs.
    output_run_id = (
        os.environ.get("EVERLOG_OUTPUT_RUN_ID")
        or os.environ.get("EVERYTIMECAPTURE_OUTPUT_RUN_ID")
        or ""
    ).strip()
    if trace_enabled and not output_run_id:
        output_run_id = trace_run_id
    # Always write versioned markdown under out/<date>/<run_id>/ for diffing across runs.
    # If caller didn't provide a run_id, generate a stable, human-readable one: h-mm-<seq>.
    if not output_run_id:
        output_run_id = make_run_id()

    out_suffix = (
        os.environ.get("EVERLOG_OUTPUT_MD_SUFFIX")
        or os.environ.get("EVERYTIMECAPTURE_OUTPUT_MD_SUFFIX")
        or ""
    ).strip()
    out_dir = p.out_dir / date / output_run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # NOTE: out/<date>.md への「latest」コピーは廃止。常に out/<date>/<run_id>/ に格納する。

    _progress(0, "stage-00: ログ読込中")
    events = read_jsonl(log_path)
    if not events:
        out_name = _md_output_name(date, "作業ログ", out_suffix)
        out_path = out_dir / out_name
        out_path.write_text(f"# 作業ログ {date}\n\n（ログがありません）\n", encoding="utf-8")
        _progress(100, "完了（ログなし）")
        return out_path

    capture_count = len(events)
    excluded_events = [e for e in events if bool(e.get("excluded"))]
    error_events = [e for e in events if bool(e.get("error"))]
    valid_events = [e for e in events if (not bool(e.get("excluded"))) and (not bool(e.get("error")))]

    dt_list = [d for d in (_parse_ts(str(e.get("ts") or "")) for e in events) if d is not None]
    dt_list_sorted = sorted(dt_list)
    start = dt_list_sorted[0] if dt_list_sorted else None
    end = dt_list_sorted[-1] if dt_list_sorted else None

    _progress(10, "stage-01: 特徴抽出中")
    interval_sec = int((events[-1].get("interval_sec") if events else 0) or 0) or 300
    segments, event_trace = build_segments_with_event_trace(events, interval_sec)
    _progress(20, "stage-03: セグメント化中")
    segment_rows = _build_segment_groups(event_trace)
    _progress(30, "stage-04: hour-pack作成中")
    hour_packs = _build_hour_packs(events, segment_rows, interval_sec)
    if trace_enabled:
        run_dir = _trace_dir(date, trace_run_id)
        _write_trace_json(
            run_dir / "run.json",
            {
                "run_id": trace_run_id,
                "source": "summarize",
                "started_at": datetime.now().astimezone().isoformat(),
            },
        )
        if trace_stage_max is None or trace_stage_max >= 0:
            _write_trace_jsonl(
                run_dir / "stage-00.raw.jsonl",
                [{"event": e} for e in events],
            )
        if trace_stage_max is None or trace_stage_max >= 1:
            _write_trace_jsonl(
                run_dir / "stage-01.entities.jsonl",
                event_trace,
            )
        if trace_stage_max is None or trace_stage_max >= 3:
            _write_trace_jsonl(
                run_dir / "stage-03.segment.jsonl",
                segment_rows,
            )
        if trace_stage_max is None or trace_stage_max >= 4:
            _write_trace_jsonl(
                run_dir / "stage-04.hour-pack.jsonl",
                hour_packs,
            )
    total_sec_valid = sum(int(s.duration_sec) for s in segments)
    total_sec_all = sum(int(h.get("active_sec_est") or 0) for h in hour_packs)

    # segment-llm (enrich) is optional; default is OFF to avoid stale enrich outputs
    # affecting the report and to keep the default pipeline cost low.
    use_segment_llm = str(os.environ.get("EVERLOG_USE_SEGMENT_LLM") or "").strip()
    if use_segment_llm == "":
        use_segment_llm = "0"
    use_segment_llm_enabled = use_segment_llm in {"1", "true", "TRUE", "yes", "YES"}
    llm_map = _load_llm_map(date, run_id=output_run_id) if use_segment_llm_enabled else {}
    hourly_llm_required = _hourly_llm_enabled()
    hourly_llm_map = _load_hourly_llm_map(date, run_id=output_run_id)
    hourly_llm_meta = _load_hourly_llm_meta(date, run_id=output_run_id)
    if hourly_llm_required and not hourly_llm_map:
        _progress(50, "stage-05: hour-llm 実行中")
        hourly_out = _maybe_run_hourly_llm(date, hour_packs)
        if hourly_out:
            _hourly_llm_path(date, run_id=output_run_id).write_text(
                json.dumps(hourly_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            hourly_llm_map = _load_hourly_llm_map(date, run_id=output_run_id)
            hourly_llm_meta = _load_hourly_llm_meta(date, run_id=output_run_id)
    hourly_llm_missing = bool(hourly_llm_required and hour_packs and not hourly_llm_map)

    daily_llm = _load_daily_llm(date, run_id=output_run_id)
    if _daily_llm_enabled() and not daily_llm and hour_packs:
        _progress(70, "stage-06: daily-llm 実行中")
        daily_out = _maybe_run_daily_llm(
            date,
            hour_packs=hour_packs,
            hourly_llm_map=hourly_llm_map,
            run_id=output_run_id,
        )
        if daily_out:
            _daily_llm_path(date, run_id=output_run_id).write_text(
                json.dumps(daily_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            daily_llm = _load_daily_llm(date, run_id=output_run_id)

    # hour-enrich LLM: daily contextを踏まえて各時間帯の目的・意味を再解釈
    hour_enrich_llm_map = _load_hour_enrich_llm_map(date, run_id=output_run_id)
    hour_enrich_llm_meta: dict[str, Any] = {}
    # If cache exists, load meta (usage/model) too so the markdown "LLM使用量" shows correct status.
    if _hour_enrich_llm_enabled() and hour_enrich_llm_map:
        try:
            meta = _load_hour_enrich_llm(date, run_id=output_run_id)
            if isinstance(meta, dict):
                hour_enrich_llm_meta = meta
        except Exception:
            hour_enrich_llm_meta = {}
    if _hour_enrich_llm_enabled() and not hour_enrich_llm_map and daily_llm and hourly_llm_map:
        _progress(85, "stage-07: hour-enrich-llm 実行中")
        hour_enrich_out = _maybe_run_hour_enrich_llm(
            date,
            daily_llm=daily_llm,
            hourly_llm_map=hourly_llm_map,
            hour_packs=hour_packs,
            run_id=output_run_id,
        )
        if hour_enrich_out:
            _hour_enrich_llm_path(date, run_id=output_run_id).write_text(
                json.dumps(hour_enrich_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            hour_enrich_llm_map = _load_hour_enrich_llm_map(date, run_id=output_run_id)
            hour_enrich_llm_meta = _load_hour_enrich_llm(date, run_id=output_run_id)

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
    # ヘッダー: yy-mm-dd_daily_title 形式（daily_titleがあれば）
    daily_for_title = daily_llm.get("daily") if isinstance(daily_llm, dict) else None
    daily_title_for_header = (
        str(daily_for_title.get("daily_title") or "").strip()
        if isinstance(daily_for_title, dict)
        else ""
    )
    if daily_title_for_header:
        daily_title_for_header = " ".join(_safe(daily_title_for_header).split())
    short_date = date[2:]  # "2026-02-07" → "26-02-07"
    title_for_filename = daily_title_for_header or "作業ログ"
    out_name = _md_output_name(date, title_for_filename, out_suffix)
    out_path = out_dir / out_name
    if daily_title_for_header:
        lines.append(f"# {short_date}_{daily_title_for_header}")
    else:
        lines.append(f"# 作業ログ {date}")
    lines.append("")
    if hourly_llm_missing:
        lines.append("⚠️ 未完成: 1時間LLM要約が無効/失敗のため、タイムラインは不完全です。")
        lines.append("   （`EVERLOG_HOURLY_LLM=1` と `OPENAI_API_KEY` を設定してください）")
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
    if total_sec_all and total_sec_all >= total_sec_valid:
        lines.append(
            f"- 推定総時間: {_fmt_hm(total_sec_valid)}（有効分のみ） / {_fmt_hm(total_sec_all)}（除外・失敗も含む）"
        )
    else:
        lines.append(f"- 推定総時間: {_fmt_hm(total_sec_valid)}（有効分のみ）")
    # LLM usage/cost breakdown (per call site + total)
    lines.append("- LLM使用量（内訳）:")
    seg_cost = None
    seg_tokens = None
    seg_model = ""
    if use_segment_llm_enabled:
        try:
            seg_llm_path = _llm_path(date, run_id=output_run_id)
            if seg_llm_path.exists():
                seg_data = json.loads(seg_llm_path.read_text(encoding="utf-8"))
                seg_usage = seg_data.get("usage") if isinstance(seg_data, dict) else None
                seg_model = str(seg_data.get("model") or "") if isinstance(seg_data, dict) else ""
                seg_tokens = _extract_usage_tokens_full(seg_usage)
                seg_cost = calc_cost_usd(seg_usage, seg_model) if seg_model else None
        except Exception:
            seg_tokens = None
            seg_cost = None
            seg_model = ""

    hour_usage = hourly_llm_meta.get("usage") if isinstance(hourly_llm_meta, dict) else None
    hour_model = str(hourly_llm_meta.get("model") or "") if isinstance(hourly_llm_meta, dict) else ""
    hour_tokens = _extract_usage_tokens_full(hour_usage)
    hour_cost = calc_cost_usd(hour_usage, hour_model) if hour_model else None

    day_usage = daily_llm.get("usage") if isinstance(daily_llm, dict) else None
    day_model = str(daily_llm.get("model") or "") if isinstance(daily_llm, dict) else ""
    day_tokens = _extract_usage_tokens_full(day_usage)
    day_cost = calc_cost_usd(day_usage, day_model) if day_model else None

    # hour-enrich LLM usage
    enrich_usage = hour_enrich_llm_meta.get("usage") if isinstance(hour_enrich_llm_meta, dict) else None
    enrich_model = str(hour_enrich_llm_meta.get("model") or "") if isinstance(hour_enrich_llm_meta, dict) else ""
    enrich_tokens = _extract_usage_tokens_full(enrich_usage)
    enrich_cost = calc_cost_usd(enrich_usage, enrich_model) if enrich_model else None

    def _line(label: str, tokens: tuple[int, int, int] | None, cost: float | None, model: str) -> str:
        if not tokens:
            return f"  - {label}: 未実行"
        inp, outp, cached = tokens
        cached_note = f"（cached { _fmt_int(cached) }）" if cached else ""
        model_note = f" / model {model}" if model else ""
        return (
            f"  - {label}: input {_fmt_int(inp)}{cached_note} / output {_fmt_int(outp)} tokens"
            f"（cost: {_fmt_cost(cost)}）{model_note}"
        )

    # Map call sites to user-visible sections.
    lines.append(
        _line(
            "segment-llm（任意: enrich）",
            seg_tokens if use_segment_llm_enabled else None,
            seg_cost if use_segment_llm_enabled else None,
            seg_model,
        )
    )
    lines.append(_line("hour-llm（タイムライン用）", hour_tokens, hour_cost, hour_model))
    lines.append(_line("daily-llm（総括用）", day_tokens, day_cost, day_model))
    lines.append(_line("hour-enrich-llm（目的・意味付け）", enrich_tokens, enrich_cost, enrich_model))

    total_in = 0
    total_out = 0
    total_cost = 0.0
    any_cost = False
    for t, c in (
        (seg_tokens, seg_cost),
        (hour_tokens, hour_cost),
        (day_tokens, day_cost),
        (enrich_tokens, enrich_cost),
    ):
        if t:
            total_in += int(t[0])
            total_out += int(t[1])
        if isinstance(c, (int, float)):
            total_cost += float(c)
            any_cost = True
    lines.append(
        f"  - 合計: input {_fmt_int(total_in)} / output {_fmt_int(total_out)} tokens（cost: {_fmt_cost(total_cost if any_cost else None)}）"
    )
    lines.append("")

    lines.append("## 本日のメイン作業（総括）")
    lines.append("")
    daily = daily_llm.get("daily") if isinstance(daily_llm, dict) else None
    if isinstance(daily, dict) and any(str(daily.get(k) or "").strip() for k in ("daily_title", "daily_summary")):
        daily_title = _safe(str(daily.get("daily_title") or "").strip())
        daily_summary = _safe(str(daily.get("daily_summary") or "").strip())
        highlights = [
            _safe(str(s or "").strip())
            for s in (daily.get("highlights") or [])
            if str(s or "").strip()
        ]
        if daily_title:
            lines.append(f"- 推定: {daily_title}")
        if daily_summary:
            lines.append(f"- 概要: {daily_summary}")
        if highlights:
            lines.append("")
            lines.append("### ハイライト")
            lines.append("")
            for h in highlights[:5]:
                lines.append(f"- {h}")
    else:
        # Fallback: compact list of hour titles in order (rule-based)
        items: list[str] = []
        highlights_fb: list[str] = []
        for h in hour_packs:
            hour_key = str(h.get("hour_start_ts") or "")
            llm_h = hourly_llm_map.get(hour_key) or {}
            title = str(llm_h.get("hour_title") or "").strip()
            summary = str(llm_h.get("hour_summary") or "").strip()
            if not title:
                labels = _hour_pack_cluster_labels(h)
                title = labels[0] if labels else ""
            if title and title not in items:
                items.append(title)
            # Build simple highlight candidates (short, unique).
            if summary:
                s = _shorten_text(summary, max_len=90)
                if s and s not in highlights_fb:
                    highlights_fb.append(s)
        if items:
            lines.append("- 推定: " + " / ".join(items[:6]))
            # Provide a lightweight overview + highlights even when daily-llm is missing.
            overview_src = highlights_fb[0] if highlights_fb else ""
            if not overview_src:
                overview_src = _hour_pack_observation(hour_packs[0]) if hour_packs else ""
            if overview_src:
                lines.append(f"- 概要: {overview_src}")
            lines.append("")
            lines.append("### ハイライト")
            lines.append("")
            for h in items[:5]:
                lines.append(f"- {h}")
        else:
            lines.append("（有効なログがありません。スクショ/OCR失敗が多い場合は、Screen Recording権限の付与先を確認してください）")

    lines.append("")

    # Segment-level summaries (from enrich / segment-llm): only show when explicitly enabled.
    if use_segment_llm_enabled:
        lines.append("## セグメント要約（LLM）")
        lines.append("")
        if llm_map:
            # Show top segments by duration as human-friendly titles/summaries.
            segs_sorted = sorted(segments, key=lambda s: (-int(s.duration_sec), int(s.segment_id)))
            shown = 0
            for s in segs_sorted:
                llm = llm_map.get(s.segment_id, {})
                title = _safe(str(llm.get("task_title") or "").strip())
                summ = _safe(str(llm.get("task_summary") or "").strip())
                if not title and not summ:
                    continue
                mins = int(s.duration_sec) // 60
                if title and summ:
                    lines.append(f"- {title}（約{mins}分）— {summ}")
                elif title:
                    hint = _evidence_hint(s.keywords, s.ocr_snippets, limit=2)
                    hint = _safe(hint) if hint else hint
                    lines.append(f"- {title}（約{mins}分）" + (f"（例: {hint}）" if hint else ""))
                else:
                    lines.append(f"- {s.label}（約{mins}分）— {summ}")
                shown += 1
                if shown >= 10:
                    break
            if shown == 0:
                lines.append("（segment-llmの出力はありますが、表示対象が見つかりませんでした）")
        else:
            lines.append("（segment-llmが有効ですが、enrich出力が見つかりませんでした）")
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
    lines.append("## タイムライン（推定・1時間）")
    if not hour_packs:
        lines.append("")
        lines.append("（有効なログがありません）")
    else:
        lines.append("")
        for h in hour_packs:
            start_ts = _parse_ts(str(h.get("hour_start_ts") or ""))
            end_ts = _parse_ts(str(h.get("hour_end_ts") or ""))
            if not start_ts or not end_ts:
                continue
            active_min = int(h.get("active_sec_est") or 0) // 60
            lines.append(
                f"### {start_ts.strftime('%H:%M')}〜{end_ts.strftime('%H:%M')}（推定稼働: {active_min}分）"
            )
            lines.append("")

            hour_key = str(h.get("hour_start_ts") or "")
            hour_llm = hourly_llm_map.get(hour_key) or {}
            hour_enrich = hour_enrich_llm_map.get(hour_key) or {}

            cluster_labels = _hour_pack_cluster_labels(h)
            
            # 観測ベースの hour_title / hour_summary を優先表示
            hour_title = _safe(str(hour_llm.get("hour_title") or "").strip())
            hour_summary = _safe(str(hour_llm.get("hour_summary") or "").strip())
            # enriched は補足として別項目に表示
            hour_summary_enriched = _safe(str(hour_enrich.get("hour_summary_enriched") or "").strip())
            
            # タイトル: hour_llm > cluster_labels > 不明
            display_title = hour_title
            if not display_title:
                display_title = (cluster_labels[0] if cluster_labels else "").strip() or "（不明）"
            
            # 概要: hour_llm > observation（観測ベースを維持）
            display_summary = hour_summary
            if not display_summary:
                display_summary = _hour_pack_observation(h)

            # Timeline: 観測ベースをメインに、enrichedは補足として追記
            lines.append(f"- 主な作業: {display_title}")
            if display_summary:
                lines.append(f"- 概要: {display_summary}")
            if cluster_labels:
                lines.append(f"- 主な作業画面: {', '.join(cluster_labels[:2])}")
            if hour_summary_enriched:
                if _is_near_duplicate(display_summary, hour_summary_enriched):
                    lines.append("- 推測される意図: （観測の要約と近いため省略）")
                else:
                    lines.append(f"- 推測される意図: {hour_summary_enriched}")
            lines.append("")

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
        if trace_stage_max is None or trace_stage_max >= 2:
            _write_trace_jsonl(
                _trace_dir(date, trace_run_id) / "stage-02.segment.jsonl",
                event_trace_with_segments,
            )
        if trace_stage_max is None or trace_stage_max >= 5:
            hour_llm_rows: list[dict[str, Any]] = []
            for h in hour_packs:
                key = str(h.get("hour_start_ts") or "")
                if key and key in hourly_llm_map:
                    hour_llm_rows.append(hourly_llm_map[key])
            if hour_llm_rows:
                _write_trace_jsonl(
                    _trace_dir(date, trace_run_id) / "stage-05.hour-llm.jsonl",
                    hour_llm_rows,
                )
        if trace_stage_max is None or trace_stage_max >= 6:
            if isinstance(daily_llm, dict) and daily_llm:
                _write_trace_json(
                    _trace_dir(date, trace_run_id) / "stage-06.daily-llm.json",
                    daily_llm,
                )


    lines.append("")
    lines.append("## 参考（データ品質）")
    lines.append("")
    lines.append(f"- 除外: {len(excluded_events)}回")
    lines.append(f"- スクショ/OCR失敗: {len(error_events)}回")

    _progress(95, "マークダウン書き出し中")
    md = "\n".join(lines).rstrip() + "\n"
    if safe_md_enabled:
        md = sanitize_markdown_for_sharing(md, cfg)
    out_path.write_text(md, encoding="utf-8")

    # Notion同期（環境変数で有効化）
    from .notion_sync import notion_sync_enabled, sync_daily, retry_pending
    if notion_sync_enabled():
        # まず未同期があれば再試行
        retry_pending()
        # 今回分を同期
        daily_llm_path = _daily_llm_path(date, run_id=output_run_id)
        sync_daily(date, output_run_id, out_path, daily_llm_path)

    _progress(100, "完了")
    return out_path
