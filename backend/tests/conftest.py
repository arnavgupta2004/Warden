import os
import tempfile

os.environ["WARDEN_DISABLE_LLM"] = "1"
os.environ["WARDEN_PAYPAL_MODE"] = "simulator"
os.environ["WARDEN_APPROVAL_TIMEOUT"] = "1.5"
os.environ.setdefault("WARDEN_DB", ":memory:")
os.environ["WARDEN_LLM_CACHE"] = os.path.join(tempfile.mkdtemp(prefix="warden-test-"), "llm_cache.json")

from warden.config import settings  # noqa: E402

settings.approval_timeout_s = 1.5

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_llm_cache(tmp_path, monkeypatch):
    """Each test gets its own empty Gemini response cache."""
    from warden import llm
    monkeypatch.setattr(settings, "llm_cache_path", str(tmp_path / "llm_cache.json"))
    llm.cache_reset()
    yield
    llm.cache_reset()
