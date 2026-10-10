"""Test isolation: no test reads the developer's .env or real credentials, and no
test reads or writes files in backend/. Every on-disk path points at a temp dir."""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="warden-test-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

os.environ["WARDEN_NO_DOTENV"] = "1"
for _var in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID", "PAYPAL_BASE_URL", "GEMINI_API_KEY",
             "GOOGLE_API_KEY", "WARDEN_GEMINI_MODEL", "WARDEN_SANDBOX_SEED", "WARDEN_PUBLIC_DEMO", "WARDEN_NTFY_TOPIC",
             "WARDEN_APPROVAL_WEBHOOK", "WARDEN_PUBLIC_URL", "WARDEN_DEMO_AUTO_APPROVE", "WARDEN_LLM_DAILY_CAP",
             "WARDEN_LLM_TIMEOUT"):
    os.environ.pop(_var, None)
os.environ["WARDEN_DISABLE_LLM"] = "1"
os.environ["WARDEN_PAYPAL_MODE"] = "simulator"
os.environ["WARDEN_APPROVAL_TIMEOUT"] = "1.5"
os.environ["WARDEN_DB"] = ":memory:"
os.environ["WARDEN_LLM_CACHE"] = str(_TMP / "llm_cache.json")
os.environ["WARDEN_LLM_USAGE"] = str(_TMP / "llm_usage.json")
os.environ["WARDEN_SEED_FILE"] = str(_TMP / "sandbox_seed.json")

from warden.config import settings  # noqa: E402

settings.approval_timeout_s = 1.5

import pytest  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent


def _backend_files() -> dict[str, tuple[int, float]]:
    out = {}
    for p in BACKEND.iterdir():
        if p.is_file() and p.suffix in (".json", ".db", ".tmp"):
            st = p.stat()
            out[p.name] = (st.st_size, st.st_mtime)
    return out


@pytest.fixture(scope="session", autouse=True)
def nothing_written_to_backend():
    """Fails the run if any test creates or changes a data file in backend/."""
    before = _backend_files()
    yield
    after = _backend_files()
    assert after == before, f"tests touched files in backend/: {sorted(set(after.items()) ^ set(before.items()))}"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Each test gets its own empty cache, usage counter and seed file, and fresh module state."""
    from demo import inbox
    from warden import llm
    monkeypatch.setattr(settings, "llm_cache_path", str(tmp_path / "llm_cache.json"))
    monkeypatch.setattr(settings, "llm_usage_path", str(tmp_path / "llm_usage.json"))
    monkeypatch.setattr(settings, "db_path", ":memory:")
    monkeypatch.setattr(inbox, "SEED_FILE", tmp_path / "sandbox_seed.json")
    monkeypatch.setattr(llm, "_limited_until", 0.0)
    monkeypatch.setattr(llm, "_last_quota", {})
    monkeypatch.setattr(llm, "_http", None)
    llm.cache_reset()
    llm.usage_reset()
    inbox.sandbox_ids.cache_clear()
    yield
    llm.cache_reset()
    llm.usage_reset()
    inbox.sandbox_ids.cache_clear()
