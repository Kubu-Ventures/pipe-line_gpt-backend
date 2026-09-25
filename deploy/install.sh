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
  read -rsp "Anthropic API key (input hidden): " anthropic; echo
  [[ -n "$anthropic" ]] || fail "An Anthropic API key is required."
  version="${PIPELINEGPT_VERSION:-$(cat VERSION 2>/dev/null || echo latest)}"

  umask 077
  sed \
    -e "s|^PIPELINEGPT_DOMAIN=.*|PIPELINEGPT_DOMAIN=${domain}|" \
    -e "s|^ACME_EMAIL=.*|ACME_EMAIL=${acme}|" \
    -e "s|^PIPELINEGPT_VERSION=.*|PIPELINEGPT_VERSION=${version}|" \
    -e "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${anthropic}|" \
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

if [[ -z "$(docker compose exec -T postgres psql -U pipelinegpt -d pipelinegpt -tAc "SELECT 1 FROM users WHERE role='ADMIN' LIMIT 1")" ]]; then
  say "Create the first administrator"
  read -rp "Admin email: " admin_email
  docker compose run --rm api create-admin "$admin_email"
fi

domain=$(grep '^PIPELINEGPT_DOMAIN=' .env | cut -d= -f2)
say "Done. Open https://${domain} and sign in; you'll be asked to set up an authenticator app."
