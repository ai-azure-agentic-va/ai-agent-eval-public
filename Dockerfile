# Single-image build: FastAPI eval service + Streamlit UI + nginx front door.
#
#   docker build -t eval-streamlit-framework .
#   docker run -d --add-host=host.docker.internal:host-gateway --env-file .env \
#       -p 8501:8501 eval-streamlit-framework
#   -> http://localhost:8501            (nginx: /auth/* and /api/* -> service,
#                                        everything else -> Streamlit)
#
# python:3.12-slim, not 3.13+ — some pinned deps lack newer wheels.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

# Dependencies first so code-only changes reuse the cached layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# .dockerignore keeps .venv/.git/__pycache__/.env/logs out of the image.
COPY --chown=app:app . .

RUN cp docker/nginx.conf /etc/nginx/nginx.conf \
    && chmod +x docker/entrypoint.sh \
    && mkdir -p /app/logs /tmp/nginx \
    && chown -R app:app /app/logs /tmp/nginx /var/lib/nginx /var/log/nginx

USER app
RUN nginx -t

# Service logs (LOG_FILE defaults to /app/logs/eval-service.log in the entrypoint);
# mount it to keep logs on the host (-v "$PWD/logs:/app/logs").
VOLUME ["/app/logs"]

EXPOSE 8501

# Healthy = nginx is routing AND the FastAPI service answers /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/api/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
