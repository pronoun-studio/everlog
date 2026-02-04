# Role: OCRテキストに含まれうるPII/認証情報を保存前にマスキングする。
# How: 正規表現でメール/電話/カード候補を置換し、カードはLuhnチェックで誤検知を減らす。認証関連は近傍行もまとめて伏せる。
# Key functions: `redact_text()`, `_luhn_ok()`
# Collaboration: `everlog/capture.py` がOCR結果に適用してからJSONLへ保存する。ON/OFFは `everlog/config.py` の設定で制御する。
from __future__ import annotations

import re

from .config import AppConfig


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[- ]?)?(?:\(?\d{2,4}\)?[- ]?)?\d{2,4}[- ]?\d{3,4}\b")
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_AUTH_HINT_RE = re.compile(
    r"(?i)\b(password|passcode|otp|one[- ]time|2fa|verification|secret|cvv|security code|card number)\b"
)


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def redact_text(text: str, cfg: AppConfig) -> str:
    if not text:
        return text

    out = text

    if cfg.redact.enable_email:
        out = _EMAIL_RE.sub("[REDACTED_EMAIL]", out)

    if cfg.redact.enable_phone:
        out = _PHONE_RE.sub("[REDACTED_PHONE]", out)

    if cfg.redact.enable_credit_card:
        def repl(m: re.Match) -> str:
            s = m.group(0)
            if _luhn_ok(s):
                return "[REDACTED_CARD]"
            return s

        out = _CARD_CANDIDATE_RE.sub(repl, out)

    if cfg.redact.enable_auth_nearby:
        lines = out.splitlines()
        masked = []
        for i, line in enumerate(lines):
            if _AUTH_HINT_RE.search(line):
                masked.append("[REDACTED_AUTH]")
                continue
            prev_line = lines[i - 1] if i > 0 else ""
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if _AUTH_HINT_RE.search(prev_line) or _AUTH_HINT_RE.search(next_line):
                masked.append("[REDACTED_AUTH]")
            else:
                masked.append(line)
        out = "\n".join(masked)

    return out
