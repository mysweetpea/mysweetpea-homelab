#!/bin/bash
# NOTE: repo copy is sanitized — real repo URLs (VPS/PC SFTP) and the Kuma
# push token live on k3s-master at /root/backup-scripts/. Set VPS_REPO/PC_REPO/
# KUMA_PUSH_TOKEN env vars (or re-edit on the master). See
# apps/infra/longhorn/BACKUP-TARGET.md for the design.
# MySweetPea homelab backup script — restic to VPS (off-site) + PC (local)
# Backs up: sealed-secrets key, k3s state.db, pg-dumps, config-backup
set -euo pipefail

# single-instance guard (added 2026-09-16 after two racing runs corrupted staging)
exec 9>/run/restic-backup.lock
flock -n 9 || { echo "restic-backup.sh: another instance already running, exiting"; exit 0; }

export RESTIC_PASSWORD="$(cat /root/.restic-passphrase)"
VPS_REPO="${VPS_REPO:-}"
PC_REPO="${PC_REPO:-}"
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
STAGE="/tmp/restic-stage"
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
# 3j. Bazarr config + DB (config.yaml, bazarr.db — config PVC at /config)
mkdir -p "$STAGE/bazarr"
if kubectl exec -n private deploy/bazarr -- tar czf - -C /config --exclude=cache --exclude=log . > "$STAGE/bazarr/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] bazarr config captured ($(du -h "$STAGE/bazarr/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: bazarr config backup failed" >> "$LOG"
fi
# 3i. Prowlarr config + DB (config.xml, prowlarr.db — config PVC at /config)
mkdir -p "$STAGE/prowlarr"
if kubectl exec -n private deploy/prowlarr -- tar czf - -C /config --exclude=logs . > "$STAGE/prowlarr/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] prowlarr config captured ($(du -h "$STAGE/prowlarr/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: prowlarr config backup failed" >> "$LOG"
fi
# 3h. Radarr config + DB (config.xml, radarr.db — config PVC at /config)
mkdir -p "$STAGE/radarr"
if kubectl exec -n private deploy/radarr -- tar czf - -C /config --exclude=MediaCover --exclude=logs . > "$STAGE/radarr/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] radarr config captured ($(du -h "$STAGE/radarr/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: radarr config backup failed" >> "$LOG"
fi
# 3e. Sonarr config + DB (config.xml, sonarr.db — config PVC at /config)
mkdir -p "$STAGE/sonarr"
if kubectl exec -n private deploy/sonarr -- tar czf - -C /config --exclude=MediaCover --exclude=logs . > "$STAGE/sonarr/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] sonarr config captured ($(du -h "$STAGE/sonarr/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: sonarr config backup failed" >> "$LOG"
fi
# 3f. Seerr config + DB (settings.json, sqlite DB, patch.js — config PVC at /app/config)
mkdir -p "$STAGE/seerr"
if kubectl exec -n dmz deploy/seerr -- tar czf - -C /app/config --exclude=cache . > "$STAGE/seerr/config.tar.gz" 2>/dev/null; then
  echo "[$DATE] seerr config captured ($(du -h "$STAGE/seerr/config.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: seerr config backup failed" >> "$LOG"
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


# 3m. Immich library + uploads + internal DB backups (data PVC at /data; thumbs/encoded-video regenerable)
mkdir -p "$STAGE/immich"
if kubectl exec -n private deploy/immich-server -- tar czf - -C /data --exclude=thumbs --exclude=encoded-video . > "$STAGE/immich/library.tar.gz" 2>/dev/null; then
  echo "[$DATE] immich library captured ($(du -h "$STAGE/immich/library.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: immich library backup failed" >> "$LOG"
fi

# 3n. Open WebUI data (webui.db + uploads + vector_db; cache 1.3G regenerable — excluded)
mkdir -p "$STAGE/open-webui"
if kubectl exec -n private deploy/open-webui -- tar czf - -C /app/backend/data --exclude=cache . > "$STAGE/open-webui/data.tar.gz" 2>/dev/null; then
  echo "[$DATE] open-webui data captured ($(du -h "$STAGE/open-webui/data.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: open-webui backup failed" >> "$LOG"
fi

# 3o. Netdata parent: alarm log + config (metric DB 2.3G regenerable — excluded)
PARENT_POD=$(kubectl get pods -n monitoring -o name | grep netdata-parent | head -1 | cut -d/ -f2)
if [ -n "$PARENT_POD" ]; then
  kubectl exec -n monitoring "$PARENT_POD" -- tar czf - -C /var/lib netdata 2>/dev/null > $STAGE/netdata-alarms.tar.gz || true
  echo "netdata: alarms + config captured"
else
  echo "netdata: parent pod not found"
fi

# 3p. qBittorrent: config + resume data (media lives on media-storage; GeoDB/logs regenerable)
QB_POD=$(kubectl get pods -n private -l app.kubernetes.io/name=qbittorrent -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$QB_POD" ]; then
    kubectl exec -n private "$QB_POD" -- tar czf - -C /config --exclude='data/GeoDB' --exclude='data/logs' config data/BT_backup 2>/dev/null > "$STAGE/qbittorrent-config.tar.gz" && \
    echo "qbittorrent config: OK ($(du -h $STAGE/qbittorrent-config.tar.gz | cut -f1))" || echo "qbittorrent config: FAILED"
else
    echo "qbittorrent: pod not found, skipping"
fi

# 3r. Gotify data (gotify.db + images + plugins — data PVC at /app/data; tiny, ~120KB)
GOTIFY_POD=$(kubectl get pods -n private -l app.kubernetes.io/name=gotify -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$GOTIFY_POD" ]; then
    kubectl exec -n private "$GOTIFY_POD" -- tar czf - -C /app/data . 2>/dev/null > "$STAGE/gotify-data.tar.gz" &&     echo "gotify data: OK ($(du -h $STAGE/gotify-data.tar.gz | cut -f1))" || echo "gotify data: FAILED"
else
    echo "gotify: pod not found, skipping"
fi


# 3q. Nextcloud data (user files + DB-adjacent data dir; previews regenerable - excluded)
mkdir -p "$STAGE/nextcloud"
if kubectl exec -n private deploy/nextcloud -c nextcloud -- tar czf - -C /var/www/html/data --exclude="*/files_trashbin" --exclude="*/files_versions" . > "$STAGE/nextcloud/data.tar.gz" 2>/dev/null; then
  echo "[$DATE] nextcloud data captured ($(du -h "$STAGE/nextcloud/data.tar.gz" | cut -f1))" >> "$LOG"
else
  echo "[$DATE] WARNING: nextcloud backup failed" >> "$LOG"
fi

# 4a. Restic backup to VPS (primary, off-site)
# 3z. AIOStreams instance on the VPS + moonbase-lite data (added 2026-10-13)
VPS_SSH="${VPS_SSH:-ubuntu@<VPS_HOST>}"   # set VPS_SSH on the master
mkdir -p "$STAGE/aiostreams"
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_SSH" "sudo sh -c 'cd /opt/aiostreams && tar czf - compose.yaml .env data/db.sqlite*'" > "$STAGE/aiostreams/config.tar.gz" 2>>"$LOG" && [ -s "$STAGE/aiostreams/config.tar.gz" ]; then
  echo "[$DATE] AIOStreams VPS snapshot: $(du -h "$STAGE/aiostreams/config.tar.gz" | cut -f1)" >> "$LOG"
else
  echo "[$DATE] AIOStreams VPS snapshot FAILED - continuing" >> "$LOG"
fi
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VPS_SSH" 'sudo tar czf - -C /opt/moonbase-lite config.json data' > "$STAGE/moonbase-lite.tar.gz" 2>>"$LOG" && [ -s "$STAGE/moonbase-lite.tar.gz" ]; then
  echo "[$DATE] moonbase-lite snapshot: $(du -h "$STAGE/moonbase-lite.tar.gz" | cut -f1)" >> "$LOG"
else
  echo "[$DATE] moonbase-lite snapshot FAILED - continuing" >> "$LOG"
fi

export RESTIC_REPOSITORY="$VPS_REPO"
restic unlock >/dev/null 2>&1 || true   # clear stale locks; repo-unreachable must NOT kill the set -e script (fixed 2026-09-22)
if restic backup "$STAGE" --tag "$TAG" --exclude "*.log" >> "$LOG" 2>&1; then
  restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune >> "$LOG" 2>&1 || true
  echo "[$DATE] VPS backup OK" >> "$LOG"
else
  echo "[$DATE] VPS BACKUP FAILED" >> "$LOG"
  notify_fail "VPS restic push" "Nightly backup to VPS failed at $DATE (VPS down? network?)"
fi

# 4b. Restic backup to PC (secondary, local)
export RESTIC_REPOSITORY="$PC_REPO"
restic unlock >/dev/null 2>&1 || true   # clear stale locks; repo-unreachable must NOT kill the set -e script (fixed 2026-09-22)
if restic backup "$STAGE" --tag "$TAG" --exclude "*.log" >> "$LOG" 2>&1; then
  restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune >> "$LOG" 2>&1 || true
  echo "[$DATE] PC backup OK" >> "$LOG"
else
  echo "[$DATE] PC BACKUP FAILED" >> "$LOG"
  notify_fail "PC restic push" "Nightly backup to PC failed at $DATE (IP drift? PC asleep?)"
fi

# 6. Heartbeat to Uptime Kuma push monitor (alerts if the whole chain stops running)
KUMA_PUSH_URL="http://uptime-kuma.monitoring.svc.cluster.local:3001/api/push/${KUMA_PUSH_TOKEN}"
curl -s -m 10 "$KUMA_PUSH_URL?msg=ok&ping=" > /dev/null 2>&1 || true


# 5. Cleanup
rm -rf "$STAGE" /tmp/sealed-secrets-key.yaml /tmp/k3s-state.db

echo "[$DATE] === RESTIC BACKUP DONE ===" >> "$LOG"
