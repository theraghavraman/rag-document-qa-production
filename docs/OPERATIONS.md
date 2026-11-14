# Deployment, recovery and operations runbook

## Baseline prerequisites

- Managed PostgreSQL 16+ with automated PITR and encrypted snapshots.
- TLS Redis broker with persistence and no-eviction policy; sufficiently long visibility timeout relative to worker hard-time limit.
- Private S3 bucket with IAM and versioning; separate backup/lifecycle policy. MinIO in development only.
- API replicas behind TLS ingress; workers scaled separately; exactly one Celery Beat; durable object keys are tenant/version-scoped.
- Transactional SMTP + email/domain authentication. Stripe paid-plan prices, test/live keys and registered signed webhook URL. Optional Sentry or external OTEL agent.

## Migrate and provision

```bash
# Never use AUTO_CREATE_TABLES=true in production.
alembic upgrade head
alembic check
python -m app.cli bootstrap --tenant customer-slug --name 'Customer Name' --email owner@example.com
```

Run database backups **before** every schema migration. Review Alembic SQL on staged copy of production data. Do not downgrade a live billing/database schema without a recovery plan.

## Health and observability

- `GET /health/live`: process responds.
- `GET /health/ready`: DB, Redis and S3 accessible. Unready pods must leave ingress rotation.
- `GET /internal/metrics` with secret header (private service network): HTTP request count/duration, Prometheus counters. Each API pod uses **one** uvicorn worker for correct pod-level scraping.
- Structured one-line JSON request logs with `X-Request-ID`; no document text or question content.
- Sentry DSN optional; notifications and dashboards should alert on >1% 5xx for 5m, p95 Q&A latency above a measured SLO, worker retry spikes, broker queue backlog, ingestion age >5m, invalid webhook signatures and storage nearing quota.

## Failure behavior

| Failure | Expected behavior | Operator action |
| --- | --- | --- |
| Redis outage | New API auth calls return 503 from fail-closed throttle; previously stored ingest jobs remain `queued` and are retried when Redis returns | Restore broker and Beat; inspect stuck versions |
| Worker crash | DB lease expires; Beat requeues; duplicate messages cannot publish stale versions | Check API/model quota and worker memory |
| OpenAI/Anthropic outage | Q&A returns 502 and a request ID; ingestion handles transient provider errors via retry | Inspect provider status/quota and traffic spikes |
| S3 outage | Ingestion errors, readiness 503, uploads return 503, Q&A cached indexes may work until read needs S3 | Restore object store/IAM and verify object manifests |
| Invalid/malicious document | Version fails with sanitized error; last active version remains queryable | Review worker logs with request/job ID, ask user for safe copy |
| Stripe outage | Checkout/portal returns 502; subscription webhooks replay safely by event ID | Use Stripe dashboard and resend events |
| clamd outage | Upload/processing fails closed | Restore scanning service and signature updates |

## Disaster recovery drill

1. Record current PostgreSQL WAL recovery point and S3 inventory/versioning status.
2. Restore a fresh database into a private test environment, not the primary database.
3. Validate doc↔version foreign keys, `active_version_id` pointers, version SHA256 references and billing customer IDs.
4. Restore object store versions and verify several `index.faiss` checksums against PostgreSQL.
5. Start API, workers and a **single** Beat against the test restoration. Confirm read-only queries and a safe test ingestion.
6. Record achieved RPO/RTO; only publish contractual targets after repeatable evidence.

## Incident checklist

- Immediately revoke compromised API keys or disable the user (`token_version` invalidates existing JWT).
- Rotate JWT secret/pepper through scheduled key rotation; rolling change invalidates existing tokens/keys, so coordinate reissuance.
- Isolate compromised storage access with IAM. Do not disclose tenant data in error responses or logs.
- Reconcile Stripe webhook IDs, plan/status, and current data usage after a payment incident.
- Trigger customer/regulator notifications according to applicable contracts and law.
