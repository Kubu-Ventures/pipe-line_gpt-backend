# Hosted pilots

`provision.sh` sets up a PipelineGPT pilot on a VM you host, in one command. Each pilot gets its own VM, database and domain, so customers never share data.

This folder is for running hosted pilots. It isn't part of the customer deploy bundle; customers who host PipelineGPT themselves use `deploy/install.sh`.

## Before you run it

1. **Create a VM:** Ubuntu 22.04/24.04 or Debian 12, at least 4 vCPU, 8 GB RAM and 40 GB disk (see `deploy/README.md` for sizing). Open ports 22, 80 and 443.
2. **Point DNS at it:** an A record for the pilot's domain, e.g. `acme.pilots.example.com`, with the VM's IP. HTTPS certificates are issued on first start, so this must be in place first.
3. **SSH access with a key**, as root or as a user with passwordless sudo (`ssh-copy-id root@<ip>`). The script accepts a new VM's host key on first connection; if you rebuild a VM at the same address, clear the old key with `ssh-keygen -R <ip>` first.
4. **AI provider credentials in your environment** (never on the command line):

   | Provider | Set |
   |---|---|
   | Anthropic API (default) | `ANTHROPIC_API_KEY` |
   | AWS Bedrock | `LLM_PROVIDER=bedrock`, `AWS_REGION`, optionally `LLM_MODEL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
   | Google Vertex AI | `LLM_PROVIDER=vertex`, `VERTEX_PROJECT_ID`, `VERTEX_KEY_FILE`, optionally `VERTEX_REGION` |

5. **A release that supports unattended install** (the first release after 0.2.0). Run `git fetch --tags` so the release tag is in your checkout.

## Provision

```bash
export ANTHROPIC_API_KEY=...        # or the Bedrock / Vertex variables
ops/pilots/provision.sh \
  --name acme \
  --host root@203.0.113.10 \
  --domain acme.pilots.example.com \
  --admin-email you@example.com \
  --docs ~/pilots/acme/documents
```

It will:

1. check the settings, the release and the DNS record before touching the server;
2. install Docker if it's missing and open ports 80/443 if `ufw` is active;
3. upload the release's `deploy/` bundle to `/opt/pipelinegpt`;
4. run `install.sh` unattended: secrets are generated on the server, the stack starts, the AI provider is checked, and the first admin is created with a random password;
5. schedule a daily database backup (02:30 server time, newest 14 kept);
6. upload `--docs` and queue them with `bulk-import` (subfolders included, duplicates skipped);
7. save records in `~/.pipelinegpt-pilots/<name>/` and check `https://<domain>/backend/health`.

Secrets never appear in a command line on either machine: the installer's answers travel over SSH on stdin into a file only root can read, and it is deleted as soon as the installer finishes.

`provision.sh --help` lists every option.

## After provisioning

- Sign in as the admin and set up the authenticator app (required for admins).
- Invite the customer's team from **Admin → Invite**. Keep the admin account for yourself so you can support the pilot.
- Documents are processed in the background. The Documents page shows progress, and scanned PDFs take longer.

## Records

`~/.pipelinegpt-pilots/<name>/` holds, readable only by you:

| File | Contents |
|---|---|
| `admin-password` | The first admin's password (only when this run created the admin) |
| `server.env` | A copy of the server's `.env`: every secret of the pilot. Needed to restore it elsewhere |
| `pilot.env` | Host, domain, version, provider, admin email, date |
| `provision-*.log` | Output of each run |

Back this folder up somewhere encrypted. Never commit it.

## Re-running, loading more documents, upgrading

- **Re-running** the same command is safe. The server keeps its `.env`, data and admin, the bundle is refreshed, and the stack restarts. Add `--docs` to load more documents; files already loaded are skipped.
- **Upgrading** a pilot to a new release: `ssh <host> 'cd /opt/pipelinegpt && sudo ./upgrade.sh 0.4.0'`. This backs up the database first.
- **Backups** stay on the VM (`/opt/pipelinegpt/backups`). Copy them off the server regularly, for example with `rsync` from your machine.

## Testing unreleased changes

`--from-worktree` deploys this checkout's `deploy/` folder instead of the release's. Container images still come from `--version`, so use it only when the images for that version exist.
