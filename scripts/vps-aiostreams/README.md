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

## Pre-warmer (media-server clients)
`aiostreams-warmer.py` (this dir) runs on the VPS from root cron every 3h
(`13 */3 * * *`, flock-guarded) and keeps the hot client surface warm:
per-view item rows, `/Items/Latest` rails, and batched `/Items?Ids=` metadata
(921 ids/run; ~6s warm, ~1-5 min after a full restart). Media cache TTLs are
6h (catalog/meta) / 24h (stream/pipeline), so clients never pay the cold
fetch (measured 3.3s -> 0.9s on rows, 2.6s -> 0.9s on 40-item meta batches).
- Creds: `/root/.aiostreams-warmer-creds.json` (chmod 600, config uuid+password)
- State: `/root/.aiostreams-warmer-state.json`  Log: `/var/log/aiostreams-warmer.log`
- Quirk baked in: `/Items/Latest` returns a bare JSON array; `/Items` returns `{Items:[...]}`.

## Media-server metadata pipeline (2026-10-13 fix)
Every item with an IMDb id needs an IMDb-capable META addon: the TMDB addon
only serves `tmdb:` ids, so without Cinemeta any streaming-catalog/search item
renders as `ttXXXX` with no episodes ("no supported addon found for meta
request"). Fix applied: Cinemeta custom preset (`https://v3-cinemeta.strem.io/manifest.json`,
resources `["meta"]`) added to the user config. Config edits: GET/PUT
`/api/v1/user` with basic auth `uuid:config-password` (`{"config":{...}}`;
presets entries need `type`+`instanceId`+`enabled`+`options`). Instance settings:
login `POST /api/v1/auth/login` -> cookie -> GET/PATCH `/api/v1/dashboard/settings`.
Server name is branded via `branding.addonName` ("MySweetPea").
