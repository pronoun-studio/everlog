# Role: 機微情報が混ざりやすい場面を「記録しない（除外）」ための判定を行う。
# How: アプリ名・ドメイン・タイトル・OCRの一部（プレビュー）を材料に、設定にあるキーワードルールで安全側に除外を返す。
# Key functions: `should_exclude()`, `ExclusionDecision`
# Collaboration: `everlog/capture.py` がOCR前/後の2段階で呼び、除外時はOCR全文を保存せずスタブイベントをJSONLに残す。ルールは `everlog/config.py` から供給される。
from __future__ import annotations

from dataclasses import dataclass

from .config import AppConfig
from .collect import ActiveContext


@dataclass(frozen=True)
class ExclusionDecision:
    excluded: bool
    reason: str


def should_exclude(ctx: ActiveContext, ocr_preview: str | None, cfg: AppConfig) -> ExclusionDecision:
    app = (ctx.active_app or "").strip()
    if app and app.lower() in {a.lower() for a in cfg.exclude.apps}:
        return ExclusionDecision(True, f"app:{app}")

    # ロック画面っぽいときはスキップ（厳密検知は後で）
    if app.lower() in {"loginwindow"}:
        return ExclusionDecision(True, "locked:loginwindow")

    domain = (ctx.browser.domain if ctx.browser else "") or ""
    domain_l = domain.lower()
    for kw in cfg.exclude.domain_keywords:
        if kw.lower() and kw.lower() in domain_l:
            return ExclusionDecision(True, f"domain_kw:{kw}")

    title = (ctx.window_title or "")
    preview = (ocr_preview or "")
    hay = f"{title}\n{preview}"
    for kw in cfg.exclude.text_keywords:
        if kw and kw in hay:
            return ExclusionDecision(True, f"text_kw:{kw}")

    return ExclusionDecision(False, "")
