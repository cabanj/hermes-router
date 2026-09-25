#!/bin/bash
# Daily router audit — 06:00 UTC via cron
# Fetch → rank → diff vs current config → apply → verify
set -euo pipefail

LOCK_FILE="/tmp/router-audit.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another audit already running"; exit 0; }

cd /opt/hermes-router
echo "=== Router Audit $(date -u +%Y-%m-%d\ %H:%M:%SZ) ==="

# Dry-run first to show what would change. Under `set -e` a non-zero exit here
# would abort the script before APPLY ever runs, so capture and continue.
echo "--- DRY RUN ---"
DRY_EXIT=0
python3 /opt/hermes-router/scripts/router_audit.py --dry-run || DRY_EXIT=$?

# Then auto-apply (safe: verify_chains rolls back on failure)
echo "--- APPLY ---"
APPLY_EXIT=0
python3 /opt/hermes-router/scripts/router_audit.py --apply || APPLY_EXIT=$?

echo "=== Router Audit COMPLETE (dry=$DRY_EXIT apply=$APPLY_EXIT) ==="
exit $APPLY_EXIT