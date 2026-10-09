"""Seed your PayPal sandbox with the demo shop's orders and invoice.

Creates four real orders (the purchases the support emails refer to), asks
you to approve each one as a sandbox buyer, captures them, sends one invoice,
and writes backend/sandbox_seed.json. When Warden runs against the sandbox,
the demo inbox rewrites its order/invoice ids to these real ones, so every
refund in the demo is a real sandbox refund.

    python scripts/seed_sandbox.py      # reads PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from .env or the environment

Approve with a sandbox *personal* account from
https://developer.paypal.com/dashboard/accounts
"""
from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from warden.config import load_dotenv  # noqa: E402

load_dotenv()
BASE = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
CID = os.getenv("PAYPAL_CLIENT_ID")
SEC = os.getenv("PAYPAL_CLIENT_SECRET")
OUT = Path(__file__).resolve().parent.parent / "backend" / "sandbox_seed.json"

# simulator capture id, simulator order id, amount, description
SEEDS = [
    ("4RT88014HN112233K", "7XK21934UB556120D", "42.00", "Stoneware mug set (4)"),
    ("9LM20577QW341870P", "2HB90321KE118845M", "180.00", "Walnut desk lamp"),
    ("1VC66309TR775024A", "5NF44208GX903317J", "220.00", "Linen throw + cushions"),
    ("6DW31852LP480916E", "8QT75510MC227094R", "64.00", "Ceramic planter"),
]
SIM_INVOICE = "INV2-KST7-4Q2M-8ZP3-HC41"


def main() -> None:
    if not (CID and SEC):
        sys.exit("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox REST app credentials).")
    c = httpx.Client(base_url=BASE, timeout=30)
    tok = c.post("/v1/oauth2/token", auth=(CID, SEC), data={"grant_type": "client_credentials"}).raise_for_status().json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})

    seed = json.loads(OUT.read_text()) if OUT.exists() else {"orders": {}, "captures": {}, "invoices": {}}
    for sim_cap, sim_order, amount, desc in SEEDS:
        if sim_cap in seed["captures"]:
            print(f"✓ {desc}: already seeded ({seed['captures'][sim_cap]})")
            continue
        o = c.post("/v2/checkout/orders", json={
            "intent": "CAPTURE",
            "purchase_units": [{"reference_id": sim_order, "description": desc, "amount": {"currency_code": "USD", "value": amount}}],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": "Kestrel & Co. (demo)", "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING",
                "return_url": "https://example.com/approved", "cancel_url": "https://example.com/cancelled"}}},
        }).raise_for_status().json()
        link = next(l["href"] for l in o["links"] if l["rel"] in ("payer-action", "approve"))
        print(f"\n{desc} (${amount}): approve this order as your sandbox buyer:\n  {link}")
        try:
            webbrowser.open(link)
        except Exception:
            pass
        while True:
            st = c.get(f"/v2/checkout/orders/{o['id']}").json()["status"]
            if st == "APPROVED":
                break
            print(f"  waiting for approval (status {st})…", end="\r")
            time.sleep(3)
        cap = c.post(f"/v2/checkout/orders/{o['id']}/capture", headers={"PayPal-Request-Id": f"seed-{o['id']}"}).raise_for_status().json()
        cap_id = cap["purchase_units"][0]["payments"]["captures"][0]["id"]
        seed["orders"][sim_order] = o["id"]
        seed["captures"][sim_cap] = cap_id
        OUT.write_text(json.dumps(seed, indent=2))
        print(f"\n✓ captured {cap_id}")

    if SIM_INVOICE not in seed["invoices"]:
        to = os.getenv("SEED_INVOICE_EMAIL") or input("\nSandbox email to receive the demo wholesale invoice: ").strip()
        inv = c.post("/v2/invoicing/invoices", json={
            "detail": {"currency_code": "USD", "note": "Wholesale order: 50 mug sets"},
            "primary_recipients": [{"billing_info": {"email_address": to}}],
            "items": [{"name": "Stoneware mug set (wholesale)", "quantity": "50", "unit_amount": {"currency_code": "USD", "value": "25.00"}}],
        }).raise_for_status().json()
        iid = inv.get("id") or inv["href"].rstrip("/").split("/")[-1]
        c.post(f"/v2/invoicing/invoices/{iid}/send", json={"send_to_invoicer": False}).raise_for_status()
        seed["invoices"][SIM_INVOICE] = iid
        OUT.write_text(json.dumps(seed, indent=2))
        print(f"✓ invoice {iid} sent to {to}")

    print(f"\nWrote {OUT}. Start Warden with the same credentials and the demo inbox will use these ids.")
    print("The dispute email (#6) needs a real dispute: open one from the buyer account on the planter order,")
    print("then add its id under \"disputes\": {\"PP-D-55210\": \"PP-D-…\"} in the seed file.")


if __name__ == "__main__":
    main()
