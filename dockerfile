# Dockerfile (repo root) - updated for threaded gunicorn, connection pooling, and timeouts
FROM python:3.11-slim

# System deps (keep minimal)
RUN apt-get update && \
    apt-get install -y build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application code
COPY . /app

ENV PORT=8080
EXPOSE 8080

# Recommended runtime env defaults (can be overridden in Cloud Run)
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=8
ENV GUNICORN_TIMEOUT=120
ENV OUTBOUND_TIMEOUT=30

# CSV validation limits (30-100 users per upload)
ENV MIN_RECORDS=30
ENV MAX_RECORDS=100

# Use gunicorn gthread worker so background threads and connection pooling work correctly.
# -w: number of worker processes
# -k gthread --threads: thread pool per worker (allows background thread usage)
# --timeout: worker timeout (increase to avoid worker kills during debugging)
CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS} -k gthread --threads ${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} --bind 0.0.0.0:${PORT} app:app"]
