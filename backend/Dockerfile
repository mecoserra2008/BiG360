# BiG Equity 360 backend image (API + worker share it). OPTIONAL path: the native Windows run (run_all.ps1) is primary.
# Build context = project root:   docker build -f backend/Dockerfile -t big360-backend .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BIG360_ROOT=/app \
    BIG360_API_HOST=0.0.0.0 \
    BIG360_SCHEDULER_TZ=Europe/Lisbon \
    TZ=Europe/Lisbon

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl tzdata libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/requirements.txt /app/backend/requirements.txt
# torch CPU wheels via the extra index declared inside requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY contracts /app/contracts
COPY backend /app/backend

# data/ is a bind mount (see docker-compose.yml); .env is mounted read-only at /app/.env
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
