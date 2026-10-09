from ..config import settings
from .errors import PayPalError
from .sandbox import PayPalSandbox
from .simulator import Simulator


def make_backend():
    if settings.use_sandbox:
        return PayPalSandbox(settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base_url)
    return Simulator()


__all__ = ["PayPalError", "PayPalSandbox", "Simulator", "make_backend"]
