import os
import tempfile

os.environ["WARDEN_DISABLE_LLM"] = "1"
os.environ["WARDEN_PAYPAL_MODE"] = "simulator"
os.environ["WARDEN_APPROVAL_TIMEOUT"] = "1.5"
os.environ.setdefault("WARDEN_DB", ":memory:")
os.environ["WARDEN_MANDATE_CACHE"] = os.path.join(tempfile.mkdtemp(prefix="warden-test-"), "mandate_cache.json")

from warden.config import settings  # noqa: E402

settings.approval_timeout_s = 1.5
