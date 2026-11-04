# Pinned minor version + slim Debian base: small, patched, reproducible.
FROM python:3.12-slim-bookworm

# PTB_TIMEDELTA opts in to python-telegram-bot's future timedelta API
# (silences a deprecation warning; our code handles both forms).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PTB_TIMEDELTA=true

WORKDIR /app

# Dependencies first so this layer is cached between code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Unprivileged user with a fixed uid. /app/data is the only writable path
# (SQLite database + scheduler lock, mounted as a volume).
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown appuser:appuser /app/data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

# Exactly one worker: the scheduler must run once (a file lock enforces this too).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header", "--timeout-graceful-shutdown", "25"]
