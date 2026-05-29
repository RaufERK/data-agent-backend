#!/bin/bash
# Start gpt2giga vision proxy (GigaChat-2-Max) on localhost:8090
# Credentials are read from .env.gpt2giga in the project root

set -e
cd "$(dirname "$0")/.."

ENV_FILE=".env.gpt2giga"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE not found"
    exit 1
fi

if lsof -Pi :8090 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Port 8090 is already in use — gpt2giga may already be running"
    exit 0
fi

set -a
source "$ENV_FILE"
set +a

LOG_FILE="/tmp/gpt2giga.log"
echo "Starting gpt2giga proxy -> GigaChat-2-Max on :8090, log: $LOG_FILE"

PYTHONHTTPSVERIFY=0 \
CURL_CA_BUNDLE="" \
REQUESTS_CA_BUNDLE="" \
SSL_CERT_FILE="" \
PYTHONWARNINGS="ignore:Unverified HTTPS request" \
.venv/bin/gpt2giga \
    --env-path "$ENV_FILE" \
    --proxy.host 0.0.0.0 \
    --proxy.port 8090 \
    --proxy.log-level INFO \
    --proxy.pass-model true \
    --proxy.enable-images true \
    --gigachat.verify-ssl-certs false \
    >> "$LOG_FILE" 2>&1 &

GPT2GIGA_PID=$!
echo $GPT2GIGA_PID > /tmp/gpt2giga.pid
echo "Started PID=$GPT2GIGA_PID"

# Wait for it to become ready
for i in $(seq 1 15); do
    sleep 1
    if curl -s --max-time 2 http://localhost:8090/v1/models >/dev/null 2>&1; then
        echo "gpt2giga is ready"
        exit 0
    fi
done

echo "WARNING: gpt2giga did not respond within 15s, check $LOG_FILE"
exit 1
