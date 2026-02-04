# Role: ログディレクトリ配下の `config.json` を読み書きし、間隔/除外/マスキングなど運用設定を提供する。
# How: dataclassで設定スキーマを定義し、未作成ならデフォルト設定を生成して保存する（UIから編集しやすい形にする）。
# Key functions: `load_config()`, `save_config()`
# Collaboration: capture/collect/exclusions/redact が参照し、launchd は `interval_sec` をplistに反映する。menubar は設定の編集・保存・反映（再ロード）を行う。
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .paths import ensure_dirs, get_paths


@dataclass
class RedactConfig:
    enable_email: bool = True
    enable_phone: bool = False
    enable_credit_card: bool = True
    enable_auth_nearby: bool = True


@dataclass
class ExcludeConfig:
    apps: list[str] = field(default_factory=lambda: ["1Password"])
    domain_keywords: list[str] = field(
        default_factory=lambda: [
            "bank",
            "netbank",
            "onlinebank",
            "atm",
            "creditunion",
            "login",
            "signin",
            "auth",
            "sso",
            "oauth",
            "account",
            "pay",
            "payment",
            "checkout",
            "billing",
            "invoice",
            "stripe",
            "paypal",
        ]
    )
    text_keywords: list[str] = field(
        default_factory=lambda: [
            "ログイン",
            "サインイン",
            "パスワード",
            "暗証番号",
            "二段階",
            "認証",
            "カード番号",
            "セキュリティコード",
            "お支払い",
            "請求",
            "Sign in",
            "Login",
            "Password",
            "One-time",
            "2FA",
            "Verification",
            "Card number",
            "CVV",
            "Security code",
            "Billing",
        ]
    )


@dataclass
class AppConfig:
    interval_sec: int = 300
    browser: str = "chrome"
    keep_screenshots: bool = False
    # Optional: path to a built macOS app (py2app) to run capture via `open`.
    # This is a workaround when Screen Recording permission cannot be granted to the raw Python binary.
    capture_app_path: str | None = None
    exclude: ExcludeConfig = field(default_factory=ExcludeConfig)
    redact: RedactConfig = field(default_factory=RedactConfig)


def _to_dict(cfg: AppConfig) -> dict:
    return {
        "interval_sec": cfg.interval_sec,
        "browser": cfg.browser,
        "keep_screenshots": cfg.keep_screenshots,
        "capture_app_path": cfg.capture_app_path,
        "exclude": {
            "apps": cfg.exclude.apps,
            "domain_keywords": cfg.exclude.domain_keywords,
            "text_keywords": cfg.exclude.text_keywords,
        },
        "redact": {
            "enable_email": cfg.redact.enable_email,
            "enable_phone": cfg.redact.enable_phone,
            "enable_credit_card": cfg.redact.enable_credit_card,
            "enable_auth_nearby": cfg.redact.enable_auth_nearby,
        },
    }


def _from_dict(data: dict) -> AppConfig:
    exclude = data.get("exclude", {}) or {}
    redact = data.get("redact", {}) or {}
    return AppConfig(
        interval_sec=int(data.get("interval_sec", 300)),
        browser=str(data.get("browser", "chrome")),
        keep_screenshots=bool(data.get("keep_screenshots", False)),
        capture_app_path=(str(data["capture_app_path"]) if data.get("capture_app_path") else None),
        exclude=ExcludeConfig(
            apps=list(exclude.get("apps", ["1Password"])),
            domain_keywords=list(exclude.get("domain_keywords", ExcludeConfig().domain_keywords)),
            text_keywords=list(exclude.get("text_keywords", ExcludeConfig().text_keywords)),
        ),
        redact=RedactConfig(
            enable_email=bool(redact.get("enable_email", True)),
            enable_phone=bool(redact.get("enable_phone", False)),
            enable_credit_card=bool(redact.get("enable_credit_card", True)),
            enable_auth_nearby=bool(redact.get("enable_auth_nearby", True)),
        ),
    )


def load_config() -> AppConfig:
    ensure_dirs()
    path = get_paths().config_path
    if not path.exists():
        cfg = AppConfig()
        save_config(cfg)
        return cfg
    data = json.loads(path.read_text(encoding="utf-8"))
    return _from_dict(data)


def save_config(cfg: AppConfig) -> Path:
    ensure_dirs()
    path = get_paths().config_path
    path.write_text(json.dumps(_to_dict(cfg), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
