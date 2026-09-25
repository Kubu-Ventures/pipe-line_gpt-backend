#!/usr/bin/env bash
# Back up the PipelineGPT database (documents, embeddings, users, audit trail).
# Usage: ./backup.sh [directory]   (default ./backups; keeps the newest $KEEP, default 14)
# Schedule it, e.g. cron: 30 2 * * * /opt/pipelinegpt/backup.sh >> /var/log/pipelinegpt-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

dir="${1:-./backups}"
keep="${KEEP:-14}"
mkdir -p "$dir"
file="$dir/pipelinegpt-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose exec -T postgres pg_dump -U pipelinegpt -d pipelinegpt --format=custom > "$file.partial"
mv "$file.partial" "$file"
chmod 600 "$file"
echo "Backup written: $file ($(du -h "$file" | cut -f1))"

# Prune: keep the newest $keep dumps.
find "$dir" -maxdepth 1 -name 'pipelinegpt-*.dump' -printf '%T@ %p\n' \
  | sort -rn | tail -n +"$((keep + 1))" | cut -d' ' -f2- | xargs -r rm --
