#!/usr/bin/env bash
# One-command burst test runner: ./scripts/burst.sh [base_url] [concurrency]
set -euo pipefail
BASE_URL="${1:-http://localhost:8000}"
N="${2:-20}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/burst_test.py" --base-url "$BASE_URL" --scenario all --n "$N"
