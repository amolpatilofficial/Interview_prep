# Build from the job-agent/ directory:
#   docker build -f deploy/Dockerfile -t job-agent .
FROM python:3.12-slim

# Browsers land here rather than in ~/.cache so the non-root user can read them.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/job-agent

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first so edits to the app don't invalidate the browser layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && chmod -R a+rx /ms-playwright

COPY app ./app
COPY config/profile.example.yaml ./config/profile.example.yaml
COPY scripts ./scripts
COPY cli.py .
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Chromium's own sandbox needs privileges we don't grant, so we disable it and
# rely on the container boundary plus a non-root user instead.
ENV JAA_NO_SANDBOX=true \
    JAA_HEADLESS=true \
    JAA_DB_PATH=/data/job_agent.duckdb \
    JAA_SCREENSHOT_DIR=/data/screenshots \
    JAA_STORAGE_STATE=/data/storage_state.json \
    JAA_PROFILE_PATH=/data/profile.yaml

RUN useradd --create-home --uid 10001 agent \
 && mkdir -p /data/screenshots /data/documents \
 && chown -R agent:agent /data /srv/job-agent
USER agent

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
