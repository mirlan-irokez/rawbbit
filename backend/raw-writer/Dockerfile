FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt ./
COPY src ./src

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-m", "raw_writer.main"]
