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


### PlaybackInfo browser filter (pi-filter sidecar) - added 2026-10-13
Browser clients (Chromium/Firefox-class UAs, incl. Moonfin Web) cannot direct-play HEVC
and AIOStreams never transcodes; the sidecar keeps only H.264 sources for browser UAs
(when any exist) and leaves native clients (Moonfin/Stremio/Dart UAs) byte-identical.
Filtered JSON is space-padded to the original byte length so Content-Length stays valid.
- nginx: `upstream aiostreams_pi` = 127.0.0.1:3001 primary + :3000 backup (fail-open via
  proxy_next_upstream); `location ~* /PlaybackInfo$` routes through it. See nginx-aiostreams-site.conf.
- Deploy: aiostreams-pi-filter.py -> /usr/local/bin/, aiostreams-pi-filter.service ->
  /etc/systemd/system/, `systemctl daemon-reload && systemctl enable --now aiostreams-pi-filter`.
- Test: `AIOS_UUID=<uuid> AIOS_PW=<pw> bash test-playback-filter.sh` (prints PASS/CHECK).
- Do NOT retry the njs js_body_filter route: holding response chunks deadlocks this
  nginx/njs build (r.ctx also unsupported). nginx backups must not live in sites-enabled.
- v2 (2026-10-13 late): two real-client findings baked in - (1) Moonfin sends
  mediaSourceId on EVERY PlaybackInfo call; never treat it as "user picked a version"
  (v1's guard made the filter a no-op for the actual client); (2) the version list that
  drives the client's default pick comes from the item-detail response, so item
  detail/list responses are routed through the sidecar too (3 nginx locations via
  snippets/aiostreams-pi-proxy.conf). Verified: browser PI+detail = 8 sources all H.264
  vs native 19, byte-identical padding. Probe note: the app rate-limits jellyfin logins
  per IP (~2 min window) - reuse one token and pace requests.
- v2.1 (2026-10-13): routes widened so EVERY browser play entry point is covered -
  all user-scoped Items/Shows responses + /Shows/ responses flow through the sidecar
  (continue-watching rows, show pages, lists, detail, PlaybackInfo). Classification is
  per-request by client UA: zero per-account or per-device config; any browser on any
  platform gets the best playable source first, native apps stay byte-identical.

## Genre taxonomy filter + year/language browse (2026-10-14)
`aiostreams-genre-filter.py` (this dir) runs as systemd unit `aiostreams-genre-filter`
on 127.0.0.1:3003, `upstream aiostreams_genre` = :3003 primary + :3000 backup
(fail-open twice; snippet `nginx-aiostreams-genre-proxy.snippet.conf`; vhost locations
`^/jellyfin/(?:[^/]+/){0,2}Genres$` and `.../Items/Filters2?$`). It cleans the browse
taxonomy Moonfin sees:
- drops the Trending catalogs' 'Day'/'Week' pseudo-genres (their catalog genre-extra
  is the trending WINDOW, not a genre);
- de-dupes genre entries by name (the same genre arrives 3x: movie / series /
  collections catalog copies) and sorts the root grid A-Z;
- ROOT grid only: drops the year + language entries owned by the browse libraries -
  resolved dynamically (cache 6h) by fetching `/Genres?ParentId=` for the views named
  'Movies by Year' etc. with the caller's own token, so it never hardcodes entry ids.
Root genres: 58 messy -> 27 clean. Per-library lists pass through intact (the year
grid keeps its 21 years). Selftest: `python3 .../aiostreams-genre-filter.py --selftest`.

Media-server browse catalogs (user-config, names via catalogModifications):
- Movies by Year / Shows by Year (`tmdb.year`; was disabled, re-enabled + renamed):
  genre grid = YEARS 2006-2026; picking one = a real TMDB discover query - verified
  "2019" -> Avengers: Endgame / Parasite / Joker / Toy Story 4. This is the
  browse-old-movies/shows mechanism (addon caps the year list at 21 back).
- Movies by Language / Shows by Language (`tmdb.language`): language grid per type
  (Korean etc.) - same discover mechanics.
- Star Wars: Chronological / Star Wars: Release Order (`star-wars-universe` preset,
  instance `swu`, wrapped ids `swue3b0.*`, type 'StarWars'; catalogs option = the 4
  sw-* values - Skywalker Saga + Live-Action Series register in the manifest but
  produce no browsable libraries). Marvel-style mods pattern: instanceId + 'e3b0.'
  is the generated wrapper prefix for ALL preset instances.
- DC Universe preset NOT added: its public addon (addon-dc-cq85.onrender.com) was
  503ing at add time - aios VALIDATES new preset manifests on save, so a dead host
  = USER_INVALID_CONFIG "Failed to fetch manifest". Retry when the host wakes;
  alt URL from its README (addon-dc.vercel.app) is a stale 404.
- Ops law: adding/removing catalogs or presets = PUT /api/v1/user (basic auth
  uuid:config-password), then views update in seconds; mods renames also drive the
  client's per-catalog "Recently Added X" row titles. Preset save-time manifest
  fetch means tapframe's onrender hosts may need a warm curl first.

## No-junk audit + accepted residuals (2026-10-14)
Full sweep of the aios-via-Moonfin surface: 40 views clean (no duplicate/placeholder
names, posters everywhere), root genres 27 clean on BOTH lanes (plain + persona mount
`/jellyfin/<uuid>/<encpw>/Genres` runs through the same filter), Filters 27, search +
item details complete, collection BoxSets drill to real members (Spider-Man MCU 4,
Insidious 6, Fall 3, Social Network 2), Year/Language/Star Wars content correct.
Two accepted residuals, documented so they don't get re-litigated:
1. Moonfin composes one "Recently Added <view name>" row per non-playlist/boxsets/livetv
   view, filtered only by `LatestItemsExcludes` from the user config. The aios hardcodes
   `LatestItemsExcludes`/`MyMediaExcludes` to [] and its POST /Users/{id}/Configuration
   whitelist persists playback prefs only, so view hiding never sticks -> the Year and
   Language browse libraries each get a redundant "Recently Added Movies by Year" style
   row. NOT suppressible server-side on v2.36. Cosmetic; leave it.
2. 'Marvel Movies'/'Marvel Series' (marvel preset) include historical relics (1944
   Captain America serial - TMDB typo "Captian America" - and the 1977 Hulk TV movie).
   Addon data; drop those two catalogs if it ever bothers.
