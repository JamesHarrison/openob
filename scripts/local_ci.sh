#!/usr/bin/env bash
# Run the OpenOB CI pipeline locally inside Docker. Identical to what runs in
# GitHub Actions. Forwards any args to pytest, e.g.:
#   ./scripts/local_ci.sh tests/test_endtoend.py::test_pink_noise_round_trip_opus -v
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker not found on PATH" >&2
    exit 1
fi

docker compose build tests

if [[ $# -gt 0 ]]; then
    exec docker compose run --rm tests pytest "$@"
fi

exec docker compose run --rm tests
