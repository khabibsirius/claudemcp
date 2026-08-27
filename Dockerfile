# Qlik AI as a Linux container.
#
# The Windows service in deploy/install-service.ps1 is the primary path for a
# Windows Server host - it is one fewer moving part between this and the Qlik
# engine, and certificate and DNS problems are far easier to diagnose without
# a container boundary in the way. This exists for hosts that already run
# Docker and would rather have one more container than one more service.
#
#   docker compose up -d
#
# Nothing about the application is OS-specific; it is the deployment around
# it that differs. See DEPLOYMENT.md.

FROM python:3.12-slim AS base

# Bytecode written at build time rather than on every start, and unbuffered
# output so `docker logs` shows the startup banner as it happens rather than
# when the buffer fills.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first, so a change to the source does not reinstall the world.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# A user of its own. The app writes to /data and reads /certs and nothing
# else, so it has no business running as root - and a container that does is
# one container escape away from being root on the host.
RUN useradd --create-home --uid 10001 qlikai \
    && mkdir -p /data/history /var/log/qlikai \
    && chown -R qlikai:qlikai /app /data /var/log/qlikai
USER qlikai

# Where state lives. Both are volumes in compose: they are the only things
# here that cannot be rebuilt from the image.
ENV USERS_DB=/data/users.db \
    HISTORY_DIR=/data/history \
    LOG_FILE=/var/log/qlikai/qlik-ai.log \
    QLIK_CERT_DIR=/certs

EXPOSE 8000

# /healthz is deliberately reachable without signing in, which is what makes
# this possible. urllib rather than curl, because the slim image has no curl
# and adding one for a health check is 10 MB for one line.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# 0.0.0.0 inside the container is not the same as exposing it: compose
# publishes to 127.0.0.1 on the host, so only the reverse proxy can reach it.
#
# One process, deliberately. The session registry and the shared Qlik
# connection live in memory, so a second worker would give users a different
# conversation depending on which one took the request. Scale the container
# up, not out.
CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8000"]
