# Demo / PoC image. SQLite is durable when /data is mounted; it is not tenant isolation.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_STORE=/data/app.sqlite

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
RUN mkdir -p /data

VOLUME ["/data"]

EXPOSE 8000

# Render / Cloud Run / Railway set PORT. Default 8000 for local docker run.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
