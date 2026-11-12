#!/bin/bash
# Backup freshness watchdog — pushes a Gotify alert if the nightly chain silently
# stops running (cron dead, master rebooted at wrong time, etc). Failure alerts in
# restic-backup.sh only fire when the script RUNS and FAILS; this covers "never ran".
# Runs daily at 06:30; the desktop pull has its own Kuma 26h heartbeat.
# Alert dedupe: once per distinct failure per day via the marker file.

LOG=/var/log/restic-backup.log
TOKEN_FILE=/root/.gotify-backup-token
GOTIFY="http://gotify.private.svc.cluster.local"
MARKER=/root/.backup-watchdog-alerted
TODAY=$(date +%Y%m%d)
VPS_SSH="${VPS_SSH:-}"

fail() {
  # dedupe: only once per day
  [ -f "$MARKER" ] && grep -q "^$TODAY:$1$" "$MARKER" && return 0
  local TOK
  TOK=$(cat "$TOKEN_FILE" 2>/dev/null)
  if [ -z "$TOK" ]; then
    echo "[$(date +%Y%m%d-%H%M%S)] WATCHDOG ALERT SKIPPED: $TOKEN_FILE missing or empty" >> "$LOG"
    return 0
  fi
  # Token in a header, not the URL (query strings leak into logs). --fail so a
  # rejected POST never gets recorded as a delivered alert; marker only on 2xx.
  # JSON body, not Title/Priority headers: Gotify 400s header-style messages, which
  # is why every watchdog push since 2026-11-05 logged "alerting broken".
  # python builds the JSON — no sed escaping games with quotes/newlines.
  local PAYLOAD
  PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"title":sys.argv[1],"message":sys.argv[2],"priority":8}))' "BACKUP WATCHDOG: $1" "$2")
  curl -sS --fail -m 10 -X POST "$GOTIFY/message" \
    -H "X-Gotify-Key: $TOK" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" > /dev/null 2>&1; rc=$?
  if [ $rc -eq 0 ]; then
    echo "[$(date +%Y%m%d-%H%M%S)] WATCHDOG ALERT pushed: $1" >> "$LOG"
    echo "$TODAY:$1" >> "$MARKER"
  else
    echo "[$(date +%Y%m%d-%H%M%S)] WATCHDOG ALERT FAILED to push: $1 (alerting broken)" >> "$LOG"
  fi
}

# 1. Today 02:30 restic run must show both legs OK
TODAYS_RUN=$(awk -v d="[$TODAY-02" "index(\$0,d)==1" "$LOG" 2>/dev/null | wc -l)
if [ "$TODAYS_RUN" -eq 0 ]; then
  fail "nightly restic missing" "No restic-backup.sh log entries found for today ($TODAY). Cron dead? Master was rebooted/suspended at 02:30? Check /var/log/restic-backup.log"
  exit 0
fi
VPS_OK=$(grep -c "^\[$TODAY-.*VPS backup OK" "$LOG")
[ "$VPS_OK" -eq 0 ] && fail "VPS leg failed" "No VPS backup OK for $TODAY. restic-backup.sh ran but VPS push failed (or alert dedupe swallowed it)."
# The CachyOS desktop pulls the stage independently; Kuma monitor 75 watches
# its 26h heartbeat, so the retired Windows SFTP leg is deliberately not read.

# 2. PG dump freshness (02:00 daily)
PG_DIR=/root/pg-dumps
NEWEST_PG=$(find "$PG_DIR" -name "*.sql.gz" -mmin -1560 2>/dev/null | wc -l)
[ "$NEWEST_PG" -eq 0 ] && fail "pg dumps stale" "No pg-backup dump updated in the last 26h (dir: $PG_DIR). pg-backup.sh (02:00 cron) stopped?"

# 3. Longhorn offsite backup freshness (04:00 daily, keep-7)
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
LH_NEW=$(kubectl -n longhorn-system get backups.longhorn.io -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
import datetime
cutoff=datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=26)
n=0
for b in d[\"items\"]:
    ts=b[\"status\"].get(\"lastCompletionTime\") or b.get(\"metadata\",{}).get(\"creationTimestamp\")
    if ts and datetime.datetime.fromisoformat(ts.replace(\"Z\",\"+00:00\"))>cutoff: n+=1
print(n)")
[ "${LH_NEW:-0}" -eq 0 ] && fail "longhorn offsite stale" "No Longhorn backup completed in last 26h. daily-backup recurring job (04:00) failing? Check longhorn-ui Backups."

# 4. Backup TARGET capacity: pods can stay green while the export is full.
TARGET_AVAIL_KB=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_SSH" "df --output=avail -k /export/longhorn-backups" 2>/dev/null | awk 'END {print $1}') || true
if ! [[ "$TARGET_AVAIL_KB" =~ ^[0-9]+$ ]]; then
  fail "backup target probe failed" "Could not read free space on the Longhorn backup target over SSH."
elif [ "$TARGET_AVAIL_KB" -lt 20971520 ]; then
  fail "backup target low" "Longhorn backup target has under 20 GiB free ($((TARGET_AVAIL_KB / 1048576)) GiB). Reclaim orphan backups or expand it before the next nightly run."
fi

echo "[$(date +%Y%m%d-%H%M%S)] WATCHDOG ok (vps=$VPS_OK pg=$NEWEST_PG lh=$LH_NEW target_free_kib=$TARGET_AVAIL_KB)" >> "$LOG"
