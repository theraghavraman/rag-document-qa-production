# Go-live and commercial acceptance gates

**Do not label this deployment production-ready until these checks are completed with evidence in staging.**

## Security and tenancy

- [ ] TLS ingress, WAF, network policy, secrets manager, production signing keys, private S3 and trusted SMTP domains configured.
- [x] Ingress request-body limit (default 22 MiB via `MAX_REQUEST_BODY_MB` middleware for 20 MiB file cap) enforced in ASGI middleware alongside gateway upload timeouts.
- [ ] clamd and definition updates healthy; upload fails closed when scanner unavailable.
- [ ] Independent review covers JWT refresh rotation, key revocation, SQL injection, file uploads, SSRF, CORS, XSS, CSRF model, and webhooks.
- [ ] Pen test proves no cross-tenant data leak, including guessing document UUIDs, version UUIDs, old revisions, audit and usage endpoints.
- [ ] Data processor contracts and privacy notices cover external embedding/LLM/SMTP processors, storage region, retention and deletion.
- [ ] MFA/SSO and per-document ACLs added if contracted enterprise customers require them (not in current implementation).

## Platform and reliability

- [ ] `python -m pytest -q`, `alembic check` and dependency/container vulnerability scan PASS in CI.
- [ ] Real PDF (scanned + searchable), DOCX (tables), invalid ZIP/PDF, oversized files, malware test fixture, version rollover and delete scenarios PASS.
- [ ] Staging integration with PostgreSQL, Redis, S3, Celery, OpenAI/Anthropic and SMTP passes without mocks.
- [ ] Queue outage, expired-lease recovery, worker hard kill, object store outage, and concurrent version uploads tested.
- [ ] Backups are tested by an actual restore, and deletion/retention responsibilities are documented.
- [ ] Celery Beat singleton enforced, worker resources and queue alerts provisioned.
- [x] API/server logs contain no document contents, auth tokens, query contents or plaintext API keys (`SecretRedactionFilter` active on `rag` logger).

## AI quality and cost

- [ ] Label a representative, customer-approved golden question set per document type and language.
- [ ] Target citation precision/recall, abstention quality and hallucination benchmarks are met.
- [x] `scripts/evaluate.py` reports p50/p95/mean latency, citation validity, and abstention accuracy.
- [ ] Run `python scripts/evaluate.py examples/evaluation.jsonl` with curated expected answers.
- [ ] Run `locust -f scripts/loadtest.py` with staging customers and required concurrency for 30+ min; report failure rate, throughput, p95 latency, queue growth, CPU, DB connection saturation and vendor quota behavior.
- [ ] No concurrency/availability/SLA claims until measured; 1,000 simultaneous requests is a **load-test target**, not an attested capacity.

## Commercial

- [ ] Stripe test-mode and live-mode Checkout/portal/webhook signature tests, replays and subscription cancel/downgrade all pass.
- [ ] Subscription plan entitlements, trial limits, retention, taxes, refunds, billing support and invoice responsibilities are approved.
- [ ] Verified registration + invitation emails tested on real sender domain with SPF/DKIM/DMARC.
- [ ] Customer onboarding documents, DPA, privacy notice, terms, support SLA, incident-response contacts and status page published.
- [ ] Customer demo (signup → upload → ready → query with sources → team API key → usage → billing) runs end-to-end.
- [ ] Sales FAQ and limitations clearly explain that citations provide evidence pointers, not legally guaranteed correctness.

## Release record

| Gate | Owner | Date | Evidence link | Result |
| --- | --- | --- | --- | --- |
| CI + vulnerability | Engineering | | | Pending |
| End-to-end staging | Engineering | | | Pending |
| Tenant isolation audit | Security | | | Pending |
| Disaster recovery drill | SRE | | | Pending |
| Load/soak test | Performance | | | Pending |
| RAG golden set | AI/QA | | | Pending |
| Stripe and email | Finance/Ops | | | Pending |
| Contract / DPA | Legal | | | Pending |
