# worker/Dockerfile
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# Use gunicorn to bind to 0.0.0.0:8080; use module path worker.worker:app if your app is worker/worker.py
CMD ["gunicorn", "-w", "1", "-k", "gthread", "--threads", "4", "--bind", "0.0.0.0:8080", "worker.worker:app"]
