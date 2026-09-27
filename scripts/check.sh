#!/usr/bin/env bash
# Run the official acceptance checker against a clean, freshly seeded portal.
#
# Uses its own compose project and volume ("dogfood-check"), so it never touches
# the data of the portal you normally run. Writes acceptance-report.txt and exits
# non-zero if any claimed tier was not verified.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=dogfood-check
cleanup() { docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

if python3 -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1', 8080)) != 0)"; then
    echo "port 8080 is in use; stop the running portal first (docker compose stop)" >&2
    exit 2
fi

cleanup
docker compose -p "$PROJECT" up -d --build --wait
python3 run.py .dogfood.toml | tee acceptance-report.txt

if grep -q "^note: claimed but not verified" acceptance-report.txt; then
    echo "FAILED: a claimed tier was not verified" >&2
    exit 1
fi
