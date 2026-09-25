#!/bin/sh
# Role dispatcher for the PipelineGPT backend image.
set -eu

role="${1:-api}"
[ $# -gt 0 ] && shift

case "$role" in
  api)
    # Migrations are safe to run on every start (Alembic is idempotent). Set
    # RUN_MIGRATIONS=false when several API replicas start at once and run `migrate` separately.
    if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
      alembic -c alembic/alembic.ini upgrade head
    fi
    # --proxy-headers: trust X-Forwarded-For from the reverse proxy so login lockout and
    # audit logs see real client IPs. Only safe because the API port is not published.
    exec uvicorn app.main:app \
      --host 0.0.0.0 --port 8000 \
      --workers "${WEB_CONCURRENCY:-2}" \
      --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
      --timeout-keep-alive 75 \
      "$@"
    ;;
  worker)
    exec celery -A app.tasks.celery_app.celery_app worker \
      --loglevel "${LOG_LEVEL:-info}" --concurrency "${WORKER_CONCURRENCY:-2}" "$@"
    ;;
  flower)
    exec celery -A app.tasks.celery_app.celery_app flower --port=5555 "$@"
    ;;
  migrate)
    exec alembic -c alembic/alembic.ini upgrade head
    ;;
  create-admin)
    exec python create_admin.py "$@"
    ;;
  *)
    exec "$role" "$@"
    ;;
esac
