#!/usr/bin/env bash
# Деплой на modelport-prod: ./deploy.sh
# Код уходит tar'ом, .env (мнемоника, API_TOKEN) — отдельно с правами 600.
set -euo pipefail
HOST=${HOST:-modelport-prod}
DIR=funpay-stars
DOMAIN=stars.modelport.org
PORT=38110

cd "$(dirname "$0")"
test -f .env || { echo "нет .env"; exit 1; }

COPYFILE_DISABLE=1 tar -cz --no-xattrs \
  --exclude .venv --exclude .git --exclude .env --exclude '*.sqlite3*' \
  --exclude data --exclude dashboard.env --exclude .claude --exclude __pycache__ \
  . | ssh "$HOST" "mkdir -p $DIR && cd $DIR && rm -rf src && tar -xzf - --warning=no-unknown-keyword"

# секреты: без локальных путей (DB_PATH задаёт Dockerfile)
grep -vE '^(DB_PATH)=' .env | ssh "$HOST" "cd $DIR && umask 077 && cat > .env && chmod 600 .env && rm -f dashboard.env && mkdir -p data"

ssh "$HOST" "cd $DIR && docker compose up -d --build --remove-orphans && docker compose ps"

# маршрут живёт в /etc/caddy/Caddyfile (stars.modelport.org -> 127.0.0.1:$PORT)
sleep 3
curl -sf -o /dev/null "https://$DOMAIN/healthz" && echo "OK: https://$DOMAIN" || echo "ВНИМАНИЕ: https://$DOMAIN не отвечает"
