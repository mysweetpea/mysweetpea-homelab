#!/bin/bash
# NOTE: sanitized reference. Set VPS_REPO, VPS_SSH and KUMA_PUSH_TOKEN on the
# master. The desktop pull is a separate local script with its own monitor.
exec 9>/run/restic-backup.lock
flock -n 9 || { echo "restic-backup.sh: another instance already running, exiting"; exit 0; }
# MySweetPea homelab backup script — restic to VPS (off-site); the CachyOS
# desktop pulls the retained stage into its own restic repo after this run.
# Backs up: sealed-secrets key, k3s state.db, pg-dumps, config-backup
set -euo pipefail

export RESTIC_PASSWORD="$(cat /root/.restic-passphrase)"
VPS_REPO="${VPS_REPO:-}"
KUMA_PUSH_TOKEN="${KUMA_PUSH_TOKEN:-}"
LOG="/var/log/restic-backup.log"

# ---- failure alerting (Gotify "Backup Alerts" app id 12) ----
notify_fail() {
  local WHAT="$1" DETAIL="$2"
  local TOKEN
  TOKEN=$(cat /root/.gotify-backup-token 2>/dev/null) || return
  if [ -z "$TOKEN" ]; then
    echo "[$(date +%Y%m%d-%H%M%S)] ALERT SKIPPED: /root/.gotify-backup-token missing or empty" >> "$LOG"
    return
  fi
  # Token in a header, not the URL (query strings leak into access logs).
  # --fail + conditional marker: never log "ALERT pushed" for a rejected POST.
  curl -sS --fail --max-time 10 -X POST "http://10.43.11.212/message" \
       -H "X-Gotify-Key: $TOKEN" \
       -H "Content-Type: application/json" \
       -d "{\"title\":\"BACKUP FAILED: $WHAT\",\"message\":\"$DETAIL - check /var/log/restic-backup.log on k3s-master\",\"priority\":8}" >/dev/null; rc=$?
  if [ $rc -eq 0 ]; then
    echo "[$(date +%Y%m%d-%H%M%S)] ALERT pushed: $WHAT" >> "$LOG"
  else
    echo "[$(date +%Y%m%d-%H%M%S)] ALERT FAILED to push for "$WHAT" (rc=$rc) - alerting is broken, investigate" >> "$LOG"
  fi
}
DATE="$(date +%Y%m%d-%H%M%S)"

echo "[$DATE] === RESTIC BACKUP START ===" >> "$LOG"

# 1. Sealed-secrets key (CRITICAL — without it, all sealed secrets are undecryptable)
: > /tmp/sealed-secrets-key.yaml
kubectl get secrets -n kube-system -o name 2>/dev/null | grep sealed-secrets-key | while read s; do kubectl get "$s" -n kube-system -o yaml >> /tmp/sealed-secrets-key.yaml 2>/dev/null; done || true
if [ -s /tmp/sealed-secrets-key.yaml ]; then
  echo "[$DATE] sealed-secrets key captured" >> "$LOG"
else
  echo "[$DATE] WARNING: sealed-secrets key NOT found!" >> "$LOG"
fi

# 2. k3s state.db (SQLite — cluster state)
if [ -f /var/lib/rancher/k3s/server/db/state.db ]; then
  sqlite3 /var/lib/rancher/k3s/server/db/state.db ".backup /tmp/k3s-state.db" 2>/dev/null || cp /var/lib/rancher/k3s/server/db/state.db /tmp/k3s-state.db
  echo "[$DATE] k3s state.db captured" >> "$LOG"
fi

# 3. Backup staging dir
STAGE="/var/backups/restic-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/sealed-secrets" "$STAGE/k3s" "$STAGE/pg-dumps" "$STAGE/config-backup"

cp /tmp/sealed-secrets-key.yaml "$STAGE/sealed-secrets/" 2>/dev/null || true
gzip -c /tmp/k3s-state.db > "$STAGE/k3s/state.db.gz" 2>/dev/null && rm -f /tmp/k3s-state.db || true
cp -r /root/pg-dumps/* "$STAGE/pg-dumps/" 2>/dev/null || true
cp -r /root/config-backup/* "$STAGE/config-backup/" 2>/dev/null || true

# 3b. Vaultwarden vault data (SQLite WAL — tar from live pod; tiny DB, WAL replays on open)
mkdir -p "$STAGE/vaultwarden"
if kubectl exec -n dmz deploy/vaultwarden -- tar czf - -C /data . > "$STAGE/vaultwarden/vault-data.tar.gz" 2>/dev/null; then
  echo "[$DATE] vaultwarden data captured ($(du -h "$STAGE/vaultwarden/vault-data.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: vaultwarden backup failed" >> "$LOG"
fi

# 3c. Matrix media store + signing key (tar from live pod; media PVC mounted at /synapse/data)
mkdir -p "$STAGE/matrix"
if kubectl exec -n dmz deploy/matrix-synapse -- tar czf - -C /synapse/data . > "$STAGE/matrix/media.tar.gz" 2>/dev/null; then
  echo "[$DATE] matrix media captured ($(du -h "$STAGE/matrix/media.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: matrix media backup failed" >> "$LOG"
fi
if kubectl get secret -n dmz matrix-synapse-signingkey -o jsonpath='{.data.signing\.key}' 2>/dev/null | base64 -d > "$STAGE/matrix/signing.key" 2>/dev/null; then
  echo "[$DATE] matrix signing key captured" >> "$LOG"
else
  echo "[$DATE] WARNING: matrix signing key backup failed" >> "$LOG"
fi

# 3d. AFFiNE uploads + config (blobs PVC at /root/.affine/storage; private.key in config)
mkdir -p "$STAGE/affine"
if kubectl exec -n dmz deploy/affine-main -- tar czf - -C /root/.affine/storage . > "$STAGE/affine/uploads.tar.gz" 2>/dev/null; then
  echo "[$DATE] affine uploads captured ($(du -h "$STAGE/affine/uploads.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: affine uploads backup failed" >> "$LOG"
fi
if kubectl exec -n dmz deploy/affine-main -- tar czf - -C /root/.affine/config . > "$STAGE/affine/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] affine config captured ($(du -h "$STAGE/affine/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: affine config backup failed" >> "$LOG"
fi
TAG="homelab-$(date +%Y%m%d)"
# 3k. Uptime Kuma data (kuma.db + uploads — data PVC at /app/data)
mkdir -p "$STAGE/uptime-kuma"
if kubectl exec -n monitoring deploy/uptime-kuma -- tar czf - -C /app/data --exclude=screenshots . > "$STAGE/uptime-kuma/data.tar.gz" 2>/dev/null; then
    echo "uptime-kuma data captured ($(du -h "$STAGE/uptime-kuma/data.tar.gz" | cut -f1))"
elif [ -s "$STAGE/uptime-kuma/data.tar.gz" ]; then
    # tar rc=1 = kuma.db-wal changed mid-read (Kuma writes continuously); archive still valid
    echo "uptime-kuma data captured ($(du -h "$STAGE/uptime-kuma/data.tar.gz" | cut -f1)) (live-DB tolerated)"
else
    echo "WARNING: uptime-kuma backup failed"
fi
# 3i. Prowlarr config + DB (config.xml, prowlarr.db — config PVC at /config)
mkdir -p "$STAGE/prowlarr"
if kubectl exec -n private deploy/prowlarr -- tar czf - -C /config --exclude=logs . > "$STAGE/prowlarr/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] prowlarr config captured ($(du -h "$STAGE/prowlarr/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: prowlarr config backup failed" >> "$LOG"
fi
# 3g. Jellyfin config (plugin configs, branding, DB, intros — config PVC at /config)
mkdir -p "$STAGE/jellyfin"
if kubectl exec -n private deploy/jellyfin -- tar czf - -C /config --exclude=cache --exclude=transcodes --exclude=plugins/Moonbase --exclude=metadata --exclude=plugins/configurations/GetAvatar --exclude=plugins/GetAvatar --exclude=data/whisper . > "$STAGE/jellyfin/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] jellyfin config captured ($(du -h "$STAGE/jellyfin/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: jellyfin config backup failed" >> "$LOG"
fi

# 3l. Grafana config + DB (grafana.db, plugins list, provisioning — PVC at /var/lib/grafana)
mkdir -p "$STAGE/grafana"
if kubectl exec -n monitoring deploy/grafana -- tar czf - -C /var/lib/grafana --exclude=plugins --exclude=png --exclude=csv --exclude=pdf . > "$STAGE/grafana/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] grafana config captured ($(du -h "$STAGE/grafana/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: grafana config backup failed" >> "$LOG"
fi

# 3q. Nextcloud data (user files + DB-adjacent data dir; previews regenerable - excluded)
mkdir -p "$STAGE/nextcloud"
kubectl exec -n private deploy/nextcloud -c nextcloud -- tar czf - --warning=no-file-changed -C /var/www/html/data --exclude="*/files_trashbin" --exclude="*/files_versions" --exclude="*/__groupfolders/*/trash" --exclude="*/__groupfolders/*/versions" . > "$STAGE/nextcloud/data.tar.gz" 2>/tmp/nc.err; rc=$?
if [ $rc -le 1 ]; then
  echo "[$DATE] nextcloud data captured ($(du -h "$STAGE/nextcloud/data.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: nextcloud backup failed" >> "$LOG"
fi


# 3m. Immich library + uploads + internal DB backups (data PVC at /data; thumbs/encoded-video regenerable)
mkdir -p "$STAGE/immich"
kubectl exec -n private deploy/immich-server -- tar czf - --warning=no-file-changed -C /data --exclude=thumbs --exclude=encoded-video . > "$STAGE/immich/library.tar.gz" 2>/tmp/im.err; rc=$?
if [ $rc -le 1 ]; then
  echo "[$DATE] immich library captured ($(du -h "$STAGE/immich/library.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: immich library backup failed" >> "$LOG"
fi

# 3n. Open WebUI data (webui.db + uploads + vector_db; cache 1.3G regenerable — excluded)
mkdir -p "$STAGE/open-webui"
kubectl exec -n private deploy/open-webui -- tar czf - --warning=no-file-changed -C /app/backend/data --exclude=cache . > "$STAGE/open-webui/data.tar.gz" 2>/tmp/ow.err; rc=$?
if [ $rc -le 1 ]; then
  echo "[$DATE] open-webui data captured ($(du -h "$STAGE/open-webui/data.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: open-webui backup failed" >> "$LOG"
fi

# 3o. Netdata parent: alarm log + config (metric DB 2.3G regenerable — excluded)
PARENT_POD=$(kubectl get pods -n monitoring -o name | grep netdata-parent | head -1 | cut -d/ -f2 || true)
if [ -n "$PARENT_POD" ]; then
  kubectl exec -n monitoring "$PARENT_POD" -- tar czf - -C /var/lib netdata 2>/dev/null > $STAGE/netdata-alarms.tar.gz || true
  echo "netdata: alarms + config captured"
else
  echo "netdata: parent pod not found"
fi

# 3p/3s. retired 2026-10/11 (qbittorrent, rustdesk) — dead sources removed.
# Their kubectl lookups used to fail under `set -euo pipefail` and silently killed
# the whole backup before the VPS leg ran (found 2026-11-08: monitor 56 down 3 days).

# 3r. Gotify data (gotify.db + images + plugins — data PVC at /app/data; tiny, ~120KB)
GOTIFY_POD=$(kubectl get pods -n private -l app.kubernetes.io/name=gotify -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$GOTIFY_POD" ]; then
    kubectl exec -n private "$GOTIFY_POD" -- tar czf - -C /app/data . 2>/dev/null > "$STAGE/gotify-data.tar.gz" &&     echo "gotify data: OK ($(du -h $STAGE/gotify-data.tar.gz | cut -f1))" || echo "gotify data: FAILED"
else
    echo "gotify: pod not found, skipping"
fi

# 3e. retired 2026-10 (rustdesk server) — dead source removed (PVC no longer exists)

# 4a. Restic backup to VPS (primary, off-site)
# Empty-archive guard: a tar that exits 0 but streams nothing (pod restart mid-exec,
# race with an init container) used to be logged as "captured (0)" and sail into the
# snapshot looking healthy. One line, whole class.
EMPTY_ARCHIVES=$(find "$STAGE" -name '*.tar.gz' -size 0 -printf '%P ' 2>/dev/null || true)
if [ -n "$EMPTY_ARCHIVES" ]; then
  echo "[$DATE] WARNING: empty archives in this snapshot: $EMPTY_ARCHIVES" >> "$LOG"
  notify_fail "empty backups" "These staged archives were 0 bytes: $EMPTY_ARCHIVES"
fi
# 3z. AIOStreams instance on the VPS + moonbase-lite data (added 2026-10-13)
VPS_USER="${VPS_SSH:-}"
mkdir -p "$STAGE/aiostreams"
# Never tar a live WAL database. The VPS helper uses SQLite's online backup API,
# checks integrity, and atomically renames a completed db-backup.sqlite.
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_USER" "sudo sh -c 'python3 /opt/aiostreams/sqlite-backup.py && cd /opt/aiostreams && tar czf - compose.yaml .env data/db-backup.sqlite'" > "$STAGE/aiostreams/config.tar.gz" 2>>"$LOG" && [ -s "$STAGE/aiostreams/config.tar.gz" ]; then
  echo "[$DATE] AIOStreams VPS snapshot: $(du -h "$STAGE/aiostreams/config.tar.gz" | cut -f1) (SQLite integrity OK)" >> "$LOG"
else
  echo "[$DATE] AIOStreams VPS snapshot FAILED" >> "$LOG"
  notify_fail "AIOStreams SQLite snapshot" "Online SQLite backup or transfer failed; no fresh snapshot was marked healthy"
  exit 1
fi
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_USER" 'sudo tar czf - -C /opt/moonbase-lite config.json data' > "$STAGE/moonbase-lite.tar.gz" 2>>"$LOG" && [ -s "$STAGE/moonbase-lite.tar.gz" ]; then
  echo "[$DATE] moonbase-lite snapshot: $(du -h "$STAGE/moonbase-lite.tar.gz" | cut -f1)" >> "$LOG"
else
  echo "[$DATE] moonbase-lite snapshot FAILED - continuing" >> "$LOG"
fi
# 3z2. LiteLLM gateway + Ollama spend history (added 2026-11-07; moved PC -> VPS)
# pg_dumpall rather than the data dir: consistent snapshot, ~2 MB vs 70 MB, no ownership games.
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_USER" 'sudo sh -c "cd /opt/llm-proxy && tar czf - --ignore-failed-read compose.yaml .env config.yaml prices.json state.json keys.json ollama-spend history.jsonl"' > "$STAGE/llm-proxy-config.tar.gz" 2>>"$LOG" && [ -s "$STAGE/llm-proxy-config.tar.gz" ]; then
  echo "[$DATE] llm-proxy config snapshot: $(du -h "$STAGE/llm-proxy-config.tar.gz" | cut -f1)" >> "$LOG"
else
  echo "[$DATE] llm-proxy config snapshot FAILED - continuing" >> "$LOG"
fi
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_USER" 'sudo docker exec llm-proxy-db-1 pg_dumpall -U litellm' 2>>"$LOG" | gzip > "$STAGE/llm-proxy-db.sql.gz" && [ -s "$STAGE/llm-proxy-db.sql.gz" ]; then
  echo "[$DATE] llm-proxy spend DB dump: $(du -h "$STAGE/llm-proxy-db.sql.gz" | cut -f1)" >> "$LOG"
else
  echo "[$DATE] llm-proxy spend DB dump FAILED - continuing" >> "$LOG"
fi

export RESTIC_REPOSITORY="$VPS_REPO"
# SKIP_VPS=1 is set by restic-pc-retry.sh: that morning run exists to redo the PC leg, and
# re-pushing to the VPS just adds a second offsite snapshot per day, eating the retention
# window (5 daily slots became 2.5 real days while the PC leg was broken).
if [ "${SKIP_VPS:-0}" = "1" ]; then
  echo "[$DATE] VPS leg skipped (SKIP_VPS=1 — PC retry run)" >> "$LOG"
else
restic unlock >/dev/null 2>&1 || true   # clear stale locks; repo-unreachable must NOT kill the set -e script (fixed 2026-09-22)
# Retention is deliberately tight: the VPS root disk is 45G shared with everything else and
# churn measured ~76 MB/day (2026-11-08: +1.89 GiB over 25 days). Daily 5 / weekly 2 /
# monthly 2 bounds the repo at a handful of snapshots while still covering "deleted Monday,
# noticed Friday". The old 7/4/6 kept month-old anchors that pinned ~2.4 GiB of dead blobs.
if restic backup "$STAGE" --tag "$TAG" --exclude "*.log" >> "$LOG" 2>&1; then
  restic forget --keep-daily 5 --keep-weekly 2 --keep-monthly 2 --prune >> "$LOG" 2>&1 || true
  echo "[$DATE] VPS backup OK" >> "$LOG"
  # Disk floor: the one thing that must not surprise us is a full VPS root disk.
  VPS_FREE_KB=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$VPS_USER" 'df --output=avail / | tail -1' 2>/dev/null | tr -d ' ' || true)
  VPS_FREE_KB=${VPS_FREE_KB//[!0-9]/}
  if [ -n "$VPS_FREE_KB" ] && [ "$VPS_FREE_KB" -lt 4194304 ]; then
    echo "[$DATE] WARNING: VPS root disk low ($(( VPS_FREE_KB / 1024 / 1024 )) GiB free after pruning)" >> "$LOG"
    notify_fail "VPS disk" "Only $(( VPS_FREE_KB / 1024 / 1024 )) GiB free on the VPS root disk after prune"
  fi
else
  echo "[$DATE] VPS BACKUP FAILED" >> "$LOG"
  notify_fail "VPS restic push" "Nightly backup to VPS failed at $DATE (VPS down? network?)"
fi
fi

# 6. Heartbeat to Uptime Kuma push monitor (alerts if the whole chain stops running).
# BEFORE the PC leg on purpose: the PC repo can hang for minutes on a dead host, and a
# hung secondary leg must not mask a successful offsite backup (25 days of silence on
# 2026-10-14..11-08 were partly this shape).
KUMA_PUSH_URL="http://uptime-kuma.monitoring.svc.cluster.local:3001/api/push/${KUMA_PUSH_TOKEN}"
curl -s -m 10 "$KUMA_PUSH_URL?msg=ok&ping=" > /dev/null 2>&1 || true

# 4b. Secondary desktop copy: the CachyOS host pulls the retained stage into
# its own restic repo and pushes Kuma monitor 75 after a successful snapshot.
# The old master -> Windows SFTP leg and its 08:00 retry were retired 2026-11-12.

# 5. Cleanup
# stage kept on disk so the PC pulls the same snapshot later (see
# ~/.local/bin/homelab-backup on the desktop); next run clears it first.
rm -f /tmp/sealed-secrets-key.yaml /tmp/k3s-state.db

echo "[$DATE] === RESTIC BACKUP DONE ===" >> "$LOG"
