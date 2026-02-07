# Role: 最終出力（Markdown/Notion同期など）に含めたくない情報をローカルでサニタイズする。
# How: 既存の `redact.redact_text()`（メール/電話/カード/認証近傍）を再利用しつつ、
#      追加で「APIキー/トークン/秘密鍵」などの典型パターンをマスクし、危険度の高い語を置換する。
# Key functions: `sanitize_text_for_sharing()`, `sanitize_markdown_for_sharing()`
# Collaboration: `everlog/summarize.py` のMarkdown生成や、外部同期の直前で適用する。
from __future__ import annotations

import re
from typing import Iterable

from .config import AppConfig
from .redact import redact_text


# --- Secrets / tokens (broad, high-signal patterns) ---
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9]{10,}\b")
_GITHUB_TOKEN_RE = re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{10,}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")

# Private key blocks: mask the whole block conservatively.
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    flags=re.MULTILINE,
)

# Generic "key=value" style secrets (only when key name is high-signal).
_SECRET_KV_RE = re.compile(
    r"(?i)\b("
    r"api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|secret|client[-_]?secret|"
    r"password|passcode|otp|one[- ]time|verification[_ -]?code|session|cookie"
    r")\b\s*[:=]\s*([^\s'\"`]{6,})"
)


# --- Sensitive content (minimal, explicit-only keyword replacement) ---
# Goal: do not keep “explicit” sexual content / self-harm etc in shareable summary.
# Keep this conservative to avoid redacting normal dev/work terms.
_SENSITIVE_KEYWORDS: list[tuple[re.Pattern[str], str]] = [
    # Adult / explicit
    (re.compile(r"(?i)\b(porn|pornhub|onlyfans|hentai)\b"), "[REDACTED_ADULT]"),
    (re.compile(r"(?i)\b(sex|sexual|xxx)\b"), "[REDACTED_ADULT]"),
    (re.compile(r"(エロ|アダルト|ポルノ|性行為|自慰|オナニー|AV|エッチ)"), "[REDACTED_ADULT]"),
    # Self-harm
    (re.compile(r"(?i)\b(suicide|self[- ]harm)\b"), "[REDACTED_SELF_HARM]"),
    (re.compile(r"(自殺|自傷)"), "[REDACTED_SELF_HARM]"),
]


def _replace_many(text: str, patterns: Iterable[tuple[re.Pattern[str], str]]) -> str:
    out = text
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    return out


def sanitize_text_for_sharing(text: str, cfg: AppConfig) -> str:
    """
    Sanitize a short text that might end up in a shareable artifact (Markdown/Notion/etc).

    - Applies existing capture-time redaction rules again (defense in depth)
    - Masks typical API keys/tokens/private keys
    - Replaces a small set of explicit sensitive keywords
    """
    if not text:
        return text

    out = str(text)

    # Re-apply PII/auth redaction (already used in capture, but LLM outputs might re-introduce patterns).
    out = redact_text(out, cfg)

    # Mask secret-like patterns
    out = _PRIVATE_KEY_BLOCK_RE.sub("[REDACTED_PRIVATE_KEY]", out)
    out = _OPENAI_KEY_RE.sub("[REDACTED_API_KEY]", out)
    out = _GITHUB_TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _SLACK_TOKEN_RE.sub("[REDACTED_TOKEN]", out)
    out = _AWS_ACCESS_KEY_RE.sub("[REDACTED_TOKEN]", out)
    out = _JWT_RE.sub("[REDACTED_TOKEN]", out)

    # Mask high-signal key=value secrets (keep key name; mask value)
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=[REDACTED_SECRET]", out)

    # Minimal keyword redaction (explicit-only)
    out = _replace_many(out, _SENSITIVE_KEYWORDS)

    return out


def sanitize_markdown_for_sharing(md: str, cfg: AppConfig) -> str:
    """
    Sanitize an entire markdown string. Currently it just applies `sanitize_text_for_sharing`
    globally, but we keep this function to evolve into line/section-aware filtering later.
    """
    return sanitize_text_for_sharing(md, cfg)

