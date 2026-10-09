"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv() -> None:
    """Read KEY=VALUE lines from the repo's .env (or backend/.env). Real env vars win."""
    backend = Path(__file__).resolve().parent.parent
    for path in (backend.parent / ".env", backend / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            if value:
                os.environ.setdefault(key.strip(), value)


load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    paypal_client_id: str = field(default_factory=lambda: os.getenv("PAYPAL_CLIENT_ID", ""))
    paypal_client_secret: str = field(default_factory=lambda: os.getenv("PAYPAL_CLIENT_SECRET", ""))
    paypal_base_url: str = field(
        default_factory=lambda: os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
    )
    paypal_webhook_id: str = field(default_factory=lambda: os.getenv("PAYPAL_WEBHOOK_ID", ""))
    # "sandbox" talks to PayPal; "simulator" uses the offline in-memory PayPal.
    paypal_mode: str = field(default_factory=lambda: os.getenv("WARDEN_PAYPAL_MODE", ""))

    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("WARDEN_LLM_MODEL", "claude-sonnet-5-5"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("WARDEN_GEMINI_MODEL", "gemini-2.5-flash"))
    # "anthropic" or "gemini"; empty picks whichever has a key (Anthropic first).
    llm_provider_setting: str = field(default_factory=lambda: os.getenv("WARDEN_LLM_PROVIDER", "").strip().lower())
    llm_disabled: bool = field(default_factory=lambda: _bool("WARDEN_DISABLE_LLM"))

    signing_key: str = field(
        default_factory=lambda: os.getenv("WARDEN_SIGNING_KEY", "dev-only-change-me")
    )
    db_path: str = field(
        default_factory=lambda: os.getenv(
            "WARDEN_DB", str(Path(__file__).resolve().parent.parent / "warden.db")
        )
    )
    approval_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("WARDEN_APPROVAL_TIMEOUT", "45"))
    )
    ntfy_topic: str = field(default_factory=lambda: os.getenv("WARDEN_NTFY_TOPIC", ""))
    approval_webhook_url: str = field(default_factory=lambda: os.getenv("WARDEN_APPROVAL_WEBHOOK", ""))
    public_url: str = field(default_factory=lambda: os.getenv("WARDEN_PUBLIC_URL", "http://localhost:8000"))
    demo_auto_approve: bool = field(default_factory=lambda: _bool("WARDEN_DEMO_AUTO_APPROVE"))

    @property
    def use_sandbox(self) -> bool:
        if self.paypal_mode:
            return self.paypal_mode == "sandbox"
        return bool(self.paypal_client_id and self.paypal_client_secret)

    @property
    def llm_provider(self) -> str | None:
        if self.llm_disabled:
            return None
        keys = {"anthropic": self.anthropic_api_key, "gemini": self.gemini_api_key}
        if self.llm_provider_setting:
            return self.llm_provider_setting if keys.get(self.llm_provider_setting) else None
        return next((name for name, key in keys.items() if key), None)

    @property
    def llm_model(self) -> str:
        return self.gemini_model if self.llm_provider == "gemini" else self.claude_model

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider is not None


settings = Settings()
