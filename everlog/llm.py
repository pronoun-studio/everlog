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

    raw_text = _extract_output_text(data)
    if not raw_text:
        raise LlmError("OpenAI API returned empty content.")

    parsed = _extract_json(raw_text)
    return LlmResult(model=model, raw_text=raw_text, data=parsed)


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
