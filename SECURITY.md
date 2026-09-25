# Security Policy

PipelineGPT handles pipeline-integrity records, and its engineer-review step gates operational advice. We take vulnerabilities seriously.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub:

- Backend and deployment bundle: [report a vulnerability](https://github.com/Kubu-Ventures/pipe-line_gpt-backend/security/advisories/new)
- Frontend: [report a vulnerability](https://github.com/Kubu-Ventures/pipe-line_gpt-frontend/security/advisories/new)

Please include the affected version or commit, reproduction steps, and the impact you observed. Proofs of concept are welcome. Please test only against your own deployment.

What to expect:

| Step | Target |
|---|---|
| Acknowledgement | within 3 business days |
| Initial assessment and severity | within 7 days |
| Fix or mitigation for critical/high issues | within 30 days |

We coordinate disclosure with you, publish a GitHub Security Advisory with the fixed version, and credit you unless you prefer otherwise.

## Supported versions

Security fixes go into the latest minor release. Self-hosted operators should follow releases and upgrade with `deploy/upgrade.sh`.

| Version | Supported |
|---|---|
| latest minor (0.x) | ✅ |
| older | ❌ |

## Scope

Especially interesting:

- **Authentication and access control:** MFA bypass, token scope escalation, role checks
- **Engineer-review (HITL) bypass:** any way for a flagged answer to reach an operator before approval
- **Cross-user data exposure:** another user's history, cached answers, audit entries
- **Injection:** SQL injection, prompt injection that leaks other documents or bypasses review, CSV/formula injection in exports
- **Deployment defaults** in `deploy/` that expose services or secrets

Out of scope: findings that need a compromised host or a malicious administrator, denial of service by volume, and missing hardening headers without a demonstrated impact.

## Hardening checklist for operators

- Run with `ENVIRONMENT=production` (the bundled compose file does). It refuses weak secrets.
- Keep `deploy/.env` private (`chmod 600`) and back it up securely.
- Only ports 80/443 should be reachable. Postgres, Redis, the API and Flower stay on internal networks.
- Keep `DEMO_MODE=false` for real data.
- Schedule `deploy/backup.sh` and test a restore.
