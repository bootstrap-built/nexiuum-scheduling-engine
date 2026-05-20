FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi>=0.115 \
    "uvicorn[standard]>=0.32" \
    httpx>=0.27 \
    pydantic>=2.9 \
    python-dotenv>=1.0 \
    sse-starlette>=2.1

COPY engine/ ./engine/

ENV PORT=8002
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

CMD ["sh", "-c", "uvicorn engine.main:app --host 0.0.0.0 --port ${PORT}"]
