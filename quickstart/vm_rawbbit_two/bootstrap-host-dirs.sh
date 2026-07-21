#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${RAWBBIT_TWO_ROOT:-/srv/rawbbit-two}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script creates host directories under ${ROOT_DIR}; run it with sudo." >&2
  exit 1
fi

dirs=(
  "${ROOT_DIR}/clickhouse/data"
  "${ROOT_DIR}/clickhouse/logs"
  "${ROOT_DIR}/postgres"
  "${ROOT_DIR}/caddy/data"
  "${ROOT_DIR}/caddy/config"
)

echo "Creating Rawbbit two-VM host directories under ${ROOT_DIR}"
for dir in "${dirs[@]}"; do
  install -d -m 0755 "${dir}"
  echo "created_or_verified ${dir}"
done

echo
echo "Done. This script does not create .env, API keys, database passwords, or other secrets."
