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


# default command; override at deploy with --image or --command/--args
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "4", "--timeout", "300", "publisher.app:app"]