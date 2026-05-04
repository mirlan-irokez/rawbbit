FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
COPY src ./src

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

CMD ["sh", "-c", "uvicorn collector_api.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
