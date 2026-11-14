# RAG Document Q&A Platform · Production-oriented SaaS edition

**FastAPI · LangChain · FAISS · PostgreSQL · Redis · Celery · S3 · Stripe**

A tenant-isolated document question-answering platform with durable ingestion, immutable document revisions, hybrid retrieval, grounded citations, account authentication, a customer dashboard, usage limits, billing webhooks, and deployment templates.

> **Readiness statement:** This is a production-oriented implementation, **not** a certification that any given deployment is secure, compliant, scalable, or commercially approved. You must configure your own managed services, secrets, DNS/TLS, payment account, backups and mailer; run the integration and load tests, perform a security review, then complete the go-live gates in `docs/PRODUCTION_CHECKLIST.md`. The authoring environment did **not** have working PyPI/Docker-network access, so live Redis/Celery/FAISS/OpenAI/Stripe/Docker integration has **not** been verified here.

## Architecture

```mermaid
flowchart TB
    User[Dashboard / API clients] --> API[FastAPI / JWT / hashed API keys / RBAC]
    API --> PG[(PostgreSQL\nTenants, versions, usage, audit)]
    API --> R[(Redis rate limits / Celery broker)]
    API --> S3[(Private S3 object storage)]
    R --> WORKERS[Celery ingestion workers]
    WORKERS --> S3
    WORKERS --> P[Parse / OCR / split / embed]
    P --> FAISS[Immutable FAISS + JSON shards]
    FAISS --> S3
    API --> RET[Dense + BM25 + RRF + optional rerank]
    RET --> LLM[OpenAI or Anthropic chat model]
    LLM --> API
    STRIPE[Stripe signed webhooks] --> API
    MAIL[SMTP transactional email] <-- API
```

### Implemented services

| Subsystem | Capability |
| --- | --- |
| Identity | JWT access, rotated refresh, Argon2 passwords, hashed/revocable API keys, owner/admin/member/viewer roles |
| Onboarding | Owner CLI bootstrap, email-verification signup, 48-hour invitations, sign-in console |
| Tenant boundary | Tenant ID enforced for every document, index, Q&A, API key, audit entry and usage operation |
| Documents | PDF/DOCX/TXT/MD, streamed capped uploads, file magic, SHA256, decompression controls, clamd scanning |
| Ingestion | DB queue/outbox repair, Celery late ack, worker leases, retries, immutable object storage, version promotion |
| Retrieval | FAISS cosine, BM25 hybrid, reciprocal-rank fusion, optional cross-encoder rerank, token-bounded context |
| Q&A | Grounded prompt, accepted-source citation syntax validation, abstain if no valid citations, structured JSON |
| Governance | Month quotas, storage and document count limits, audit trail, deletions/retention |
| Billing | Stripe Checkout, Customer Portal, signature-verified idempotent webhooks, paid-plan activation |
| Operations | Alembic migrations, readiness, metrics, JSON access logs, Sentry hook, Kubernetes/HPA examples, CI |
| Web dashboard | Document library, upload, Q&A source evidence, usage/billing, key creation, invitations |

## Start a local demo

Prerequisites: Docker Engine + Docker Compose plugin and an OpenAI API key. For scanned PDFs the container includes Tesseract. **Demo uses unencrypted local MinIO, PostgreSQL, Redis and Mailpit; never expose these containers to the public Internet.**

```bash
cp .env.example .env
# Edit .env and set OPENAI_API_KEY. Change development secrets for any shared environment.
docker compose up --build -d
# UI
open http://localhost:8000/app
# API docs
open http://localhost:8000/docs
# Mailpit inbox for sign-up verification emails
open http://localhost:8025
```

The default demo enables **verified registration via Mailpit**. Create a workspace on `/app`, then open the verification email in Mailpit and click the link. Alternatively bootstrap an owner using the CLI:

```bash
docker compose exec api python -m app.cli bootstrap \
  --tenant acme-research --name "Acme Research" --email admin@acme.example
```

You'll be prompted for a password (not put into shell history). Users can then sign in on `/app`.

### API flow

```bash
# 1. Sign in
curl -s http://localhost:8000/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"workspace_slug":"acme-research", "email":"admin@acme.example", "password":"YOUR_OWNER_PASSWORD"}'

# 2. Upload document (replace TOKEN with JSON access_token)
curl -s http://localhost:8000/v1/documents \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@examples/sample.md"

# 3. Poll the document status; processing occurs in Celery
curl -s http://localhost:8000/v1/documents \
  -H "Authorization: Bearer $TOKEN"

# 4. Ask a question after document status is ready
curl -s http://localhost:8000/v1/qa/query \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"What does the sample say?","top_k":5}'
```

**API key for integrations:** create one through `POST /v1/auth/keys` as an admin or via the CLI. Use the response once:

```bash
curl -s -H "X-API-Key: $RAG_API_KEY" http://localhost:8000/v1/documents
```

## Important API routes

| Method | Endpoint | Authorization |
| --- | --- | --- |
| POST | `/v1/auth/register/start`, `/v1/auth/register/verify` | Public (when enabled, verified by email) |
| POST | `/v1/auth/login`, `/v1/auth/refresh` | Public, rate limited |
| GET | `/v1/auth/me` | Signed-in user |
| POST | `/v1/auth/keys` | Human admin |
| GET / DELETE | `/v1/auth/keys`, `/v1/auth/keys/{id}` | Admin |
| POST | `/v1/auth/invitations` | Human admin |
| POST | `/v1/auth/invitations/accept` | Public, single-use invite |
| POST | `/v1/documents` | Member |
| POST | `/v1/documents/{id}/versions` | Member |
| GET | `/v1/documents`, `/v1/documents/{id}` | Viewer |
| GET | `/v1/documents/{id}/versions` | Viewer |
| POST | `/v1/documents/{id}/reindex` | Member |
| DELETE | `/v1/documents/{id}` | Member |
| DELETE | `/v1/documents/{id}/versions/{version_id}` | Admin; nonactive versions only |
| POST | `/v1/qa/query` | Viewer |
| GET | `/v1/workspace/usage` | Viewer |
| GET | `/v1/workspace/users`, `/v1/workspace/audit` | Admin |
| PATCH | `/v1/workspace/users/{id}` | Owner |
| POST | `/v1/billing/checkout/{starter,business}`, `/v1/billing/portal` | Owner |
| POST | `/v1/billing/stripe/webhook` | Stripe signature |
| GET | `/health/live`, `/health/ready` | Kubernetes/load balancer |
| GET | `/internal/metrics` | X-Metrics-Token header, private only |

### Example structured answer

```json
{
  "answer": "The sample permits returns within 30 days [S1].",
  "grounded": true,
  "citations_valid": true,
  "sources": [{
    "source_id": "S1", "document_id": "UUID", "version_id": "UUID",
    "filename": "policy.md", "page": null, "chunk_index": 0,
    "similarity": 0.89, "rank_score": 0.0164,
    "snippet": "Unused items may be returned within 30 days."
  }],
  "model": "gpt-4.1-mini", "input_tokens": 145, "output_tokens": 23
}
```

**Grounded ≠ proven true:** `citations_valid` checks that the model used at least one identifier from the supplied sources and none outside it; it does not formally verify that every claim follows from the evidence. Human review and offline golden-set evaluations remain necessary.

## Configuration

| Variable | Default / requirements |
| --- | --- |
| `APP_ENV` | `development`, `test`, `production` (production is fail-closed) |
| `DATABASE_URL` | PostgreSQL+psycopg, SSL required by your DB configuration |
| `REDIS_URL` | TLS `rediss://` enforced in production |
| `JWT_SECRET`, `API_KEY_PEPPER` | Separate high-entropy secrets, not embedded in source |
| `STORAGE_BACKEND`, `S3_BUCKET`, `S3_REGION` | Private S3-compatible bucket in production |
| `CLAMAV_ENABLED` | Required in production; malware scan fail-closed |
| `OPENAI_API_KEY`, `OPENAI_EMBEDDING_MODEL` | OpenAI embeddings |
| `LLM_PROVIDER` | `openai` or `anthropic` for chat generation |
| `RETRIEVAL_MODE` | `hybrid` or `dense` |
| `RERANKER` | `none` or `cross-encoder` (requires advanced requirements & model preloading) |
| `SMTP_ENABLED`, `SMTP_*` | SMTP for verified onboarding; production enforces STARTTLS |
| `STRIPE_ENABLED`, `STRIPE_*` | Stripe subscriptions, prices and signed webhooks |
| `ENABLE_PUBLIC_SIGNUP` | False by default in production; verification and abuse controls required |
| `AUDIT_RETENTION_DAYS` | 90 days; billing events remain for reconciliation |

See `.env.production.example` and `docs/PRODUCTION_CHECKLIST.md` for safe deployment details.

## Testing

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q app alembic
node --check app/web/app.js
alembic upgrade head
alembic check
```

Model calls are mocked in tests; real embedding / LLM / S3 / Redis / Stripe end-to-end tests and load tests must additionally run in staging. `scripts/evaluate.py` is a golden-set smoke/evaluation runner; `scripts/loadtest.py` requires `pip install locust`.

## Shipping to production

1. Provision managed PostgreSQL (automated backup + point-in-time restore), TLS Redis with durable AOF, private object storage with versioning/SSE and lifecycle policy, malware scanner, TLS DNS ingress, SMTP and optional Stripe and Sentry accounts.
2. Populate secrets using an external secrets manager, including correct `DATABASE_URL`, `REDIS_URL`, IAM / S3 settings and model provider keys. Set `APP_ENV=production`.
3. Build/scan a version-tagged image and pin that digest in Kubernetes. Run the **migration Job** from `deploy/production-migration-job.yaml` before deploying the new image.
4. Deploy `deploy/kubernetes.yaml` after adjusting domain, image, secret references, resources, network policy and real secret values. **Only one Celery Beat**.
5. Validate billing webhooks through Stripe CLI/test mode, invite emails, tenant isolation, document version updates, PDF OCR, malware scanning, backup restoration and metrics alerts.
6. Run golden-set retrieval tests, soak/load tests, vulnerability scanning, pen test and the release gates in the checklist. Do not claim supported concurrency or SLAs until measured.

More detail: `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`, `docs/PRODUCTION_CHECKLIST.md`.

## Scope boundaries

- **New schema:** this v2 implementation has Alembic migrations for **fresh installations**; it does not automatically migrate the earlier single-workspace `documents` table, disk indexes or metadata. Back up and create an explicit data migration before applying to an existing v1 environment.
- **Billing:** fixed recurring Stripe plans with internal usage quotas; not metered Stripe invoicing, tax accounting, refunds or PCI certification. Configure legal commercial terms externally.
- **OCR:** Tesseract-based OCR, not guaranteed to reconstruct complex layouts, handwriting or tables faithfully. Validate on your document mix.
- **Vector scale:** each document has a separate immutable FAISS shard, with a configurable max of 50 documents per query; large enterprise corpora may warrant consolidated sharded indexes or a managed vector engine.
- **Privacy:** documents and questions may be transmitted to third-party LLM providers; customer disclosures, DPA and residency requirements are operational responsibilities.
