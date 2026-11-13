# Backup scripts (sanitized copies)

These scripts run on **k3s-master** under `/root/backup-scripts/`. The live
copies execute there; this directory is the durable, sanitized rebuild source.

| Script | Schedule | Purpose |
|---|---|---|
| `pg-backup.sh` | 02:00 daily | Dump all shared-PostgreSQL databases |
| `restic-backup.sh` | 02:30 daily | Stage core data and push to the VPS restic repo; retain 5 daily / 2 weekly / 2 monthly |
| `config-backup.sh` | 03:00 daily | Dump namespace configs and SealedSecrets |
| `restic-check.sh` | 1st monthly 05:00 | Check the VPS restic repo with a 5% data subset |
| `containerd-hygiene.sh` | Sun 04:00 | Prune exited containers and unused images on the master |
| `backup-freshness-watchdog.sh` | 06:30 daily | Alert on stale VPS/desktop/Longhorn backups and low backup-target space |
| `setup-cron.sh` | manual | Install the entries above while preserving unrelated cron jobs |

The desktop's `homelab-backup.timer` pulls the completed master stage at 04:00
and snapshots it into `/mnt/software/homelab backup/restic-repo` (7 daily / 4
weekly). It has its own Kuma heartbeat, so a healthy master cannot mask a failed
desktop pull. The old Windows PC push/retry leg is retired.

AIOStreams uses `scripts/vps-aiostreams/sqlite-backup.py`: SQLite's online
backup API, `integrity_check`, and an atomic rename. The master pages and stops
rather than marking a snapshot healthy if this capture fails.

## Sanitization

Public copies parameterize repo/SSH/Kuma endpoints (`VPS_REPO`, `VPS_SSH`, and
push URL). Real values stay on the master. Before redeploying, supply them from
a root-only environment source or install the live configuration deliberately;
do not commit credentials or push tokens.

The restic passphrase is read from `/root/.restic-passphrase`; database
credentials come from Kubernetes secrets at runtime.

## Related

- Longhorn offsite backups: `apps/infra/longhorn/BACKUP-TARGET.md`
- Desktop restic repository: `/mnt/software/homelab backup/restic-repo`
- AIOStreams/Moonbase helper scripts: `scripts/vps-aiostreams/` and
  `scripts/vps-moonbase-lite/`
