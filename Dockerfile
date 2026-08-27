FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 qlikai \
    && mkdir -p /data/history /var/log/qlikai \
    && chown -R qlikai:qlikai /app /data /var/log/qlikai
USER qlikai

ENV USERS_DB=/data/users.db \
    HISTORY_DIR=/data/history \
    LOG_FILE=/var/log/qlikai/qlik-ai.log \
    QLIK_CERT_DIR=/certs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8000"]
