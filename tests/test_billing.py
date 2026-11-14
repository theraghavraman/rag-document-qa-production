"""Stripe signature, webhook replay and out-of-order event regression."""
import hashlib
import hmac
import json
import time


def test_stripe_signed_webhook_replay_and_ordering(client, headers, monkeypatch):
    import stripe
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Tenant
    from sqlalchemy import select
    s = get_settings()
    monkeypatch.setattr(s, 'stripe_enabled', True)
    monkeypatch.setattr(s, 'stripe_secret_key', 'sk_test_dummy')
    monkeypatch.setattr(s, 'stripe_webhook_secret', 'whsec_unit_test_secret')
    monkeypatch.setattr(s, 'stripe_business_price_id', 'price_business_test')
    monkeypatch.setattr(s, 'stripe_starter_price_id', 'price_starter_test')
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).where(Tenant.slug == 'alpha'))
        tenant_id = tenant.id

    def post(event):
        raw = json.dumps(event, separators=(',', ':')).encode()
        now = int(time.time())
        sig = hmac.new(s.stripe_webhook_secret.encode(), str(now).encode() + b'.' + raw,
                       hashlib.sha256).hexdigest()
        return client.post('/v1/billing/stripe/webhook', data=raw,
                           headers={'Stripe-Signature': f't={now},v1={sig}', 'Content-Type': 'application/json'})

    event = {'id': 'evt_checkout_1', 'object': 'event', 'created': 2000000000,
             'type': 'checkout.session.completed', 'data': {'object': {
                 'object': 'checkout.session', 'customer': 'cus_fixture', 'subscription': 'sub_fixture',
                 'metadata': {'tenant_id': tenant_id}}}}
    assert post(event).status_code == 200
    assert post(event).json()['duplicate'] is True
    updated = {'id': 'evt_subscription_new', 'object': 'event', 'created': 2000000100,
               'type': 'customer.subscription.updated', 'data': {'object': {
                   'object': 'subscription', 'customer': 'cus_fixture', 'id': 'sub_fixture',
                   'status': 'active', 'items': {'data': [{'price': {'id': 'price_business_test'}}]}}}}
    assert post(updated).status_code == 200
    outdated = {'id': 'evt_subscription_old', 'object': 'event', 'created': 2000000001,
                'type': 'customer.subscription.updated', 'data': {'object': {
                    'object': 'subscription', 'customer': 'cus_fixture', 'id': 'sub_fixture',
                    'status': 'unpaid', 'items': {'data': [{'price': {'id': 'price_starter_test'}}]}}}}
    assert post(outdated).status_code == 200
    with SessionLocal() as db:
        tenant = db.get(Tenant, tenant_id)
        assert tenant.plan == 'business'
        assert tenant.billing_status == 'active'
    assert client.post('/v1/billing/stripe/webhook', data=json.dumps(event),
                       headers={'Stripe-Signature': 'invalid'}).status_code == 400
