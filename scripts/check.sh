#!/usr/bin/env bash
# Run the official acceptance checker against a clean, freshly seeded portal.
#
# Uses its own compose project and volume ("dogfood-check"), so it never touches
# the data of the portal you normally run. Writes acceptance-report.txt and exits
# non-zero if any claimed tier was not verified.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=dogfood-check
# A system proxy must not intercept the checker's requests to localhost.
export no_proxy="localhost,127.0.0.1,::1${no_proxy:+,$no_proxy}" NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}"
cleanup() { docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

if python3 -c "
import socket, sys
for fam, addr in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
    try:
        if socket.socket(fam).connect_ex((addr, 8080)) == 0:
            sys.exit(0)
    except OSError:
        pass
sys.exit(1)"; then
    echo "port 8080 is in use; stop the running portal first (docker compose stop)" >&2
    exit 2
fi

cleanup
docker compose -p "$PROJECT" up -d --build --wait
python3 run.py .dogfood.toml | tee acceptance-report.txt

# run.py only has checks for T1 and T2; T3 and T4 are judged by hand, so run.py always lists them
# as "claimed but not verified". Fail only if a machine-checked tier did not verify.
if ! grep -q "^claimed .*, verified T1 T2$" acceptance-report.txt; then
    echo "FAILED: T1 and T2 were not both verified" >&2
    exit 1
fi
if grep "^note: claimed but not verified" acceptance-report.txt | grep -qw "T1\|T2"; then
    echo "FAILED: a machine-checked tier was not verified" >&2
    exit 1
fi
