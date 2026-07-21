#!/usr/bin/env bash
set -euo pipefail

QUICKSTART_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEDULE="${RAWBBIT_TWO_LOADER_CRON:-7 * * * *}"
LOG_FILE="${RAWBBIT_TWO_LOADER_LOG:-${HOME}/rawbbit-two-load-events.log}"
MARKER="# rawbbit-two-load-events"

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab command not found. Install cron before running this script." >&2
  exit 1
fi

if [[ ! -f "${QUICKSTART_DIR}/.env" ]]; then
  echo "Missing ${QUICKSTART_DIR}/.env. Create and validate .env before installing cron." >&2
  exit 1
fi

if [[ ! -f "${QUICKSTART_DIR}/clickhouse/load_events_hourly.sh" ]]; then
  echo "Missing clickhouse/load_events_hourly.sh in ${QUICKSTART_DIR}." >&2
  exit 1
fi

printf -v quoted_quickstart_dir "%q" "${QUICKSTART_DIR}"
printf -v quoted_log_file "%q" "${LOG_FILE}"

job="${SCHEDULE} cd ${quoted_quickstart_dir} && bash clickhouse/load_events_hourly.sh >> ${quoted_log_file} 2>&1 ${MARKER}"
existing_cron="$(mktemp)"
new_cron="$(mktemp)"
trap 'rm -f "${existing_cron}" "${new_cron}"' EXIT

crontab -l > "${existing_cron}" 2>/dev/null || true
grep -vF "${MARKER}" "${existing_cron}" > "${new_cron}" || true
printf "%s\n" "${job}" >> "${new_cron}"

crontab "${new_cron}"

echo "Installed Rawbbit two hourly loader cron:"
echo "${job}"
