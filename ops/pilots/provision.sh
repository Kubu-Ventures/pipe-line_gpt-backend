#!/usr/bin/env bash
# Provision a hosted PipelineGPT pilot on a fresh Linux VM, in one command.
#
#   ops/pilots/provision.sh --name acme --host root@203.0.113.10 \
#       --domain acme.pilots.example.com --admin-email you@example.com \
#       [--docs ./acme-docs] [--version 0.3.0] [--acme-email you@example.com]
#
# Before running: create the VM (Ubuntu 22.04/24.04 or Debian 12; 4 vCPU, 8 GB RAM,
# 40 GB disk at least), point the domain's DNS A record at it, and make sure you can
# ssh in with a key as root or as a user with passwordless sudo.
#
# AI provider settings are read from your environment, never the command line:
#   LLM_PROVIDER=anthropic (default)  ANTHROPIC_API_KEY
#   LLM_PROVIDER=bedrock              AWS_REGION [LLM_MODEL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY]
#   LLM_PROVIDER=vertex               VERTEX_PROJECT_ID VERTEX_KEY_FILE [VERTEX_REGION]
#
# Options:
#   --name NAME          short pilot id (lowercase letters, digits, dashes)
#   --host [USER@]HOST   the VM to provision
#   --domain DOMAIN      hostname users will open; DNS must already point at the VM
#   --admin-email EMAIL  first administrator (use your own address, then invite the
#                        customer's team from Admin -> Invite)
#   --acme-email EMAIL   Let's Encrypt notices (default: the admin email)
#   --docs FOLDER        documents to load into the pilot (subfolders included)
#   --version X.Y.Z      release to deploy (default: the newest vX.Y.Z git tag)
#   --from-worktree      deploy this checkout's deploy/ folder instead of the release's
#                        (for testing unreleased changes; images still come from --version)
#   --remote-dir DIR     install location on the VM (default: /opt/pipelinegpt)
#   --skip-dns-check     don't check that the domain resolves to the VM
#   --no-backup-cron     don't schedule the daily database backup
#
# Re-running is safe: an existing installation keeps its .env, data and admin, and is
# restarted on the requested version's bundle. Records (admin password, a copy of the
# server's .env, logs) are kept in ~/.pipelinegpt-pilots/NAME/ (override with
# PILOTS_DIR), readable only by you. Keep them safe: they hold every secret of the pilot.
set -euo pipefail
umask 077

repo=$(cd "$(dirname "$0")/../.." && pwd)

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit "${1:-0}"; }

name="" host="" domain="" admin_email="" acme_email="" docs="" version=""
remote_dir=/opt/pipelinegpt from_worktree=false dns_check=true backup_cron=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) name="${2:?}"; shift 2 ;;
    --host) host="${2:?}"; shift 2 ;;
    --domain) domain="${2:?}"; shift 2 ;;
    --admin-email) admin_email="${2:?}"; shift 2 ;;
    --acme-email) acme_email="${2:?}"; shift 2 ;;
    --docs) docs="${2:?}"; shift 2 ;;
    --version) version="${2:?}"; shift 2 ;;
    --remote-dir) remote_dir="${2:?}"; shift 2 ;;
    --from-worktree) from_worktree=true; shift ;;
    --skip-dns-check) dns_check=false; shift ;;
    --no-backup-cron) backup_cron=false; shift ;;
    -h|--help) usage ;;
    *) printf 'Unknown option: %s\n\n' "$1" >&2; usage 1 ;;
  esac
done

# ------------------------------------------------------------------ checks (nothing touched yet)
[[ -n "$name" && -n "$host" && -n "$domain" && -n "$admin_email" ]] \
  || { printf 'Missing --name, --host, --domain or --admin-email.\n\n' >&2; usage 1; }
[[ "$name" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || fail "--name must be lowercase letters, digits and dashes."
[[ "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || fail "--domain '$domain' is not a hostname."
[[ "$remote_dir" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "--remote-dir must be an absolute path without spaces."
[[ -z "$docs" || -d "$docs" ]] || fail "--docs '$docs' is not a folder."
acme_email="${acme_email:-$admin_email}"
for tool in ssh tar openssl git; do
  command -v "$tool" >/dev/null || fail "$tool is required on this machine."
done

LLM_PROVIDER="${LLM_PROVIDER:-anthropic}"
case "$LLM_PROVIDER" in
  anthropic) llm_vars=(ANTHROPIC_API_KEY); required=(ANTHROPIC_API_KEY) ;;
  bedrock)   llm_vars=(AWS_REGION LLM_MODEL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY); required=(AWS_REGION) ;;
  vertex)    llm_vars=(VERTEX_PROJECT_ID VERTEX_REGION); required=(VERTEX_PROJECT_ID VERTEX_KEY_FILE) ;;
  *) fail "LLM_PROVIDER must be anthropic, bedrock or vertex (got '$LLM_PROVIDER')." ;;
esac
for var in "${required[@]}"; do
  [[ -n "${!var:-}" ]] || fail "$var must be set in your environment for LLM_PROVIDER=$LLM_PROVIDER."
done
if [[ "$LLM_PROVIDER" == vertex ]]; then
  [[ -f "$VERTEX_KEY_FILE" ]] || fail "VERTEX_KEY_FILE: no file at '$VERTEX_KEY_FILE'."
fi

if [[ -z "$version" ]]; then
  version=$(git -C "$repo" tag --list 'v[0-9]*' --sort=-v:refname | head -n1)
  version="${version#v}"
  [[ -n "$version" ]] || fail "No release tag found; pass --version."
fi
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]] || fail "--version '$version' is not X.Y.Z."

# The bundle that goes to the server: the release's deploy/ folder, or this checkout's.
if $from_worktree; then
  bundle() { tar -C "$repo" --exclude=deploy/.env --exclude=deploy/secrets --exclude=deploy/backups -cf - deploy; }
  installer=$(cat "$repo/deploy/install.sh")
else
  git -C "$repo" rev-parse -q --verify "refs/tags/v$version" >/dev/null \
    || fail "No git tag v$version in this checkout. Run 'git fetch --tags', or pass --from-worktree."
  bundle() { git -C "$repo" archive --format=tar "v$version" deploy; }
  installer=$(git -C "$repo" show "v$version:deploy/install.sh")
fi
grep -q ADMIN_PASSWORD <<<"$installer" \
  || fail "The v$version installer can't run unattended (added after 0.2.0). Deploy a newer release."

if $dns_check; then
  # getent exits non-zero for unknown names; that's an empty answer here, not an error.
  addr() { { getent ahostsv4 "$1" 2>/dev/null || true; } | awk 'NR==1 {print $1}'; }
  vm_ip=$(addr "${host#*@}")
  domain_ip=$(addr "$domain")
  [[ -n "$domain_ip" ]] || fail "$domain doesn't resolve yet. Create its DNS A record first (or --skip-dns-check)."
  [[ -z "$vm_ip" || "$vm_ip" == "$domain_ip" ]] \
    || fail "$domain points at $domain_ip, but ${host#*@} is $vm_ip. HTTPS certificates need the domain to point at the VM."
fi

records="${PILOTS_DIR:-$HOME/.pipelinegpt-pilots}/$name"
mkdir -p "$records"
log="$records/provision-$(date -u +%Y%m%dT%H%M%SZ).log"

# ------------------------------------------------------------------ server access
# accept-new: a fresh VM's host key is unknown, and BatchMode would otherwise refuse it
# instead of asking. A key that *changed* since it was recorded is still refused.
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
# Remote command lines are built here on purpose; every value in them goes through q().
# shellcheck disable=SC2029
remote() { ssh "${ssh_opts[@]}" "$host" "$@"; }

say "Connecting to $host"
if ! ssh_error=$(remote true 2>&1); then
  fail "Can't ssh to $host without a prompt: ${ssh_error:-no error message}
  Check the address, that your key is authorized (ssh-copy-id), and, if the VM was
  rebuilt, remove its old host key with: ssh-keygen -R ${host#*@}"
fi
if [[ "$(remote id -u)" == 0 ]]; then
  sudo=""
else
  remote sudo -n true 2>/dev/null || fail "$host: the ssh user needs passwordless sudo (or connect as root)."
  sudo="sudo -n"
fi
# rbash: run the bash script given on stdin on the server, as root.
rbash() { remote "$sudo bash -s"; }
q() { printf '%q' "$1"; }

# The installer's own trap deletes the answer and key files, but only once it has started.
# This covers a run that stops before that (failed upload, lost connection, Ctrl-C).
remove_answer_files() {
  remote "$sudo rm -f $(q "$remote_dir/.provision.env") $(q "$remote_dir/.provision-gcp-key.json")" \
    2>/dev/null || warn "Couldn't remove $remote_dir/.provision.env from $host; delete it by hand."
}
trap remove_answer_files EXIT

say "Preparing the server (Docker, firewall)"
rbash <<EOF 2>&1 | tee -a "$log"
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Installing Docker Engine"
  command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl ca-certificates; }
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
command -v openssl >/dev/null || { apt-get update -qq && apt-get install -y -qq openssl; }
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null
fi
mkdir -p $(q "$remote_dir")
EOF

say "Uploading the v$version deployment bundle"
# .env, secrets/ and backups/ are never in the bundle, so an existing install keeps them.
bundle | remote "$sudo tar -x --no-same-owner --strip-components=1 -C $(q "$remote_dir")"
remote "$sudo sh -c $(q "echo $version > $(q "$remote_dir")/VERSION")"

# ------------------------------------------------------------------ install
admin_password=$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-24)
if [[ "$LLM_PROVIDER" == vertex ]]; then
  remote "$sudo sh -c $(q "umask 077; cat > $(q "$remote_dir")/.provision-gcp-key.json")" < "$VERTEX_KEY_FILE"
fi
# The answers travel on stdin into a 600 file that is deleted when the installer exits (or
# by remove_answer_files if this script stops first). No secret appears in a command line
# here or in install.sh, which writes .env with bash built-ins.
{
  for var in PIPELINEGPT_DOMAIN ACME_EMAIL PIPELINEGPT_VERSION LLM_PROVIDER ADMIN_EMAIL ADMIN_PASSWORD; do
    case "$var" in
      PIPELINEGPT_DOMAIN) val="$domain" ;;
      ACME_EMAIL) val="$acme_email" ;;
      PIPELINEGPT_VERSION) val="$version" ;;
      LLM_PROVIDER) val="$LLM_PROVIDER" ;;
      ADMIN_EMAIL) val="$admin_email" ;;
      ADMIN_PASSWORD) val="$admin_password" ;;
    esac
    printf '%s=%q\n' "$var" "$val"
  done
  for var in "${llm_vars[@]}"; do
    [[ -n "${!var:-}" ]] && printf '%s=%q\n' "$var" "${!var}"
  done
  [[ "$LLM_PROVIDER" == vertex ]] && printf 'VERTEX_KEY_FILE=%q\n' "$remote_dir/.provision-gcp-key.json"
  true
} | remote "$sudo sh -c $(q "umask 077; cat > $(q "$remote_dir")/.provision.env")"

say "Installing (pulls images, starts the stack, creates the admin)"
if ! rbash <<EOF 2>&1 | tee -a "$log"; then
set -euo pipefail
cd $(q "$remote_dir")
trap 'rm -f .provision.env .provision-gcp-key.json' EXIT
set -a; . ./.provision.env; set +a
./install.sh </dev/null
EOF
  fail "The installer failed; see above or $log. Fix the cause and re-run the same command."
fi

if $backup_cron; then
  say "Scheduling a daily database backup (02:30 server time, keeps 14)"
  rbash <<EOF
printf '%s\n' '30 2 * * * root $remote_dir/backup.sh >> /var/log/pipelinegpt-backup.log 2>&1' > /etc/cron.d/pipelinegpt-backup
chmod 644 /etc/cron.d/pipelinegpt-backup
EOF
fi

# ------------------------------------------------------------------ documents
if [[ -n "$docs" ]]; then
  say "Uploading documents from $docs"
  tar -C "$docs" -cf - . | remote "$sudo sh -c $(q "rm -rf $(q "$remote_dir/import") && mkdir -p $(q "$remote_dir/import") && tar -x --no-same-owner -C $(q "$remote_dir/import") && chmod -R a+rX $(q "$remote_dir/import")")"
  say "Queueing them for ingestion"
  if ! rbash <<EOF 2>&1 | tee -a "$log"; then
set -euo pipefail
cd $(q "$remote_dir")
docker compose run --rm -T -v $(q "$remote_dir/import"):/import:ro api bulk-import /import --operator $(q "$admin_email") </dev/null
rm -rf import
EOF
    warn "Document import failed (see $log). The pilot is running; re-run with --docs to retry."
  fi
fi

# ------------------------------------------------------------------ records
remote "$sudo cat $(q "$remote_dir/.env")" > "$records/server.env"
admin_created=false
grep -q "Admin account created: $admin_email" "$log" && admin_created=true
if $admin_created; then
  printf '%s\n' "$admin_password" > "$records/admin-password"
fi
cat > "$records/pilot.env" <<EOF
NAME=$name
HOST=$host
DOMAIN=$domain
REMOTE_DIR=$remote_dir
VERSION=$version
LLM_PROVIDER=$LLM_PROVIDER
ADMIN_EMAIL=$admin_email
PROVISIONED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

say "Checking https://$domain/backend/health"
healthy=false
if command -v curl >/dev/null; then
  for _ in $(seq 1 12); do
    code=0
    curl -fsS -o /dev/null --max-time 10 "https://$domain/backend/health" || code=$?
    [[ $code == 0 ]] && { healthy=true; break; }
    [[ $code == 6 ]] && break   # the name doesn't resolve from here: retrying won't help
    sleep 5                     # the certificate can take a minute on first start
  done
fi
$healthy || warn "The health check didn't pass from here yet. Open https://$domain in a minute; if it still fails, see 'Troubleshooting' in deploy/README.md."

cat <<EOF

$(say "Pilot '$name' is ready")
  URL:          https://$domain
  Admin:        $admin_email
EOF
if $admin_created; then
  echo "  Password:     $admin_password   (also in $records/admin-password)"
  echo "  First sign-in asks the admin to set up an authenticator app."
else
  echo "  Password:     unchanged (the admin already existed)"
fi
cat <<EOF
  Records:      $records   (server.env holds every secret: keep it safe)
  Backups:      $($backup_cron && echo "daily on the server in $remote_dir/backups; copy them off the server regularly" || echo "not scheduled (--no-backup-cron)")
$([[ -n "$docs" ]] && echo "  Documents:    queued; the Documents page shows progress (scanned PDFs take longer)")

Next: sign in, then invite the customer's team from Admin -> Invite.
EOF
