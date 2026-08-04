FROM python:3.12-slim-bookworm

ARG TARGETARCH=amd64
ARG SUPERCRONIC_VERSION=v0.2.48
ARG DBT_UID=1000
ARG DBT_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DBT_PROFILES_DIR=/app \
    DBT_PROJECT_DIR=/app \
    DBT_LOG_PATH=/app/runtime/logs \
    DBT_TARGET_PATH=/app/runtime/target \
    TZ=UTC

RUN apt-get update \
    && apt-get install --no-install-recommends -y bash ca-certificates curl git util-linux \
    && case "${TARGETARCH}" in \
         amd64) supercronic_sha256="88c1b66b94c486f972fdd1a4d1f901e3e75ff04f749cddd60c5db573e3a33c6c" ;; \
         arm64) supercronic_sha256="50ae8755e04fa72812d0a1bc47a112a856811cc91cce7b6c875c378a850788bc" ;; \
         *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
       -o /usr/local/bin/supercronic \
    && echo "${supercronic_sha256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/supercronic \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . /app

RUN groupadd --gid "${DBT_GID}" dbt \
    && useradd --uid "${DBT_UID}" --gid "${DBT_GID}" --create-home --shell /bin/bash dbt \
    && mkdir -p /app/runtime/logs /app/runtime/target \
    && chown -R dbt:dbt /app

USER dbt

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["supercronic", "-test", "/app/crontab"]

CMD ["supercronic", "-passthrough-logs", "/app/crontab"]
