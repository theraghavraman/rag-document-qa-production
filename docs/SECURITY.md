# Security model and threat checklist

## Implemented controls

- Access JWT is HS256 with exp, issuer, audience, nonce and `token_version`, short lifetime. Refresh secrets are opaque 48-byte random tokens with hashed storage and single-use rotation. Passwords use Argon2id parameters configured in `app/security.py`.
- Hashed API keys use HMAC-SHA256 with a separate pepper; only a limited prefix is stored for lookup. Keys are revocable, roles are limited to read/member/admin, and cleartext is displayed only at issuance.
- Every document, version, question lookup, user record, audit and credential query is authorized against the authenticated tenant. IDs never grant authority by themselves.
- Redis rate limits are atomic and unavailable Redis returns 503 rather than disabling limits. Public login/register endpoints are rate-limited by client IP, selected from trusted proxy headers only if the TCP peer is in `TRUSTED_PROXY_CIDRS`. Strip untrusted incoming X-Forwarded-For at ingress.
- Size-capped streaming uploads, PDF/DOCX header checks, DOCX decompression bomb safeguards, max pages/characters/chunks, clamd malware scanner (fail closed), OCR page/pixel/time limits.
- Server-generated S3 object keys, S3 AES-256 server-side encryption, private access expected, no untrusted filenames in paths, checksum-verified FAISS index and JSON metadata instead of pickle.
- Static UI uses `textContent`, strict same-origin CSP, no third-party script URLs, no auth tokens in localStorage or cookies; refresh and access tokens are only in process memory.
- XSS-risk controls: no HTML rendering of untrusted source text. LLM prompt explicitly treats document excerpts as untrusted instructions, but prompt injection remains a risk; do not grant an LLM tool privileges based on document content.
- Stripe events verify raw-body webhook signatures, maintain event IDs for deduplication and guard against older subscription webhooks overriding newer state.
- Container drops capabilities/runs nonroot, Kubernetes sample uses read-only root filesystem and resource caps.

## REQUIRED external controls before launch

1. Require TLS 1.2+ at ingress, PostgreSQL and Redis; use a certificate manager, WAF, DDoS protection and fail-closed network ACLs.
2. Use IAM workload identity for S3. Put SSE-KMS / object versioning / object lock or lifecycle controls under organizational security policy; use a restricted prefix per deployment.
3. Provision clamd with updated signatures; monitor scanning availability and fail uploads if unavailable.
4. Restrict access to `/internal/metrics`, database ports, Redis, MinIO, S3 and SMTP. Add network policies to permit only necessary service/provider egress.
5. Scan dependencies and container images, generate SBOM, sign images, review third-party model/provider data-processing terms and evaluate data residency.
6. Use an independent pentest, secrets rotation drills, backup-restore test and incident-response runbook before accepting customer documents.
7. Set identity-provider SSO/MFA if required by enterprise prospects. This sample uses password + JWT and **does not implement SAML/OIDC enterprise SSO or MFA**.
8. Consider per-document authorization beyond tenant roles where teams share a workspace. Current product enforces **workspace-level RBAC**, not document-by-document ACLs.
9. Verify logs do not contain content, secrets or PII; the HTTP logger records method/template/status/latency/request-id only.
10. Confirm data deletion in backups and retention windows meets legal/DPA commitments; logical deletion precedes asynchronous physical purge.

## Residual risks

- HTTP API tokens held in browser JS memory are exposed during a successful XSS; CSP + text-only DOM reduces but cannot remove that risk.
- Public trial registration without bot protection beyond IP throttling should remain disabled until anti-abuse testing, CAPTCHA/turnstile and domain policies are in place.
- API key and JWT validation runs against DB on every request. Add DB connection pooling and performance tests at the desired concurrency.
- Background parsing of untrusted PDFs and DOCX is CPU-intensive; isolate workers in constrained pods, not on the database host.
- Optional neural reranker may download models from third parties; pre-download, scan and pin model artifacts before production.
