# ops-console — status/usage dashboard for every deployed client instance
# (clinics, law firms, whatever vertical — same underlying product).
#
# Mirrors dental-clinic-agent's own Dockerfile conventions: python:3.12-slim,
# non-root runtime user, HEALTHCHECK against /health, pip layer caching.
#
# One addition specific to this app: openssh-client. The version-check
# (deployed commit, commits behind origin/master, container state) SSHes
# into each monitored client's VPS using YOUR SSH key, mounted read-only at
# runtime (see docker-compose.local.yml) — this container never has its own
# key baked in. If ops-console never runs anywhere but your own machine via
# a plain venv, this is only exercised in the Docker path.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl: container HEALTHCHECK below.
# openssh-client: the version-check shells out to `ssh` (backend/core.py),
# same mechanism deploy.ps1 uses from Windows — this is its Linux equivalent.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Non-root runtime user. /data holds clients.json (the only persistent
# state this app has — no database). SSH needs a real home directory to
# find ~/.ssh/known_hosts and to keep key-file permission checks happy —
# see entrypoint.sh for why the mounted key gets copied rather than used
# directly from its bind-mounted path.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

USER appuser

ENV CLIENTS_CONFIG=/data/clients.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
