FROM python:3.12-slim

WORKDIR /app

# curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copy source and install dependencies
COPY pyproject.toml .
COPY backend/ backend/
COPY collect.py .
RUN pip install --no-cache-dir .

# Copy frontend (static files, no Python deps)
COPY frontend/ frontend/

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
