FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY config.yaml /app/sqlmesh_project/config.yaml
COPY models /app/sqlmesh_project/models
COPY macros /app/sqlmesh_project/macros
COPY audits /app/sqlmesh_project/audits
COPY tests /app/sqlmesh_project/tests

WORKDIR /app/sqlmesh_project

CMD ["sqlmesh", "-p", "/app/sqlmesh_project", "run", "prod"]
