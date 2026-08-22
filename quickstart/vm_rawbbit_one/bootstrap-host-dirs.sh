#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${RAWBBIT_ONE_ROOT:-/srv/rawbbit-one}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script creates host directories under ${ROOT_DIR}; run it with sudo." >&2
  exit 1
fi

runtime_owner="${RAWBBIT_ONE_RUNTIME_OWNER:-${SUDO_USER:-root}}"
runtime_group="$(id -gn "${runtime_owner}")"

runtime_dirs=(
  "${ROOT_DIR}/nats/jetstream"
  "${ROOT_DIR}/seaweedfs/master"
  "${ROOT_DIR}/seaweedfs/volume"
  "${ROOT_DIR}/seaweedfs/filerldb2"
)

root_dirs=(
  "${ROOT_DIR}/caddy/data"
  "${ROOT_DIR}/caddy/config"
)

echo "Creating Rawbbit one-VM host directories under ${ROOT_DIR}"

for dir in "${runtime_dirs[@]}"; do
  install -d -m 0755 \
    -o "${runtime_owner}" \
    -g "${runtime_group}" \
    "${dir}"
  echo "created_or_verified ${dir} owner=${runtime_owner}:${runtime_group}"
done

for dir in "${root_dirs[@]}"; do
  install -d -m 0755 "${dir}"
  echo "created_or_verified ${dir}"
done

echo
echo "Done. This script does not create .env, s3.json, API keys, or other secrets."
