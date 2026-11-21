#!/usr/bin/env bash
# Деплой дашборда на modelport-prod: ./deploy.sh
set -euo pipefail
HOST=${HOST:-modelport-prod}
DIR=funpay-stars
DOMAIN=stars.modelport.org
PORT=38110

cd "$(dirname "$0")"
COPYFILE_DISABLE=1 tar -cz --no-xattrs \
  --exclude .venv --exclude .git --exclude .env --exclude '*.sqlite3' \
  --exclude data --exclude dashboard.env --exclude .claude --exclude __pycache__ \
  . | ssh "$HOST" "mkdir -p $DIR && cd $DIR && rm -rf src && tar -xzf - --warning=no-unknown-keyword"

# публичный адрес берём из локального .env (мнемоника на сервер не уходит)
ADDR=$(uv run python -c "from funpay_stars.config import load_settings as l;from funpay_stars.wallet import TonWallet as W;print(W(l()).address)")
TCK=$(grep -E '^TONCENTER_API_KEY=' .env | cut -d= -f2- || true)

ssh "$HOST" "cd $DIR && mkdir -p data && umask 077 && printf 'WALLET_ADDRESS=%s\nTONCENTER_API_KEY=%s\n' '$ADDR' '$TCK' > dashboard.env && docker compose up -d --build && docker compose ps"

# маршрут в Caddy через admin API (если ещё нет)
ssh "$HOST" "curl -s localhost:2019/config/apps/http/servers/srv0/routes | grep -q '$DOMAIN' || curl -sf -X POST -H 'Content-Type: application/json' localhost:2019/config/apps/http/servers/srv0/routes -d '{\"match\":[{\"host\":[\"$DOMAIN\"]}],\"handle\":[{\"handler\":\"subroute\",\"routes\":[{\"handle\":[{\"handler\":\"reverse_proxy\",\"upstreams\":[{\"dial\":\"127.0.0.1:$PORT\"}]}]}]}],\"terminal\":true}' && echo caddy-route-ok"
