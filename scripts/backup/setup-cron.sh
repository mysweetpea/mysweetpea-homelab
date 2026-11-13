#!/bin/bash
# Install the master backup/hygiene cron entries without removing unrelated jobs.
set -euo pipefail

install_cron() {
  local name="$1" schedule="$2" log="${3:-}"
  local line="$schedule /root/backup-scripts/$name"
  if [ -n "$log" ]; then line="$line >> /var/log/$log 2>&1"; fi
  (crontab -l 2>/dev/null | grep -v "/root/backup-scripts/$name"; echo "$line") | crontab -
}

install_cron pg-backup.sh '0 2 * * *' pg-backup.log
install_cron config-backup.sh '0 3 * * *' config-backup.log
install_cron restic-backup.sh '30 2 * * *' restic-backup.log
install_cron restic-check.sh '0 5 1 * *'
install_cron containerd-hygiene.sh '0 4 * * 0' containerd-hygiene.log
install_cron backup-freshness-watchdog.sh '30 6 * * *' restic-backup.log

echo 'Backup cron jobs installed; unrelated entries preserved.'
