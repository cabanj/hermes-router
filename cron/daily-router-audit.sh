#!/bin/bash
# Daily router audit — 06:00 UTC via cron
# Fetch → rank → diff vs current config → apply → verify
set -euo pipefail

LOCK_FILE="/tmp/router-audit.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another audit already running"; exit 0; }

cd /opt/hermes-router
echo "=== Router Audit $(date -u +%Y-%m-%d\ %H:%M:%SZ) ==="

# Dry-run first to show what would change
echo "--- DRY RUN ---"
python3 /opt/hermes-router/scripts/router_audit.py --dry-run
DRY_EXIT=$?

# Always write changelog entry in dry-run mode too
# Then auto-apply (safe because verify_chains rolls back on failure)
echo "--- APPLY ---"
python3 /opt/hermes-router/scripts/router_audit.py --apply
APPLY_EXIT=$?

echo "=== Router Audit COMPLETE (apply=$APPLY_EXIT) ==="
exit $APPLY_EXIT