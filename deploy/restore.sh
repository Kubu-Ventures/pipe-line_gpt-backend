#!/usr/bin/env bash
# Restore a backup made by backup.sh. REPLACES the current database.
# Usage: ./restore.sh backups/pipelinegpt-YYYYMMDDTHHMMSSZ.dump
set -euo pipefail
cd "$(dirname "$0")"

file="${1:?usage: ./restore.sh <backup.dump>}"
[[ -f "$file" ]] || { echo "No such file: $file" >&2; exit 1; }

read -rp "This replaces ALL current data with $file. Type 'restore' to continue: " answer
[[ "$answer" == "restore" ]] || { echo "Aborted."; exit 1; }

docker compose stop api worker
docker compose exec -T postgres pg_restore -U pipelinegpt -d pipelinegpt --clean --if-exists --no-owner < "$file"
docker compose exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning FLUSHDB' >/dev/null
docker compose up -d
echo "Restored $file. Cached answers were cleared."
