# Architecture and design decisions

## Data flow

**Upload:** multipart upload streams into size-capped temporary file → verify extension and file header → malware scan (mandatory in production) → acquire PostgreSQL tenant row lock → check quota → write private S3 object with server-generated key → commit document and version records → best-effort enqueue. If Redis is down, Celery Beat rescans queued versions from PostgreSQL every 45s.

**Ingestion:** Celery attempts atomic claim on a `document_versions` row, tracking worker token, attempts, expiration/lease. Worker verifies SHA256, scans again, parses PDF/DOCX/TXT/MD, performs OCR selectively, chunks with LangChain, embeds batches, writes FAISS + JSON metadata, uploads both to immutable S3 prefix, and then atomically flips `documents.active_version_id` to the ready version. A previous published version stays queryable until promotion completes. Late or duplicated workers cannot publish after document deletion or replacement.

**Retrieval:** authenticate principal → enforce role, tenant and rate-limit → select published `(DocumentRecord, DocumentVersion)` rows by tenant → embed query → verify checksummed index in bounded local cache → FAISS cosine and optional BM25 → RRF fusion → optional local cross-encoder → select top K with context cap → call configured chat provider → validate cited source tokens → record usage.

## SQL schema

- `tenants`: workspace + plan, billing, stored-byte accumulator.
- `users`: tenant membership, Argon2 password hash, role and token-version revocation.
- `api_keys`: tenant-scoped prefix + HMAC-SHA256 secret hash, revocation, never plaintext.
- `refresh_tokens`: opaque refresh secret hash; each rotation revokes the previous row.
- `invitations`, `signup_intents`: hashed, single-use expiring onboarding tokens.
- `documents`: tenant key, active and pending version pointers, tombstone.
- `document_versions`: immutable input/checksum/source, status and queue lease, published FAISS checksum, page and chunk counts.
- `usage_periods` and `usage_events`: monthly quotas and append-only event history.
- `audit_events`: tenant-scoped action tracking.
- `billing_webhook_events`: webhook delivery deduplication.

## Boundaries and tradeoffs

- Per-version FAISS shards are simple and recoverable but involve O(documents) index lookups; cap per-query shards and scale to a separate query-serving vector tier if global retrieval requirements grow.
- The DB is source of truth; Redis is the delivery mechanism and rate limiter. Do not run two Celery Beats.
- S3 is the durable file + index store. Local `data/cache` is disposable and must not be used as authoritative state.
- PostgreSQL tenant row locks enforce quota reserve atomicity, at cost of serialized uploads and queries per tenant at high throughput. Replace with a dedicated transactional quota ledger when benchmarks show contention.
- The API uses synchronous SQLAlchemy calls inside normal endpoints; upload endpoint offloads blocking blob persistence to threadpool.
- Index metadata is JSON (no pickled LangChain index deserialization). Index file SHA256 is stored in PostgreSQL and verified on load.
- `grounded` indicates valid structural citations, **not** semantic entailment. Track full citation and answer factuality through labeled evaluations.
- Audit/event retention runs hourly; deleted documents are tombstoned immediately and physically purged later, including object storage. Operator backup and lifecycle policies must honor contractual deletion periods.
