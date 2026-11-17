#!/usr/bin/env bash
# Restart the SplitBill user service on the code currently checked out, after the test suite passes.
# Usage: deploy/restart.sh [--skip-tests]
set -euo pipefail

UNIT="splitbill.service"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
PORT="$(grep -E '^PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2 || true)"
PORT="${PORT:-8080}"
WAIT_SECONDS=20

fail() { echo "❌ $*" >&2; exit 1; }

cd "$ROOT"
[[ -x "$PYTHON" ]] || fail "No virtualenv at $PYTHON (python -m venv .venv && .venv/bin/pip install -r requirements.txt)"
echo "Code: $(git rev-parse --abbrev-ref HEAD) @ $(git log -1 --format='%h %s')"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || echo "⚠️  Uncommitted changes will be deployed too"

if [[ "${1:-}" != "--skip-tests" ]]; then
    echo "Running tests..."
    "$PYTHON" src/split.py >/dev/null || fail "split.py checks failed, service not restarted"
    "$PYTHON" tests/test_app.py >/dev/null 2>&1 || fail "tests/test_app.py failed, service not restarted (run it to see why)"
    echo "Tests: ok"
fi

started_at="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl --user restart "$UNIT"

for _ in $(seq "$WAIT_SECONDS"); do
    if curl -fs -o /dev/null -m 2 "http://127.0.0.1:$PORT/"; then
        echo "✅ $UNIT is up on :$PORT"
        journalctl --user -u "$UNIT" --since "$started_at" --no-pager -o cat | tail -n 5
        exit 0
    fi
    systemctl --user is-active --quiet "$UNIT" || break
    sleep 1
done

journalctl --user -u "$UNIT" --since "$started_at" --no-pager -o cat | tail -n 30
fail "$UNIT did not answer on :$PORT within ${WAIT_SECONDS}s (logs above)"
