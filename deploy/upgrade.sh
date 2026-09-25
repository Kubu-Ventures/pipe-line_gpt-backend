#!/usr/bin/env bash
# Upgrade to a new release: backs up the database, switches the version, restarts.
# Usage: ./upgrade.sh 0.3.0
set -euo pipefail
cd "$(dirname "$0")"

new="${1:?usage: ./upgrade.sh <version>}"
current=$(grep '^PIPELINEGPT_VERSION=' .env | cut -d= -f2)
echo "Upgrading ${current} -> ${new}. Read the release notes first:"
echo "  https://github.com/Kubu-Ventures/pipe-line_gpt-backend/releases/tag/v${new}"

./backup.sh
sed -i.bak "s|^PIPELINEGPT_VERSION=.*|PIPELINEGPT_VERSION=${new}|" .env && rm -f .env.bak

PIPELINEGPT_VERSION="$new" docker compose pull
docker compose up -d   # the api container applies database migrations on start
echo "Now running ${new}. To roll back: restore the backup above and run ./upgrade.sh ${current}"
