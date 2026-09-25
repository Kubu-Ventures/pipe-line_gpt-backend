# Deploying PipelineGPT (self-hosted)

This bundle runs the complete PipelineGPT stack on one Linux server with Docker:

| Service | Image | Role |
|---|---|---|
| `caddy` | `caddy:2` | HTTPS (automatic Let's Encrypt certificates), routes `/` → frontend, `/backend/*` → API |
| `frontend` | `ghcr.io/kubu-ventures/pipelinegpt-frontend` | Web app |
| `api` | `ghcr.io/kubu-ventures/pipelinegpt-backend` | REST/SSE API; applies database migrations on start |
| `worker` | same image | Document ingestion (parsing, embeddings, insights) |
| `postgres` | `pgvector/pgvector:pg16` | Data, embeddings, audit trail |
| `redis` | `redis:7` | Task queue, cache, rate limits |

Only ports 80 and 443 are exposed. The database and Redis sit on a network with no internet access. Embedding, reranking and PII models are bundled in the image; the only outbound calls are to the Anthropic API (and Let's Encrypt).

## Requirements

- **Host:** Linux x86-64, 4 vCPU, 8 GB RAM, 40 GB disk minimum (8 vCPU / 16 GB recommended for large PDF corpora)
- **Software:** Docker Engine 24+ with the Compose v2 plugin, `openssl`
- **Network:** a DNS name pointing at the server, and ports 80/443 open to users. Port 80 is needed for certificate issuance.
- **Access:** an [Anthropic API key](https://console.anthropic.com/). Questions and retrieved document excerpts are sent to Anthropic to generate answers; review this against your data-handling policies.

## Install

```bash
# Download the bundle for a release (see GitHub Releases), then:
tar -xzf pipelinegpt-deploy-<version>.tar.gz
cd pipelinegpt
sha256sum -c ../pipelinegpt-deploy-<version>.tar.gz.sha256   # optional integrity check
./install.sh
```

The installer:

1. asks for your domain, a certificate-notice email, and the Anthropic key;
2. writes `.env` with freshly generated secrets (mode 600);
3. pulls the images and starts the stack;
4. waits for the API to become healthy;
5. creates the first administrator (it prompts for a 12+ character password).

Open `https://<your-domain>`, sign in, and enroll an authenticator app (Microsoft/Google Authenticator, 1Password, …). MFA is mandatory for engineers and admins.

Then invite your team from **Admin → Invite**. Registration is invite-only, and invitation links expire after 48 hours.

## Operating

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f api worker` |
| Restart | `docker compose restart` |
| Stop / start | `docker compose down` / `docker compose up -d` (data is kept in volumes) |
| Another admin | `docker compose run --rm api create-admin someone@company.com` |
| Task monitor | `docker compose --profile ops up -d flower`, then `ssh -L 5555:127.0.0.1:5555 <server>` and open http://localhost:5555 |

**Monitoring:**
- `https://<domain>/backend/health` returns 200 when the API, database and Redis are healthy, and 503 otherwise. Point your uptime monitor at it.
- Prometheus metrics are at `api:8000/metrics` on the internal network. Caddy blocks them publicly.

## Backups

```bash
./backup.sh                  # writes backups/pipelinegpt-<timestamp>.dump, keeps the newest 14
KEEP=30 ./backup.sh /mnt/nas # custom location and retention
```

Schedule it daily, and **copy the dumps and `.env` off the server**:

```cron
30 2 * * * /opt/pipelinegpt/backup.sh >> /var/log/pipelinegpt-backup.log 2>&1
```

The dump contains every document chunk, embedding, user, review decision and audit event. Protect it like the source data.

To restore (this replaces the current data): `./restore.sh backups/pipelinegpt-<timestamp>.dump`. Test a restore on a spare machine at least once.

## Upgrades

```bash
./upgrade.sh 0.3.0
```

This backs up the database, switches `PIPELINEGPT_VERSION`, pulls the new images and restarts; migrations run automatically. Read the release notes first; while on 0.x, minor versions can include breaking changes. To roll back, restore the pre-upgrade backup and run `./upgrade.sh <previous-version>`.

## Configuration reference (`.env`)

| Variable | Default | Notes |
|---|---|---|
| `PIPELINEGPT_DOMAIN` | required | Public hostname |
| `ACME_EMAIL` | required | Let's Encrypt account email |
| `PIPELINEGPT_VERSION` | required | Image tag to run |
| `ANTHROPIC_API_KEY` | required | |
| `LLM_MODEL` | `claude-sonnet-4-6` | Claude model used for answers |
| `JWT_SECRET`, `NEXTAUTH_SECRET`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | generated | Don't change after install: sessions and data access depend on them |
| `MAX_TOKENS_PER_DAY` | `100000` | Per-user daily Claude token budget |
| `UPLOAD_MAX_BYTES` | `52428800` | Max upload size (50 MB) |
| `HITL_CONFIDENCE_THRESHOLD` | `0.75` | Answers below this confidence go to engineer review |
| `WEB_CONCURRENCY` / `WORKER_CONCURRENCY` | `2` / `2` | API processes / parallel ingestion tasks |
| `DEMO_MODE` | `false` | Demo instances only. Never enable with real data. |

After editing `.env`, apply the changes with `docker compose up -d`.

## Troubleshooting

- **Certificate not issued:** check that DNS resolves to this server and that port 80 is reachable from the internet, then run `docker compose logs caddy`.
- **"The AI service is misconfigured":** the Anthropic key is wrong or out of credits. Fix `ANTHROPIC_API_KEY` in `.env`, then run `docker compose up -d`.
- **Uploads stuck in PROCESSING:** check `docker compose logs worker`. Large scanned PDFs without a text layer produce no text; OCR them first.
- **Locked out of an admin account (lost authenticator):** another admin can reset MFA from the admin page. If you have no other admin, run `docker compose run --rm api create-admin recovery@company.com`.

## License

PipelineGPT is free software under the [GNU Affero General Public License v3.0](LICENSE). If you modify it and make it available to users over a network, the AGPL requires you to offer them your modified source. Copyright (C) 2026 Collins Kubu. Commercial licenses (for use outside the AGPL's terms) and support are available from the author.
