#!/bin/bash
# Monthly integrity check of the VPS restic repo. The desktop has its own
# snapshot repository and heartbeat; the retired Windows PC repo is gone.
set -uo pipefail

LOG=/var/log/restic-check.log
PASSFILE=/root/.restic-passphrase
VPS_REPO="${VPS_REPO:-}"

if [ ! -r "$PASSFILE" ]; then
  echo "[$(date +%Y%m%d-%H%M%S)] CHECK ABORTED: $PASSFILE missing or unreadable" >> "$LOG"
  exit 1
fi
export RESTIC_PASSWORD="$(cat "$PASSFILE")"
if [ -z "$RESTIC_PASSWORD" ] || [ -z "$VPS_REPO" ]; then
  echo "[$(date +%Y%m%d-%H%M%S)] CHECK ABORTED: empty passphrase or VPS_REPO" >> "$LOG"
  exit 1
fi

# Guard against overlap so restic's exclusive lock cannot collide with a backup.
exec 9>/var/lock/restic-check.lock
if ! flock -n 9; then
  echo "[$(date +%Y%m%d-%H%M%S)] CHECK SKIPPED: another restic-check holds the lock" >> "$LOG"
  exit 1
fi

export RESTIC_REPOSITORY="sftp:${VPS_REPO}"
echo "[$(date +%Y%m%d-%H%M%S)] === RESTIC CHECK START ===" >> "$LOG"
if restic check --read-data-subset=5% >> "$LOG" 2>&1; then
  echo "[$(date +%Y%m%d-%H%M%S)] CHECK OK: VPS repo" >> "$LOG"
  exit 0
else
  RC=$?
  echo "[$(date +%Y%m%d-%H%M%S)] CHECK FAILED: VPS repo (rc=$RC)" >> "$LOG"
  exit "$RC"
fi
