# -*- coding: utf-8 -*-
"""
server/app/routes/checkout.py
-----------------------------
Public endpoints for the purchase website (``site/...``).

Two endpoints:

* ``POST /checkout/create`` — called when the customer submits the
  checkout form on the website. Creates a ``pending`` order and
  returns ``order_id`` + metadata. The website then redirects the
  customer to the payment provider (the redirect URL is computed in
  this handler once a provider is chosen).

* ``GET  /checkout/plans`` — lightweight public list of active plans
  for the website to render prices (no admin auth needed, read-only).

The actual payment + license issuance happens in
:mod:`app.routes.webhooks` after the provider confirms the payment.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..schemas import CheckoutCreateRequest, CheckoutCreateResponse, PublicPlan
from ..services import orders_service as orders
from ..services import plans_service as plans


logger = logging.getLogger(__name__)

router = APIRouter()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client_ip(request: Request) -> str:
    try:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# GET /checkout/plans
# ---------------------------------------------------------------------------
@router.get("/plans", response_model=list[PublicPlan])
def public_plans():
    """Return the list of plans the website should render.

    We expose only the subset of fields the website needs (id, name,
    days, license_type, price_ils, sort_order) — never admin-only
    fields.
    """
    try:
        rows = plans.list_plans(include_inactive=False)
    except Exception as exc:
        logger.error("checkout/plans: failed to list plans: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load plans")

    out: list[dict] = []
    for r in rows:
        out.append({
            "id":           r.get("id"),
            "name":         r.get("name") or "",
            "days":         r.get("days") or 0,
            "license_type": r.get("license_type") or "",
            "price_ils":    float(r.get("price_ils") or 0),
            "sort_order":   int(r.get("sort_order") or 0),
        })
    return out


# ---------------------------------------------------------------------------
# POST /checkout/create
# ---------------------------------------------------------------------------
@router.post("/create", response_model=CheckoutCreateResponse)
def create_checkout(req: CheckoutCreateRequest, request: Request):
    """Create a pending order from a website checkout submission.

    Does NOT charge the customer — that happens at the payment
    provider. The returned ``order_id`` is used by:

    1. The website to key the "thank you / waiting for confirmation"
       page.
    2. The webhook handler to correlate the incoming payment
       notification with this order (via ``metadata.order_id`` passed
       to the payment provider).
    """
    name  = (req.customer_name  or "").strip()
    email = (req.customer_email or "").strip().lower()
    phone = (req.customer_phone or "").strip()
    plan_id = int(req.plan_id or 0)

    if not name:
        raise HTTPException(status_code=400, detail="יש לספק שם לקוח.")
    if not email or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="כתובת אימייל לא תקינה.")
    if plan_id <= 0:
        raise HTTPException(status_code=400, detail="יש לבחור תוכנית רישוי.")

    # Resolve the plan so we can snapshot its price + name on the order.
    plan = plans.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="התוכנית שנבחרה לא נמצאה.")
    if not plan.get("is_active"):
        raise HTTPException(status_code=400, detail="התוכנית שנבחרה אינה פעילה.")

    price_ils = float(plan.get("price_ils") or 0)
    amount_cents = int(round(price_ils * 100))

    row = orders.create_pending_order(
        plan_key=str(plan_id),
        customer_name=name,
        customer_email=email,
        customer_phone=phone,
        amount_cents=amount_cents,
        currency="ILS",
        provider="pending",  # real provider is attached after redirect
        raw_payload={
            "source":    "website",
            "client_ip": _client_ip(request),
            "user_agent": (request.headers.get("user-agent") or "")[:512],
            "plan_snapshot": {
                "name":         plan.get("name"),
                "days":         plan.get("days"),
                "license_type": plan.get("license_type"),
                "price_ils":    price_ils,
            },
        },
    )

    order_id = row.get("id")
    # NEW-SRV-6: avoid writing the raw email to the server log. The
    # ``orders`` row carries the full address; logs only need a
    # privacy-preserving hint (first char + domain) for triage.
    def _mask_email(addr: str) -> str:
        if not addr or "@" not in addr:
            return "***"
        user, dom = addr.split("@", 1)
        return (user[:1] + "***@" + dom) if user else ("***@" + dom)

    logger.info(
        "checkout/create: order=%s plan=%s email=%s amount=%s",
        order_id, plan_id, _mask_email(email), amount_cents,
    )

    # ``next_step`` is where the website should redirect the customer.
    # Until a payment provider is wired up, we return an empty string
    # and the website can show a "coming soon" message OR treat the
    # endpoint as a lead-capture only.
    return {
        "order_id": order_id,
        "status":   row.get("status") or "pending",
        "amount_cents": amount_cents,
        "currency": "ILS",
        "next_step": "",  # TODO: fill with provider-specific checkout URL
        "message":  "הזמנה נוצרה. אנא המשיכו לתשלום.",
    }
