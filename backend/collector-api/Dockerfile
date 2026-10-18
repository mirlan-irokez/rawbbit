FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/mirlan-irokez/rawbbit"
LABEL org.opencontainers.image.description="Rawbbit collector API"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
COPY src ./src

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

# exec makes uvicorn PID 1 so it receives SIGTERM and finishes in-flight requests.
CMD ["sh", "-c", "exec uvicorn collector_api.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers ${UVICORN_WORKERS:-1}"]
