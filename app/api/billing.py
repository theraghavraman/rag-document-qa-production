"""Optional Stripe checkout, portal, signed and idempotent webhook processing."""
import json
import logging
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import BillingWebhookEvent, Tenant
from app.security import Principal, require_role
from app.services.usage import audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v1/billing', tags=['billing'])


def _stripe():
    if not get_settings().stripe_enabled:
        raise HTTPException(503, 'Billing integration has not been configured')
    import stripe
    stripe.api_key = get_settings().stripe_secret_key
    return stripe


@router.post('/checkout/{plan}')
def create_checkout(plan: str, principal: Principal = Depends(require_role('owner')),
                    db: Session = Depends(get_db)):
    if plan not in {'starter', 'business'}:
        raise HTTPException(422, 'Choose starter or business')
    stripe = _stripe()
    s = get_settings()
    price = s.stripe_starter_price_id if plan == 'starter' else s.stripe_business_price_id
    if not price:
        raise HTTPException(503, 'Plan price ID has not been configured')
    tenant = db.get(Tenant, principal.tenant_id)
    if tenant.stripe_subscription_id and tenant.billing_status in {'active', 'trialing'}:
        raise HTTPException(409, 'An active subscription exists; use the billing portal')
    kwargs = {'mode': 'subscription', 'line_items': [{'price': price, 'quantity': 1}],
              'client_reference_id': tenant.id, 'metadata': {'tenant_id': tenant.id, 'plan': plan},
              'subscription_data': {'metadata': {'tenant_id': tenant.id}},
              'success_url': s.public_app_url + '/app?billing=success',
              'cancel_url': s.public_app_url + '/app?billing=cancelled'}
    if tenant.stripe_customer_id:
        kwargs['customer'] = tenant.stripe_customer_id
    try:
        session = stripe.checkout.Session.create(**kwargs)
    except Exception:
        logger.exception('Stripe checkout creation failed')
        raise HTTPException(502, 'Billing provider unavailable') from None
    audit(db, tenant.id, principal.actor_id, 'billing.checkout_started')
    db.commit()
    return {'url': session.url}


@router.post('/portal')
def portal(principal: Principal = Depends(require_role('owner')), db: Session = Depends(get_db)):
    stripe = _stripe()
    tenant = db.get(Tenant, principal.tenant_id)
    if not tenant.stripe_customer_id:
        raise HTTPException(409, 'No Stripe customer for workspace')
    try:
        session = stripe.billing_portal.Session.create(
            customer=tenant.stripe_customer_id,
            return_url=get_settings().public_app_url + '/app')
    except Exception:
        logger.exception('Stripe portal failed')
        raise HTTPException(502, 'Billing portal unavailable') from None
    return {'url': session.url}


@router.post('/stripe/webhook', include_in_schema=False)
async def stripe_webhook(request: Request, stripe_signature: str | None = Header(None, alias='Stripe-Signature')):
    stripe = _stripe()
    if not stripe_signature:
        raise HTTPException(400, 'Missing Stripe signature')
    payload = await request.body()
    try:
        stripe.Webhook.construct_event(payload, stripe_signature,
                                       get_settings().stripe_webhook_secret)
        # Signature verified above. Read the same verified bytes as plain JSON:
        # stripe-python StripeObject no longer supports dict methods like .get().
        event = json.loads(payload)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, 'Invalid Stripe webhook signature') from None
    if not isinstance(event, dict) or not isinstance(event.get('data'), dict):
        raise HTTPException(400, 'Malformed Stripe webhook payload')
    event_id = event['id']
    created = int(event.get('created', 0))
    event_type = event['type']
    data = event['data']['object']
    with SessionLocal.begin() as db:
        if db.get(BillingWebhookEvent, event_id):
            return {'received': True, 'duplicate': True}
        # Only recognized signed events affect billing; unknown are acknowledged.
        tenant = None
        if event_type == 'checkout.session.completed':
            tid = (data.get('metadata') or {}).get('tenant_id')
            tenant = db.scalar(select(Tenant).where(Tenant.id == tid).with_for_update()) if tid else None
            if tenant:
                customer_id = data.get('customer')
                if customer_id and (not tenant.stripe_customer_id or tenant.stripe_customer_id == customer_id):
                    tenant.stripe_customer_id = customer_id
                    tenant.stripe_subscription_id = data.get('subscription')
                    # Actual plan is derived from subscription price webhook (not user metadata).
        elif event_type in {'customer.subscription.created', 'customer.subscription.updated',
                            'customer.subscription.deleted'}:
            customer_id = data.get('customer')
            tenant = db.scalar(select(Tenant).where(Tenant.stripe_customer_id == customer_id).with_for_update())
            if not tenant:
                tenant_id = (data.get('metadata') or {}).get('tenant_id')
                tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update()) if tenant_id else None
            if tenant and created and created < tenant.last_billing_event_created:
                tenant = None  # Out-of-order event; do not revert the current subscription.
            if tenant and (tenant.stripe_customer_id is None or tenant.stripe_customer_id == customer_id):
                tenant.last_billing_event_created = max(created, tenant.last_billing_event_created)
                tenant.stripe_customer_id = customer_id
                tenant.stripe_subscription_id = data.get('id')
                tenant.billing_status = data.get('status', 'unpaid')
                if event_type == 'customer.subscription.deleted':
                    tenant.plan = 'trial'
                    tenant.billing_status = 'suspended'
                else:
                    price_ids = {item.get('price', {}).get('id')
                                 for item in (data.get('items') or {}).get('data', [])}
                    if get_settings().stripe_business_price_id in price_ids:
                        tenant.plan = 'business'
                    elif get_settings().stripe_starter_price_id in price_ids:
                        tenant.plan = 'starter'
                audit(db, tenant.id, 'stripe', 'billing.subscription_updated')
        db.add(BillingWebhookEvent(id=event_id, event_type=event_type))
    return {'received': True}
