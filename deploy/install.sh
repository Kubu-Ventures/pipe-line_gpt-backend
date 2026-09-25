#!/usr/bin/env bash
# First-time install of PipelineGPT on a Linux host with Docker.
# Idempotent: re-running keeps an existing .env and just (re)starts the stack.
set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || fail "Docker is not installed (https://docs.docker.com/engine/install/)."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (docker compose ...)."
command -v openssl >/dev/null || fail "openssl is required to generate secrets."

secret() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-48; }

if [[ ! -f .env ]]; then
  say "Configuring a new installation"
  read -rp "Domain users will open (e.g. pipelinegpt.yourcompany.com): " domain
  [[ -n "$domain" ]] || fail "A domain is required."
  read -rp "Email for Let's Encrypt certificate notices: " acme
  [[ -n "$acme" ]] || fail "An email is required."
  version="${PIPELINEGPT_VERSION:-$(cat VERSION 2>/dev/null || echo latest)}"

  echo "Where should the AI model run?"
  echo "  1) Anthropic API (simplest; needs an Anthropic API key)"
  echo "  2) AWS Bedrock in your AWS account"
  echo "  3) Google Vertex AI in your Google Cloud project"
  read -rp "Choose 1, 2 or 3 [1]: " choice
  llm=()
  case "${choice:-1}" in
    1)
      read -rsp "Anthropic API key (input hidden): " anthropic; echo
      [[ -n "$anthropic" ]] || fail "An Anthropic API key is required."
      llm+=(-e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=anthropic|"
            -e "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${anthropic}|")
      ;;
    2)
      read -rp "AWS region with Claude enabled in Bedrock (e.g. us-east-1): " region
      [[ -n "$region" ]] || fail "An AWS region is required."
      read -rp "Bedrock model ID [anthropic.claude-sonnet-5]: " model
      echo "AWS credentials: leave the access key empty to use this server's IAM role."
      read -rp "AWS access key ID (optional): " aws_key
      aws_secret=""
      if [[ -n "$aws_key" ]]; then
        read -rsp "AWS secret access key (input hidden): " aws_secret; echo
        [[ -n "$aws_secret" ]] || fail "The secret access key is required with an access key ID."
      fi
      llm+=(-e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=bedrock|"
            -e "s|^LLM_MODEL=.*|LLM_MODEL=${model:-anthropic.claude-sonnet-5}|"
            -e "s|^AWS_REGION=.*|AWS_REGION=${region}|"
            -e "s|^AWS_ACCESS_KEY_ID=.*|AWS_ACCESS_KEY_ID=${aws_key}|"
            -e "s|^AWS_SECRET_ACCESS_KEY=.*|AWS_SECRET_ACCESS_KEY=${aws_secret}|")
      ;;
    3)
      read -rp "Google Cloud project ID: " project
      [[ -n "$project" ]] || fail "A project ID is required."
      read -rp "Vertex region [global]: " vregion
      read -rp "Path to the service-account key JSON (role: Vertex AI User): " keyfile
      [[ -f "$keyfile" ]] || fail "No file at '$keyfile'."
      # Directory 700 keeps the key private on the host; the file itself must be readable
      # by the container's non-root user.
      mkdir -p -m 700 secrets
      install -m 644 "$keyfile" secrets/gcp-key.json
      llm+=(-e "s|^LLM_PROVIDER=.*|LLM_PROVIDER=vertex|"
            -e "s|^VERTEX_PROJECT_ID=.*|VERTEX_PROJECT_ID=${project}|"
            -e "s|^VERTEX_REGION=.*|VERTEX_REGION=${vregion:-global}|"
            -e "s|^#   COMPOSE_FILE=|COMPOSE_FILE=|")
      ;;
    *) fail "Please choose 1, 2 or 3." ;;
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
  read -rp "Admin email: " admin_email
  docker compose run --rm api create-admin "$admin_email"
fi

domain=$(grep '^PIPELINEGPT_DOMAIN=' .env | cut -d= -f2)
say "Done. Open https://${domain} and sign in; you'll be asked to set up an authenticator app."
