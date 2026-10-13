# AIOStreams on the VPS

Production AIOStreams instance for the Stremio front-end. Runs as a docker
container on the Oracle VPS (public streaming lane), independent of the
home Jellyfin stack. Deployed 2026-10-13; verified `https://aiostreams.mysweetpea.cc` 200.

## Layout on the VPS

- `/opt/aiostreams/compose.yaml` - this file
- `/opt/aiostreams/.env` (chmod 600, NOT in git) - `BASE_URL`, `AIOSTREAMS_AUTH`,
  `AIOSTREAMS_AUTH_PERMISSIONS` (`sweetpea=admin`, equals format), `SECRET_KEY`,
  cache TTLs (mirrored from the former cluster instance)
- `/opt/aiostreams/data` - app data volume
- `/etc/nginx/sites-enabled/aiostreams` - TLS vhost (certbot-managed)
- DNS: `aiostreams.mysweetpea.cc` A record -> VPS IP, **MUST stay grey (DNS-only)**:
  Cloudflare BFM challenges Stremio's HTTP clients.

## Ops

```bash
cd /opt/aiostreams
sudo docker compose ps                 # status (image has built-in healthcheck)
sudo docker logs --tail 50 aiostreams  # logs
sudo docker compose pull && sudo docker compose up -d   # update (manual by design)
```

Monitored by Uptime Kuma monitor "aiostreams (vps)" (60s interval, Gotify alerts).
Restore path: re-deploy from this compose + recreate `.env` + re-import the
user config `.json` via the configure page.
