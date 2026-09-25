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

Only ports 80 and 443 are exposed. The database and Redis sit on a network with no internet access. Embedding, reranking and PII models are bundled in the image; the only outbound calls are to the AI provider you choose (and Let's Encrypt).

## Requirements

- **Host:** Linux x86-64, 4 vCPU, 8 GB RAM, 40 GB disk minimum (8 vCPU / 16 GB recommended for large PDF corpora)
- **Software:** Docker Engine 24+ with the Compose v2 plugin, `openssl`
- **Network:** a DNS name pointing at the server, and ports 80/443 open to users. Port 80 is needed for certificate issuance.
- **AI provider (one of):** an [Anthropic API key](https://console.anthropic.com/), **or** Claude enabled in AWS Bedrock in your AWS account, **or** Claude enabled in Google Vertex AI in your Google Cloud project. Questions and retrieved document excerpts are sent to that provider to generate answers. With Bedrock or Vertex they are processed within your own cloud account, under your cloud's data terms. See [Choosing the AI provider](#choosing-the-ai-provider).

## Install

```bash
# Download the bundle for a release (see GitHub Releases), then:
tar -xzf pipelinegpt-deploy-<version>.tar.gz
cd pipelinegpt
sha256sum -c ../pipelinegpt-deploy-<version>.tar.gz.sha256   # optional integrity check
./install.sh
```

The installer:

1. asks for your domain, a certificate-notice email, and the AI provider with its settings;
2. writes `.env` with freshly generated secrets (mode 600);
3. pulls the images and starts the stack;
4. waits for the API to become healthy, then checks that the AI provider answers;
5. creates the first administrator (it prompts for a 12+ character password).

Open `https://<your-domain>`, sign in, and enroll an authenticator app (Microsoft/Google Authenticator, 1Password, …). MFA is mandatory for engineers and admins.

Then invite your team from **Admin → Invite**. Registration is invite-only, and invitation links expire after 48 hours.

**Unattended install** (configuration management, CI): set the answers as environment variables and the installer won't ask for them. Without a terminal, a missing answer stops it with the name of the variable. The variables are listed at the top of `install.sh`: `PIPELINEGPT_DOMAIN`, `ACME_EMAIL`, `LLM_PROVIDER` with that provider's settings, `ADMIN_EMAIL` and `ADMIN_PASSWORD`. Pass secrets through a file only root can read rather than on the command line, and delete it afterwards.

## Choosing the AI provider

| Provider | Data goes to | You need |
|---|---|---|
| Anthropic API | Anthropic | An API key |
| AWS Bedrock | Your AWS account | Claude enabled in Bedrock (console → Model access), and credentials allowed to invoke it |
| Google Vertex AI | Your Google Cloud project | Claude enabled in Vertex AI Model Garden, and a service account with the **Vertex AI User** role |

Embeddings, search and reranking always run on this server; only answer generation uses the provider.

**Data residency.** Both clouds offer *global* routing, which may process a request in any of their regions. If data must stay in one region, choose a regional option with your cloud team: on Vertex set `VERTEX_REGION` to a specific region (e.g. `europe-west4`); on Bedrock use a regional or geographic (US/EU/JP/AU) endpoint, which AWS prices about 10% higher. Your cloud's CloudTrail / Cloud Audit Logs record every call.

**AWS Bedrock credentials.** Either enter an access key during install (an IAM user or role whose policy allows `bedrock-mantle:CreateInference`), or leave it empty to use the server's IAM instance role. On EC2 the containers reach the instance role only if the instance metadata hop limit is 2 (`aws ec2 modify-instance-metadata-options --instance-id <id> --http-put-response-hop-limit 2`). Bedrock model IDs start with `anthropic.`. Bedrock offers Claude Sonnet 5 and newer (not Sonnet 4.6), so the installer defaults to `anthropic.claude-sonnet-5`. Sonnet 5 reasons before answering, which counts toward `MAX_TOKENS_PER_DAY`: raise it if users hit the daily limit.

**Google Vertex AI credentials.** The installer copies your service-account key to `secrets/gcp-key.json` (folder readable only by you) and enables `docker-compose.vertex.yml` through `COMPOSE_FILE` in `.env`. Back this file up with `.env`: it isn't in the database backups.

**Check or switch the provider.** Edit the provider variables in `.env` (see the configuration reference), run `docker compose up -d`, then:

```bash
docker compose run --rm api llm-check   # prints OK, or what is wrong
```

## Operating

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f api worker` |
| Restart | `docker compose restart` |
| Stop / start | `docker compose down` / `docker compose up -d` (data is kept in volumes) |
| Another admin | `docker compose run --rm api create-admin someone@company.com` |
| Check the AI provider | `docker compose run --rm api llm-check` |
| Import an archive folder | see [Importing an archive](#importing-an-archive) |
| Task monitor | `docker compose --profile ops up -d flower`, then `ssh -L 5555:127.0.0.1:5555 <server>` and open http://localhost:5555 |

**Monitoring:**
- `https://<domain>/backend/health` returns 200 when the API, database and Redis are healthy, and 503 otherwise. Point your uptime monitor at it.
- Prometheus metrics are at `api:8000/metrics` on the internal network. Caddy blocks them publicly.

## Importing an archive

To load years of existing records at once, copy them to the server and queue the whole folder, subfolders included, instead of uploading files one by one:

```bash
# See what would be imported, without changing anything
docker compose run --rm -v /srv/records:/import:ro api bulk-import /import --dry-run
# Queue it
docker compose run --rm -v /srv/records:/import:ro api bulk-import /import --operator you@company.com
```

- Supported files are PDF, CSV, TSV/TXT (PHMSA) and PHMSA ZIP. Anything else, empty files and files over `--max-mb` (default 200) are listed as skipped.
- Files whose content is already in the knowledge base are skipped, so an interrupted import can be run again with the same command.
- Each document is listed under its path inside the folder (e.g. `2009/ILI/segment-14.pdf`), which shows up in citations.
- The command only queues. The workers process the files in the background; follow progress on the Documents page or in the task monitor. Raise `WORKER_CONCURRENCY` in `.env` (about one per vCPU) to go faster.
- Queued files are held in the `uploads` volume until they are processed, so allow free disk space of about the size of the folder.
- Each document also gets one AI call to summarise it for the dashboard, which counts against your AI provider's usage.
- Scanned PDFs are read with OCR at a few seconds per page, so an archive of scans takes much longer to process than typed documents. Set `OCR_LANGUAGES` before importing if they aren't in English.

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
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `bedrock` or `vertex` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Claude model used for answers. Bedrock: `anthropic.claude-sonnet-5` (Bedrock IDs take the `anthropic.` prefix) |
| `ANTHROPIC_API_KEY` | required for `anthropic` | |
| `AWS_REGION` | required for `bedrock` | Region where Claude is enabled |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | empty | Bedrock only. Empty uses the server's IAM role |
| `VERTEX_PROJECT_ID` | required for `vertex` | Google Cloud project ID |
| `VERTEX_REGION` | `global` | `global` (routes across regions), `us`, `eu`, or a specific region for data residency |
| `COMPOSE_FILE` | unset | Vertex only: `docker-compose.yml:docker-compose.vertex.yml` mounts the key |
| `JWT_SECRET`, `NEXTAUTH_SECRET`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | generated | Don't change after install: sessions and data access depend on them |
| `MAX_TOKENS_PER_DAY` | `100000` | Per-user daily Claude token budget (includes reasoning tokens on models that think, e.g. Sonnet 5) |
| `UPLOAD_MAX_BYTES` | `52428800` | Max upload size (50 MB) |
| `OCR_ENABLED` | `true` | Read scanned PDF pages (no text layer) with OCR |
| `OCR_LANGUAGES` | `eng` | Languages of your scanned documents, joined with `+`, e.g. `eng+spa`. Bundled: `eng ara chi_sim deu fra hin jpn por rus spa`. Each extra language slows OCR |
| `HITL_CONFIDENCE_THRESHOLD` | `0.75` | Answers below this confidence go to engineer review |
| `WEB_CONCURRENCY` / `WORKER_CONCURRENCY` | `2` / `2` | API processes / parallel ingestion tasks |
| `DEMO_MODE` | `false` | Demo instances only. Never enable with real data. |

After editing `.env`, apply the changes with `docker compose up -d`.

## Troubleshooting

- **Certificate not issued:** check that DNS resolves to this server and that port 80 is reachable from the internet, then run `docker compose logs caddy`.
- **"The AI service is misconfigured":** the provider credentials are wrong or missing (or, with Anthropic, out of credits). Run `docker compose run --rm api llm-check`: it prints the underlying reason, such as a missing IAM permission, a model not enabled in that region, or an unreadable key file. Fix `.env`, then `docker compose up -d`.
- **API won't start after changing the provider:** the logs name the missing setting (`docker compose logs api`), e.g. `AWS_REGION must be set when LLM_PROVIDER=bedrock` or a Bedrock model ID without the `anthropic.` prefix.
- **Uploads stuck in PROCESSING:** check `docker compose logs worker`. Scanned PDFs are read with OCR at a few seconds per page, so a long scanned report can take a while; documents that take over 2 hours are marked failed.
- **Scanned PDF indexed with little or garbled text:** set `OCR_LANGUAGES` to the documents' language (e.g. `eng+fra`), then delete and re-upload the document. Very faint or low-resolution scans may not be readable. Citations from OCR'd pages are labelled "Page N (OCR)"; check the original for exact figures.
- **Locked out of an admin account (lost authenticator):** another admin can reset MFA from the admin page. If you have no other admin, run `docker compose run --rm api create-admin recovery@company.com`.

## License

PipelineGPT is free software under the [GNU Affero General Public License v3.0](LICENSE). If you modify it and make it available to users over a network, the AGPL requires you to offer them your modified source. Copyright (C) 2026 Collins Kubu. Commercial licenses (for use outside the AGPL's terms) and support are available from the author.
