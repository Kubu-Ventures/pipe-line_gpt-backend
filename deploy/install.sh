#!/usr/bin/env bash
# First-time install of PipelineGPT on a Linux host with Docker.
# Idempotent: re-running keeps an existing .env and just (re)starts the stack.
#
# Unattended use: any answer already set as an environment variable is not asked for,
# and without a terminal a missing required answer is an error. The variables are
#   PIPELINEGPT_DOMAIN, ACME_EMAIL, PIPELINEGPT_VERSION (optional),
#   LLM_PROVIDER = anthropic | bedrock | vertex, then for that provider
#     anthropic: ANTHROPIC_API_KEY
#     bedrock:   AWS_REGION, and optionally LLM_MODEL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
#     vertex:    VERTEX_PROJECT_ID, VERTEX_KEY_FILE (path to the key JSON), optionally VERTEX_REGION
#   ADMIN_EMAIL and ADMIN_PASSWORD (12+ characters) for the first administrator.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || fail "Docker is not installed (https://docs.docker.com/engine/install/)."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (docker compose ...)."
command -v openssl >/dev/null || fail "openssl is required to generate secrets."

secret() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-48; }

# ask VAR PROMPT [secret]: keep VAR if already set, otherwise prompt for it (required).
ask() {
  [[ -n "${!1:-}" ]] && return
  [[ -t 0 ]] || fail "$1 is not set, and there is no terminal to ask for it."
  if [[ "${3:-}" == secret ]]; then read -rsp "$2" "$1"; echo; else read -rp "$2" "$1"; fi
  [[ -n "${!1}" ]] || fail "$1 is required."
}
# ask_optional VAR PROMPT [secret]: like ask, but an empty answer (or no terminal) is fine.
ask_optional() {
  [[ -n "${!1:-}" || ! -t 0 ]] && return
  if [[ "${3:-}" == secret ]]; then read -rsp "$2" "$1"; echo; else read -rp "$2" "$1"; fi
}

if [[ ! -f .env ]]; then
  say "Configuring a new installation"
  ask PIPELINEGPT_DOMAIN "Domain users will open (e.g. pipelinegpt.yourcompany.com): "
  ask ACME_EMAIL "Email for Let's Encrypt certificate notices: "
  domain="$PIPELINEGPT_DOMAIN"
  acme="$ACME_EMAIL"
  version="${PIPELINEGPT_VERSION:-$(cat VERSION 2>/dev/null || echo latest)}"

  if [[ -z "${LLM_PROVIDER:-}" ]]; then
    [[ -t 0 ]] || fail "LLM_PROVIDER is not set (anthropic, bedrock or vertex)."
    echo "Where should the AI model run?"
    echo "  1) Anthropic API (simplest; needs an Anthropic API key)"
    echo "  2) AWS Bedrock in your AWS account"
    echo "  3) Google Vertex AI in your Google Cloud project"
    read -rp "Choose 1, 2 or 3 [1]: " choice
    case "${choice:-1}" in
      1) LLM_PROVIDER=anthropic ;;
      2) LLM_PROVIDER=bedrock ;;
      3) LLM_PROVIDER=vertex ;;
      *) fail "Please choose 1, 2 or 3." ;;
    esac
  fi
  llm=()
  case "$LLM_PROVIDER" in
    anthropic)
      ask ANTHROPIC_API_KEY "Anthropic API key (input hidden): " secret
      llm+=(-e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=anthropic|"
            -e "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}|")
      ;;
    bedrock)
      ask AWS_REGION "AWS region with Claude enabled in Bedrock (e.g. us-east-1): "
      ask_optional LLM_MODEL "Bedrock model ID [anthropic.claude-sonnet-5]: "
      [[ -t 0 && -z "${AWS_ACCESS_KEY_ID:-}" ]] && echo "AWS credentials: leave the access key empty to use this server's IAM role."
      ask_optional AWS_ACCESS_KEY_ID "AWS access key ID (optional): "
      if [[ -n "${AWS_ACCESS_KEY_ID:-}" ]]; then
        ask AWS_SECRET_ACCESS_KEY "AWS secret access key (input hidden): " secret
      fi
      llm+=(-e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=bedrock|"
            -e "s|^LLM_MODEL=.*|LLM_MODEL=${LLM_MODEL:-anthropic.claude-sonnet-5}|"
            -e "s|^AWS_REGION=.*|AWS_REGION=${AWS_REGION}|"
            -e "s|^AWS_ACCESS_KEY_ID=.*|AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}|"
            -e "s|^AWS_SECRET_ACCESS_KEY=.*|AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}|")
      ;;
    vertex)
      ask VERTEX_PROJECT_ID "Google Cloud project ID: "
      ask_optional VERTEX_REGION "Vertex region [global]: "
      ask VERTEX_KEY_FILE "Path to the service-account key JSON (role: Vertex AI User): "
      [[ -f "$VERTEX_KEY_FILE" ]] || fail "No file at '$VERTEX_KEY_FILE'."
      # Directory 700 keeps the key private on the host; the file itself must be readable
      # by the container's non-root user.
      mkdir -p -m 700 secrets
      install -m 644 "$VERTEX_KEY_FILE" secrets/gcp-key.json
      llm+=(-e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=vertex|"
            -e "s|^VERTEX_PROJECT_ID=.*|VERTEX_PROJECT_ID=${VERTEX_PROJECT_ID}|"
            -e "s|^VERTEX_REGION=.*|VERTEX_REGION=${VERTEX_REGION:-global}|"
            -e "s|^#   COMPOSE_FILE=|COMPOSE_FILE=|")
      ;;
    *) fail "LLM_PROVIDER must be anthropic, bedrock or vertex (got '$LLM_PROVIDER')." ;;
  esac

  umask 077
  sed \
    -e "s|^PIPELINEGPT_DOMAIN=.*|PIPELINEGPT_DOMAIN=${domain}|" \
    -e "s|^ACME_EMAIL=.*|ACME_EMAIL=${acme}|" \
    -e "s|^PIPELINEGPT_VERSION=.*|PIPELINEGPT_VERSION=${version}|" \
    "${llm[@]}" \
    -e "s|^JWT_SECRET=.*|JWT_SECRET=$(secret)|" \
    -e "s|^NEXTAUTH_SECRET=.*|NEXTAUTH_SECRET=$(secret)|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(secret)|" \
    -e "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(secret)|" \
    .env.example > .env
  say "Wrote .env (permissions 600). Back it up somewhere safe: it holds all secrets."
else
  say "Using existing .env"
fi

say "Pulling images"
docker compose pull

say "Starting the stack (database migrations run automatically)"
docker compose up -d

say "Waiting for the API to become healthy"
for _ in $(seq 1 60); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)" 2>/dev/null || true)
  [[ "$status" == "healthy" ]] && break
  sleep 5
done
[[ "${status:-}" == "healthy" ]] || fail "API did not become healthy. Check: docker compose logs api"

say "Checking the AI provider"
if ! docker compose run --rm -T api llm-check; then
  printf '\033[1;33mwarning:\033[0m %s\n' "The AI provider did not answer (see above). Questions will fail until" \
    "this is fixed: correct .env, run 'docker compose up -d', then 'docker compose run --rm api llm-check'." >&2
fi

if [[ -z "$(docker compose exec -T postgres psql -U pipelinegpt -d pipelinegpt -tAc "SELECT 1 FROM users WHERE role='ADMIN' LIMIT 1")" ]]; then
  say "Create the first administrator"
  ask ADMIN_EMAIL "Admin email: "
  if [[ -n "${ADMIN_PASSWORD:-}" ]]; then
    printf '%s\n' "$ADMIN_PASSWORD" | docker compose run --rm -T api create-admin "$ADMIN_EMAIL" --password-stdin
  else
    [[ -t 0 ]] || fail "ADMIN_PASSWORD is not set, and there is no terminal to ask for it."
    docker compose run --rm api create-admin "$ADMIN_EMAIL"
  fi
fi

domain=$(grep '^PIPELINEGPT_DOMAIN=' .env | cut -d= -f2)
say "Done. Open https://${domain} and sign in; you'll be asked to set up an authenticator app."
