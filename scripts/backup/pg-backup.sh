#!/bin/bash
# PostgreSQL database backup script
# Runs daily via cron (02:00). Dumps all databases to /root/pg-dumps/,
# then the restic chain picks them up (restic-backup.sh) for offsite copies.
#
# Hardening (deep-audit 2026-09-15):
#   - A failed dump can no longer look like a successful run: each dump is
#     written to a temp file, checked for pg_dump's completion marker, then
#     atomically moved into place. Failures are counted and the script exits 1.
#   - The database list is fetched with error checking: if the listing fails
#     the script aborts instead of silently dumping zero databases.
#   - stderr is no longer discarded into /dev/null; diagnostics are kept.

set -u

DATE=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/pg-dumps"
mkdir -p "$BACKUP_DIR"

echo "[$(date)] Starting PostgreSQL backup..."

FAILED=0
DUMPED=0
MARKER="PostgreSQL database dump complete"

dump_db() {
  # $1 = label, $2 = namespace, $3 = pod, $4 = username, $5 = dbname, $6 = password
  local label="$1" ns="$2" pod="$3" user="$4" db="$5" pass="$6"
  local out="$BACKUP_DIR/${label}-${DATE}.sql"
  local tmp="${out}.tmp"

  if kubectl exec "$pod" -n "$ns" -- env PGPASSWORD="$pass" \
       pg_dump -U "$user" "$db" > "$tmp" 2>"${tmp}.err"; then
    if grep -q "$MARKER" "$tmp"; then
      rm -f "${tmp}.err"
      mv "$tmp" "$out"
      gzip -f "$out"
      echo "  ✓ $label"
      DUMPED=$((DUMPED + 1))
      return 0
    fi
    echo "  ✗ $label: dump truncated (no completion marker) — see ${tmp}.err"
  else
    echo "  ✗ $label: pg_dump failed — $(tail -1 "${tmp}.err" 2>/dev/null)"
  fi
  rm -f "$tmp" "${tmp}.err"
  FAILED=$((FAILED + 1))
  return 0
}

# ---- Get postgres admin password -------------------------------------------
PGPASS=$(kubectl get secret postgresql -n private -o jsonpath='{.data.postgres-password}' | base64 -d)
if [ -z "$PGPASS" ]; then
  echo "FATAL: could not read postgresql admin password (kubectl/RBAC failure) — aborting"
  exit 1
fi

# ---- List databases (with error checking) ----------------------------------
echo "[$(date)] Listing databases on shared postgresql..."
DBLIST=$(kubectl exec postgresql-0 -n private -- env PGPASSWORD="$PGPASS" \
         psql -U postgres -t -A -c \
         "SELECT datname FROM pg_database WHERE datistemplate = false;") || {
  echo "FATAL: could not list databases (kubectl/psql failure) — aborting"
  exit 1
}
if [ -z "$(echo "$DBLIST" | tr -d '[:space:]')" ]; then
  echo "FATAL: database list came back EMPTY — refusing to report a successful backup"
  exit 1
fi

# ---- Dump each database on the shared postgres instance --------------------
echo "[$(date)] Dumping shared postgresql databases..."
for db in $DBLIST; do
  dump_db "$db" private postgresql-0 postgres "$db" "$PGPASS"
done

# ---- Dump standalone instances ---------------------------------------------
echo "[$(date)] Dumping authentik database..."
AKPASS=$(kubectl get secret authentik-postgresql -n dmz -o jsonpath='{.data.password}' | base64 -d)
if [ -n "$AKPASS" ]; then
  dump_db authentik dmz authentik-postgresql-0 authentik authentik "$AKPASS"
else
  echo "  ✗ authentik: could not read password"; FAILED=$((FAILED + 1))
fi

echo "[$(date)] Dumping immich database..."
IMMICHPASS=$(kubectl get secret immich-postgresql -n private -o jsonpath='{.data.postgres-password}' | base64 -d)
if [ -n "$IMMICHPASS" ]; then
  dump_db immich private immich-postgresql-0 immich immich "$IMMICHPASS"
else
  echo "  ✗ immich: could not read password"; FAILED=$((FAILED + 1))
fi

echo "[$(date)] Dumping firecrawl queue database..."
FCPASS=$(kubectl get secret firecrawl-db -n firecrawl -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
if [ -n "$FCPASS" ]; then
  dump_db firecrawl-nuq firecrawl firecrawl-nuq-postgres-0 postgres postgres "$FCPASS"
else
  echo "  ✗ firecrawl: could not read password"; FAILED=$((FAILED + 1))
fi

echo "[$(date)] Dumping matrix-synapse database..."
SYNAPSEPASS=$(kubectl get secret matrix-synapse-db -n dmz -o jsonpath='{.data.password}' | base64 -d)
if [ -n "$SYNAPSEPASS" ]; then
  dump_db synapse dmz matrix-synapse-postgresql-0 synapse synapse "$SYNAPSEPASS"
else
  echo "  ✗ synapse: could not read password"; FAILED=$((FAILED + 1))
fi

# ---- Retain 7 days of dumps -------------------------------------------------
echo "[$(date)] Cleaning old dumps (keeping 7 days)..."
find "$BACKUP_DIR" -name "*.sql*" -mtime +7 -delete

# ---- Verdict -----------------------------------------------------------------
if [ "$FAILED" -gt 0 ]; then
  echo "[$(date)] BACKUP FINISHED WITH ERRORS: $DUMPED dumped, $FAILED FAILED"
  exit 1
fi
echo "[$(date)] PostgreSQL backup complete. $DUMPED database(s) dumped."
