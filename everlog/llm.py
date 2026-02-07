# Role: OpenAI APIで作業セグメントを要約/ラベル付けする。
# How: Chat Completions API (/v1/chat/completions) を呼び出し、JSON形式の結果をパースして返す。
# Key functions: `analyze_segments()`
# Collaboration: `everlog/enrich.py` から使用する。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import os
import re
import urllib.request
from pathlib import Path


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmResult:
    model: str
    raw_text: str
    data: dict[str, Any]
    usage: dict[str, Any]

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
                "required": [
                    "segment_id",
                    "task_title",
                    "task_summary",
                    "category",
                    "confidence",
                ],
                "properties": {
                    "segment_id": {"type": "integer", "minimum": 0},
                    "task_title": {"type": "string"},
                    "task_summary": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["dev", "meeting", "research", "writing", "admin", "other"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}

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
                    "confidence",
                ],
                "properties": {
                    "hour_start_ts": {"type": "string"},
                    "hour_end_ts": {"type": "string"},
                    "hour_title": {"type": "string"},
                    "hour_summary": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


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

# hour-enrich: daily contextを踏まえて各時間帯の目的・意味を再解釈
_HOUR_ENRICH_SCHEMA: dict[str, Any] = {
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
                    "hour_title_enriched",
                    "hour_summary_enriched",
                ],
                "properties": {
                    "hour_start_ts": {"type": "string"},
                    "hour_title_enriched": {"type": "string"},
                    "hour_summary_enriched": {"type": "string"},
                },
            },
        }
    },
}


def _extract_json(text: str) -> dict[str, Any]:
    # Prefer direct JSON parsing; fallback to the first JSON object found.
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


def _extract_output_text(responses_payload: dict[str, Any]) -> str:
    # Responses API: concatenate all output_text blocks.
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

    # Backward-compatible fallback: Chat Completions-like payload
    choices = responses_payload.get("choices") or []
    if isinstance(choices, list) and choices:
        raw_text = (
            (choices[0] or {}).get("message", {}).get("content", "") if isinstance(choices[0], dict) else ""
        )
        return str(raw_text or "").strip()

    return ""


# enrich処理で2026年2月7日現在使っていない
def _build_user_prompt(date: str, segments: list[dict[str, Any]]) -> str:
    payload = json.dumps({"date": date, "segments": segments}, ensure_ascii=False, indent=2)
    return (
        "あなたはPC作業ログの解析者です。以下のセグメント一覧から、各セグメントに"
        "「task_title」「task_summary」「category」「confidence」を付与してください。\n"
        "\n"
        "重要: 具体性を最優先にしてください。入力には `keywords` / `ocr_snippets` / `domain` / `window_title` が含まれます。\n"
        "- `task_title` は短く具体的（例:「OpenAIのAPI keys画面を確認」「everlog.appをターミナルから起動してログ確認」「GitHubでpronoun-studio/parameter-coinを閲覧」）。\n"
        "- `task_summary` は日本語1〜2文。必ず「何を」見た/操作したかを、ファイル名・ディレクトリ・URL・画面名など具体的なアンカーで最低1つ入れる。\n"
        "- 「データ探索」「情報参照」「短時間作業」「ファイル確認」だけの抽象表現で終わらせない（具体アンカー無しは禁止）。\n"
        "- 推測は控えめに（根拠が `keywords` / `ocr_snippets` / `window_title` にある範囲で）。\n"
        "- 秘密情報は出さない: APIキーやトークン文字列（`sk-...` など）/メール/パスワードらしきものは伏せる。既に `[REDACTED_*]` があればそれを尊重。\n"
        "\n"
        "必ずJSONのみを返してください。category は [dev, meeting, research, writing, admin, other] から選ぶ。\n\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "segments": [\n'
        "    {\n"
        '      "segment_id": 0,\n'
        '      "task_title": "短い作業名",\n'
        '      "task_summary": "1-2文の要約",\n'
        '      "category": "dev",\n'
        '      "confidence": 0.0\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "入力:\n"
        f"{payload}\n"
    )


def _build_hourly_user_prompt(date: str, hours: list[dict[str, Any]]) -> str:
    payload = json.dumps({"date": date, "hours": hours}, ensure_ascii=False, indent=2)
    return (
        "あなたはPC作業ログの解析者です。以下の1時間パッケージ一覧から、各時間帯に"
        "「hour_title」「hour_summary」「confidence」を付与してください。\n"
        "\n"
        "重要: 出力は「ツール列挙」ではなく「行動/目的ベース」にする（アプリ名の羅列だけは禁止）。\n"
        "重要: 本文は自然文を優先し、URL/パス/ファイル名の羅列は禁止。\n"
        "重要: 入力の `clusters[].active_timeline` が主入力（最優先）、`hour_common_texts` は閲覧画面/背景情報として参照する（主役にしない）。\n"
        "重要: Markdown のタグ（例: `<details>`）や「根拠（OCR/URL/パス）」のような根拠ブロックを **生成しない**。\n"
        "補足: `active_timeline` は ts 順の差分OCRで、`segment_key` は作業文脈のヒント。\n"
        "\n"
        "- hour_title: 短く具体的（可能なら「〜を〜する」など行動+目的）。アプリ名だけは禁止\n"
        "- hour_summary: 日本語2〜3文。必ず「アクティブ画面で何を見ながら/操作しながら、何を進めていたか」を中心に書く。URL/パスの列挙は禁止\n"
        "- 推測は書かない（この工程は短く、観測ベースでまとめる）\n"
        "- 秘密情報は出さない: APIキーやトークン文字列（`sk-...` など）/メール/パスワードらしきものは伏せる。既に `[REDACTED_*]` があればそれを尊重。\n"
        "\n"
        "必ずJSONのみを返してください。\n\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "hours": [\n'
        "    {\n"
        '      "hour_start_ts": "2026-02-05T10:00:00+09:00",\n'
        '      "hour_end_ts": "2026-02-05T10:59:59+09:00",\n'
        '      "hour_title": "短い作業名",\n'
        '      "hour_summary": "2-3文の要約",\n'
        '      "confidence": 0.0,\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "入力:\n"
        f"{payload}\n"
    )


def _build_daily_user_prompt(date: str, hours: list[dict[str, Any]]) -> str:
    payload = json.dumps({"date": date, "hours": hours}, ensure_ascii=False, indent=2)
    return (
        "あなたはPC作業ログの解析者です。以下の1時間要約（hours）を踏まえて、1日全体の総括を作ってください。\n"
        "\n"
        "重要: 1日の総括は、hoursを全部俯瞰して「何をやった日か」を自然文でまとめる。\n"
        "- daily_title: 短く具体的（行動+目的）。アプリ名だけは禁止\n"
        "- daily_summary: 2〜3文。今日の主目的と流れを要約（断定寄りでよい）\n"
        "- daily_detail: 4〜6文。時系列の大きな流れ→山場→結果/次の課題。推測は **1つだけ**（\"推測:\" を1回だけ）\n"
        "- highlights: 箇条書きの短文（3〜6個）。成果/進捗/意思決定を中心に\n"
        "- evidence: 根拠の短いアンカー（最大10）。URL/パスの羅列は避け、短いフレーズで\n"
        "\n"
        "必ずJSONのみを返してください。\n\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "daily_title": "短い総括タイトル",\n'
        '  "daily_summary": "2-3文の要約",\n'
        '  "daily_detail": "4-6文の詳細",\n'
        '  "highlights": ["...", "..."],\n'
        '  "confidence": 0.0,\n'
        '  "evidence": ["..."]\n'
        "}\n\n"
        "入力:\n"
        f"{payload}\n"
    )


def _build_hour_enrich_prompt(
    date: str,
    daily_context: dict[str, Any],
    hours_overview: list[dict[str, Any]],
) -> str:
    """daily contextを踏まえて各時間帯の目的・意味を再解釈するプロンプトを生成"""
    payload = json.dumps(
        {
            "date": date,
            "daily_context": daily_context,
            "hours": hours_overview,
        },
        ensure_ascii=False,
        indent=2,
    )
    return (
        "あなたはPC作業ログの解析者です。以下の情報を踏まえて、各時間帯の作業を「目的・意味」ベースで再解釈してください。\n"
        "\n"
        "## 入力の説明\n"
        "- `daily_context`: 1日全体の目的・流れ（daily_title, daily_summary）\n"
        "- `hours`: 各時間帯の観測ベース要約（hour_title, hour_summary）\n"
        "\n"
        "## あなたのタスク\n"
        "各時間帯について、1日の目的や前後の文脈を踏まえて以下を出力してください:\n"
        "- `hour_title_enriched`: 「〜のために〜する」「〜に向けた〜」のように目的を含んだタイトル（短く）\n"
        "- `hour_summary_enriched`: 1〜2文。以下を含める:\n"
        "  1. なぜこの作業をしていたか（前後の文脈から推測される目的）\n"
        "  2. 1日の流れの中でこの作業がどんな意味を持つか（準備/調査/実装/確認など）\n"
        "\n"
        "## 注意\n"
        "- 1日の行動の概要（daily_title/daily_summary）を踏まえて行動の意味を解釈すること\n"
        "- 前の時間帯の作業が次の時間帯の準備になっている場合は、その関係を明示すること\n"
        "- アプリ名の羅列は禁止。行動・目的ベースで書くこと\n"
        "- URL/パスの列挙は禁止\n"
        "- 秘密情報（APIキー等）は出さない\n"
        "\n"
        "必ずJSONのみを返してください。\n\n"
        "出力JSONの形式:\n"
        "{\n"
        '  "hours": [\n'
        "    {\n"
        '      "hour_start_ts": "2026-02-05T10:00:00+09:00",\n'
        '      "hour_title_enriched": "〜のために〜する",\n'
        '      "hour_summary_enriched": "目的と意味を踏まえた2-3文の要約"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "入力:\n"
        f"{payload}\n"
    )


def analyze_segments(
    date: str,
    segments: list[dict[str, Any]],
    model: str,
    api_key: str,
    timeout_sec: int = 180,
) -> LlmResult:
    _load_dotenv_if_needed()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        hint = ""
        try:
            from .paths import get_paths

            hint = f" (try putting it in {get_paths().home / '.env'})"
        except Exception:
            hint = ""
        raise LlmError(f"OPENAI_API_KEY is not set.{hint}")

    system_prompt = (
        "You are a precise assistant that returns JSON only. "
        "Do not include any extra text outside of JSON."
    )
    user_prompt = _build_user_prompt(date, segments)

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "segment_labels",
                "strict": True,
                "schema": _SEGMENTS_SCHEMA,
            }
        },
    }

    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/responses"

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
        raise LlmError(f"Failed to parse API response: {e}") from e

    usage = data.get("usage") if isinstance(data, dict) else None
    raw_text = _extract_output_text(data)
    if not raw_text:
        raise LlmError("OpenAI API returned empty content.")

    parsed = _extract_json(raw_text)
    return LlmResult(
        model=model,
        raw_text=raw_text,
        data=parsed,
        usage=usage if isinstance(usage, dict) else {},
    )


def analyze_hour_blocks(
    date: str,
    hours: list[dict[str, Any]],
    model: str,
    api_key: str,
    timeout_sec: int = 180,
) -> LlmResult:
    _load_dotenv_if_needed()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        hint = ""
        try:
            from .paths import get_paths

            hint = f" (try putting it in {get_paths().home / '.env'})"
        except Exception:
            hint = ""
        raise LlmError(f"OPENAI_API_KEY is not set.{hint}")

    system_prompt = (
        "You are a precise assistant that returns JSON only. "
        "Do not include any extra text outside of JSON."
    )
    user_prompt = _build_hourly_user_prompt(date, hours)

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "hourly_summaries",
                "strict": True,
                "schema": _HOURS_SCHEMA,
            }
        },
    }

    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/responses"

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
        raise LlmError(f"Failed to parse API response: {e}") from e

    usage = data.get("usage") if isinstance(data, dict) else None
    raw_text = _extract_output_text(data)
    if not raw_text:
        raise LlmError("OpenAI API returned empty content.")

    parsed = _extract_json(raw_text)
    return LlmResult(
        model=model,
        raw_text=raw_text,
        data=parsed,
        usage=usage if isinstance(usage, dict) else {},
    )


def analyze_day_summary(
    date: str,
    hours: list[dict[str, Any]],
    model: str,
    api_key: str,
    timeout_sec: int = 180,
) -> LlmResult:
    """
    Build a daily summary from hour-level summaries (already LLM'd or rule-based).
    """
    _load_dotenv_if_needed()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        hint = ""
        try:
            from .paths import get_paths

            hint = f" (try putting it in {get_paths().home / '.env'})"
        except Exception:
            hint = ""
        raise LlmError(f"OPENAI_API_KEY is not set.{hint}")

    system_prompt = (
        "You are a precise assistant that returns JSON only. "
        "Do not include any extra text outside of JSON."
    )
    user_prompt = _build_daily_user_prompt(date, hours)

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "daily_summary",
                "strict": True,
                "schema": _DAILY_SCHEMA,
            }
        },
    }

    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/responses"

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
        raise LlmError(f"Failed to parse API response: {e}") from e

    usage = data.get("usage") if isinstance(data, dict) else None
    raw_text = _extract_output_text(data)
    if not raw_text:
        raise LlmError("OpenAI API returned empty content.")

    parsed = _extract_json(raw_text)
    return LlmResult(
        model=model,
        raw_text=raw_text,
        data=parsed,
        usage=usage if isinstance(usage, dict) else {},
    )


def enrich_hours_with_context(
    date: str,
    daily_context: dict[str, Any],
    hours_overview: list[dict[str, Any]],
    model: str,
    api_key: str,
    timeout_sec: int = 180,
) -> LlmResult:
    """
    daily contextを踏まえて各時間帯の目的・意味を再解釈する。
    
    Args:
        date: 日付（YYYY-MM-DD形式）
        daily_context: daily_title, daily_summary を含む辞書
        hours_overview: 各時間帯の hour_start_ts, hour_title, hour_summary を含むリスト
        model: 使用するモデル名
        api_key: OpenAI APIキー
        timeout_sec: タイムアウト秒数
    
    Returns:
        LlmResult: hours[].hour_title_enriched, hour_summary_enriched を含む結果
    """
    _load_dotenv_if_needed()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        hint = ""
        try:
            from .paths import get_paths

            hint = f" (try putting it in {get_paths().home / '.env'})"
        except Exception:
            hint = ""
        raise LlmError(f"OPENAI_API_KEY is not set.{hint}")

    system_prompt = (
        "You are a precise assistant that returns JSON only. "
        "Do not include any extra text outside of JSON."
    )
    user_prompt = _build_hour_enrich_prompt(date, daily_context, hours_overview)

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "hour_enrich",
                "strict": True,
                "schema": _HOUR_ENRICH_SCHEMA,
            }
        },
    }

    base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/responses"

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
        raise LlmError(f"Failed to parse API response: {e}") from e

    usage = data.get("usage") if isinstance(data, dict) else None
    raw_text = _extract_output_text(data)
    if not raw_text:
        raise LlmError("OpenAI API returned empty content.")

    parsed = _extract_json(raw_text)
    return LlmResult(
        model=model,
        raw_text=raw_text,
        data=parsed,
        usage=usage if isinstance(usage, dict) else {},
    )


def _get_usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int] | None:
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


def _read_price_env() -> tuple[float | None, float | None]:
    def _get(keys: list[str]) -> float | None:
        for k in keys:
            v = os.environ.get(k)
            if v is None or str(v).strip() == "":
                continue
            try:
                return float(str(v).strip())
            except Exception:
                return None
        return None

    input_price = _get(
        [
            "EVERLOG_LLM_PRICE_INPUT_PER_1M",
            "EVERYTIMECAPTURE_LLM_PRICE_INPUT_PER_1M",
        ]
    )
    output_price = _get(
        [
            "EVERLOG_LLM_PRICE_OUTPUT_PER_1M",
            "EVERYTIMECAPTURE_LLM_PRICE_OUTPUT_PER_1M",
        ]
    )
    return input_price, output_price


def _llm_price_tier() -> str:
    """
    Pricing tier for cost estimation.
    Matches OpenAI pricing table sections: batch/flex/standard/priority.
    """
    raw = (
        os.environ.get("EVERLOG_LLM_TIER")
        or os.environ.get("EVERYTIMECAPTURE_LLM_TIER")
        or "standard"
    )
    return str(raw).strip().lower() or "standard"


# Text token prices per 1M tokens (Input / Cached input / Output).
# Source: https://platform.openai.com/docs/pricing (2026-02)
_TEXT_TOKEN_PRICES_PER_1M: dict[str, dict[str, tuple[float | None, float | None, float | None]]] = {
    "standard": {
        "gpt-5-nano": (0.05, 0.005, 0.40),
    },
    "flex": {
        "gpt-5-nano": (0.025, 0.0025, 0.20),
    },
    "batch": {
        "gpt-5-nano": (0.025, 0.0025, 0.20),
    },
    "priority": {
        # Note: pricing table pasted does not list gpt-5-nano under Priority.
        # If you use Priority processing, set env overrides or add the price here.
    },
}


def _get_text_token_prices_per_1m(
    model: str, *, tier: str | None = None
) -> tuple[float | None, float | None, float | None]:
    t = (tier or _llm_price_tier()).strip().lower() or "standard"
    m = str(model or "").strip()
    if not m:
        return None, None, None
    tier_map = _TEXT_TOKEN_PRICES_PER_1M.get(t) or {}
    if m in tier_map:
        return tier_map[m]
    # Fallback: if model isn't in the table, we can still honor env overrides (input/output only).
    ip, op = _read_price_env()
    return ip, None, op


def calc_cost_usd(
    usage: dict[str, Any] | None,
    model: str,
) -> float | None:
    tokens = _get_usage_tokens(usage)
    if not tokens:
        return None
    input_tokens, output_tokens = tokens
    cached_tokens = 0
    try:
        details = usage.get("input_tokens_details") if isinstance(usage, dict) else None
        if isinstance(details, dict):
            cached_tokens = int(details.get("cached_tokens") or 0)
    except Exception:
        cached_tokens = 0

    input_price, cached_input_price, output_price = _get_text_token_prices_per_1m(model)
    if input_price is None or output_price is None:
        return None
    if cached_tokens < 0:
        cached_tokens = 0
    if cached_tokens > input_tokens:
        cached_tokens = input_tokens

    non_cached = input_tokens - cached_tokens
    cached_rate = cached_input_price if cached_input_price is not None else input_price
    return (non_cached * input_price + cached_tokens * cached_rate + output_tokens * output_price) / 1_000_000.0


def _load_dotenv_if_needed() -> None:
    """
    Load environment variables from a .env file (without overwriting existing env vars).

    Why: `launchd` / `.app` 実行では CWD や環境変数が期待通りでないことが多い。
    This searches a few well-known locations so LLMが起動しやすいようにする。
    """
    candidates: list[Path] = []

    override = (
        os.environ.get("EVERLOG_DOTENV_PATH")
        or os.environ.get("EVERYTIMECAPTURE_DOTENV_PATH")
        or ""
    ).strip()
    if override:
        for part in override.split(os.pathsep):
            p = part.strip()
            if p:
                candidates.append(Path(p).expanduser())

    log_home_override = (
        os.environ.get("EVERLOG_LOG_HOME")
        or os.environ.get("EVERYTIMECAPTURE_LOG_HOME")
        or ""
    ).strip()
    if log_home_override:
        candidates.append(Path(log_home_override).expanduser() / ".env")

    # 1) current working directory
    candidates.append(Path.cwd() / ".env")

    # 2) log directory (detected project log dir)
    try:
        from .paths import get_paths

        candidates.append(get_paths().home / ".env")
    except Exception:
        pass

    # 3) project root (directory containing pyproject.toml or .git)
    try:
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
                candidates.append(parent / ".env")
                break
    except Exception:
        pass

    # De-duplicate while preserving order
    seen: set[Path] = set()
    for path in candidates:
        try:
            path = path.resolve()
        except Exception:
            path = path
        if path in seen:
            continue
        seen.add(path)
        _load_dotenv_file(path)


def _load_dotenv_file(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except Exception:
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip("\"'")  # remove surrounding quotes if present
        if not key or key in os.environ:
            continue
        os.environ[key] = val
