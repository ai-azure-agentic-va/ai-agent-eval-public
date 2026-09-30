#!/bin/bash
# Starts the three processes of the single-container eval framework:
#   uvicorn  (FastAPI eval service, 127.0.0.1:8100)
#   streamlit (UI, 127.0.0.1:8601)
#   nginx    (single exposed port 0.0.0.0:8501 routing to both)
# If ANY of them dies, the whole container exits so Docker can restart it.
set -uo pipefail

cd /app

# --- Container-internal wiring (overrides anything from --env-file) ----------
# UI -> service calls stay inside the container:
export EVAL_API_URL="http://127.0.0.1:8100"
# Where /auth/login|logout land when no `next` is given (browser-facing URL):
export STREAMLIT_URL="${STREAMLIT_URL:-http://localhost:8501}"
# Auth links in the UI must be RELATIVE so they ride the one nginx port:
export AUTH_SAME_ORIGIN="true"
# An agent on 127.0.0.1/localhost (registry seed `va`, LANGGRAPH_BASE_URL) lives
# on the HOST, not in this container — re-target such URLs to the host gateway.
export AGENT_LOOPBACK_HOST="${AGENT_LOOPBACK_HOST:-host.docker.internal}"
# Logs: stderr always (docker logs), plus a rotating file under /app/logs
# (mount it to keep logs on the host). LOG_FORMAT=json is handy for log shippers.
export LOG_FILE="${LOG_FILE:-/app/logs/eval-service.log}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export LOG_FORMAT="${LOG_FORMAT:-text}"
export PYTHONUNBUFFERED=1

echo "entrypoint: starting eval service (LOG_LEVEL=$LOG_LEVEL LOG_FORMAT=$LOG_FORMAT LOG_FILE=$LOG_FILE" \
     "LANGSMITH_TRACING=${LANGSMITH_TRACING:-unset} DISABLE_AUTH=${DISABLE_AUTH:-unset})" >&2

python -m uvicorn service.main:app --host 127.0.0.1 --port 8100 --log-level warning &
UVICORN_PID=$!

python -m streamlit run ui/app.py \
    --server.address 127.0.0.1 \
    --server.port 8601 \
    --server.headless true &
STREAMLIT_PID=$!

nginx -c /etc/nginx/nginx.conf -g 'daemon off;' &
NGINX_PID=$!

shutdown() {
    kill -TERM "$UVICORN_PID" "$STREAMLIT_PID" "$NGINX_PID" 2>/dev/null
}
trap shutdown TERM INT

# Block until the FIRST process exits (normally never), then take the
# container down with it so Docker's restart policy can kick in.
wait -n "$UVICORN_PID" "$STREAMLIT_PID" "$NGINX_PID"
CODE=$?
echo "entrypoint: a supervised process exited (status $CODE) — stopping container" >&2
shutdown
wait
exit "$CODE"
