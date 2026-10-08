# Decypharr — Debrid → STRM gateway

Decypharr v2.5 turns Real-Debrid into a **zero-storage library source** for the
Arr stack: Radarr/Sonarr see it as a qBittorrent client, it adds magnets to RD,
and writes tiny `.strm` files (URL pointers) that the arrs import into the
library. Jellyfin scans the `.strm` files as real items — real metadata, real
home rows, fast media bar — while bytes stream from RD on demand.

## Architecture

```
Seerr → Radarr/Sonarr → Decypharr (fake qBittorrent, port 8282)
  → magnet → Real-Debrid (instant cache)
  → .strm file → /data/decypharr-downloads/<category>/<release>/
  → arr imports .strm into /data/media/{movies,tv}
  → Jellyfin scans real files (NFO + poster.jpg from arr metadata consumer)
  → playback: Jellyfin → .strm URL → Decypharr WebDAV → RD CDN (Range requests)
```

- **No FUSE, no privileged pods** — `mount.type: none` (WebDAV only)
- **No RD IP-pinning** — Decypharr proxies all RD traffic server-side; strm
  URLs never expose RD CDN links
- **No URL rot** — links are fetched on demand and cached (24h expiry)
- **strm URLs** point at `http://decypharr.private.svc.cluster.local:8282/webdav/stream/__all__/<folder>/<file>`

## Key config (configmap.yaml + sealed secret)

| Setting | Value | Why |
|---|---|---|
| `default_download_action` | `strm` | Write URL pointers, not files |
| `mount.type` | `none` | WebDAV only — no FUSE |
| `app_url` | `http://decypharr.private.svc.cluster.local:8282` | Cluster DNS for strm URLs |
| `enable_webdav_auth` | `false` | strm URLs carry no token; internal network only |
| `workers` | `25` | Cap below the 50 default (issue #282 OOM) |
| `download_uncached` | `false` | Only grab instant-cache releases |
| `repair.enabled` | `false` | Community reports auto-repair deletes files |
| `queue_cleanup` | failed→blacklist_research etc. | Auto-resolve stuck queue items |

**Sealed secret keys** (`sealed-secrets/private/decypharr-secrets.yaml`):
`DECYPHARR_DEBRIDS__0__API_KEY`, `DECYPHARR_ARRS__0__TOKEN`,
`DECYPHARR_ARRS__1__TOKEN`, plus (phase 2 usenet, populated when the user
subscribes — per-provider, provider array materializes only when `HOST` is set):
`DECYPHARR_USENET__PROVIDERS__0__HOST`, `__0__PORT`, `__0__USERNAME`,
`__0__PASSWORD`, `__0__BACKBONE`, `__0__SSL`, `__0__MAX_CONNECTIONS`,
`__0__PRIORITY` (primary), and `__1__*` (backup block provider).

⚠️ **Env override gotcha (source-verified)**: Decypharr's env overrides need the
`DECYPHARR_` prefix AND a `NAME` trigger (`DECYPHARR_DEBRIDS__0__NAME` must be
set or the API_KEY override is skipped). Worse, `setDefaults()` runs **before**
`applyEnvOverrides()` and pre-computes `DownloadAPIKeys` from the (then-empty)
`APIKey` — so env-only keys leave the account manager with `[""]` → account
disabled → "No active accounts available". **Fix**: the init container injects
the key into `/app/config.json` via sed (file-based, survives the ordering).

⚠️ **Usenet providers validation gate (source-verified)**: `Validate()` requires
every configured usenet provider to have non-empty username AND password —
staging providers with empty creds trips the setup-wizard gate → **all API
endpoints return 503** ("usenet provider username is required... visit
/setup"). Keep `usenet.providers: []` in the configmap; usenet providers are
added via env vars (`DECYPHARR_USENET__PROVIDERS__N__*`), which only
materialize when `HOST` is set — so the sealed secret can hold empty values
until the user subscribes without breaking the API.

## Usenet (phase 2 — RD expiry Sep 17 2026)

Decypharr v2.5 has **direct NNTP streaming** built in — no SABnzbd container.
Arrs submit NZBs to the SABnzbd-compatible API at `/sabnzbd/api` on port 8282;
Decypharr parses the NZB, streams segments from the NNTP provider on demand
(STRM mode — zero storage).

Provider config is **env-var driven** (`DECYPHARR_USENET__PROVIDERS__N__*` in
the sealed secret — the provider array only materializes when `HOST` is set,
so empty secret values keep the API healthy). The configmap holds
`usenet.providers: []` + the tuning block.

**Recommended provider layout** (user's chosen purchase):
- **Primary**: Newshosting (Omicron backbone, EU+US, 6506+ day retention,
  ~$1.67/mo — DMCA-resistant, matches the resilience goal)
- **Backup block**: theCubeNet (UsenetExpress backbone, 2TB block $12 —
  different backbone, pay-once fallback; Decypharr skips same-backbone
  providers after 423/430 responses when `backbone` matches)
- Indexers: NinjaCentral + NZBGeek + NZBPlanet (all Newznab with proper
  categories → sync to arrs cleanly, unlike NZBIndex which has none)
- Decypharr usenet tuning: `read_ahead: 16MB` prefetch for smooth playback,
  `conn_idle_timeout: 5m` warm NNTP pool, `availability_sample_percent: 10`
  import gate, `disk_buffer_path: /cache/usenet` (emptyDir, 4Gi).

**SABnzbd API auth (v2.6 contract, fixed 2026-10-08)**: v2.6 rewrote compat-API
auth. With `use_auth: true` the SAB probe passes via **decypharr's own API token**
(`ma_password` = token from `/app/auth.json`, `ma_username` = anything) — the old
v2.5 scheme (`ma_username` = arr URL, `ma_password` = arr API key) only works when
the request carries a `category` matching a configured arr, and Radarr/Sonarr
probe without one → 401. The qBit API accepts the same token as the basic-auth
password. Verified live: both arr `Test` → 200, real grab → RD → strm symlinks.

**Download client config in arrs** (id 3 SAB + id 2 qbit in both; fixed 2026-10-08):
Sabnzbd: host `decypharr.private.svc.cluster.local`, port 8282, URL base `/sabnzbd`,
username `http://<arr FQDN>:<port>`, **password = decypharr api_token** (NOT the arr
key — v2.6 change), category `Sonarr`/`Radarr`, priority 2.
qBittorrent (RD client, prio 1): same host, username `http://<arr FQDN>:<port>`
(**exact FQDN** `radarr.private.svc.cluster.local` — v2.6 compares it to the
configured arr host), password = decypharr api_token.

## Arr wiring

- **Radarr/Sonarr → Settings → Download Clients**: qBittorrent, host
  `decypharr.private.svc.cluster.local`, port 8282, username
  `http://<arr>:<port>` (the arr's own URL), password = arr API key, category
  `radarr`/`sonarr`, Remove Completed = Yes.
- Old qBittorrent client **disabled** (firewalled, no longer used).
- **Metadata**: Kodi/Emby metadata consumer enabled in both arrs → writes
  `movie.nfo`/`episode.nfo` + `poster.jpg`/`fanart.jpg` next to every imported
  .strm. Jellyfin reads them locally (no per-item scraping).

## Known issues

- **RD 451 "infringing_file"** (issue #379): RD blocks some releases (e.g. YTS
  copies of Dune Part Two). Decypharr flattens this to a generic 400 → arr
  re-grabs a different release. Not a bug in our config; pick a different
  release or accept the re-grab loop.
- **RAR-packed RD torrents** (issue #365): virtual members start with RAR
  header → black screen. Avoid RAR releases.
- **ffprobe noise on .strm import**: expected; current arr builds skip probing
  streaming extensions (Sonarr PR #8730, Radarr #11544).

## Ops

- UI: `http://192.168.20.215:8282` (admin / password in auth.json — bcrypt
  hash in configmap; API token auto-generated on first run, stored in
  `/app/auth.json`)
- Config edits: the UI writes `/app/config.json` (emptyDir — survives restarts
  but NOT pod recreation; the init container re-seeds from the ConfigMap on
  every start, so **edit the ConfigMap + redeploy** for durable changes)
- Restart: `kubectl rollout restart deploy/decypharr -n private`
- Logs: `kubectl logs -n private deploy/decypharr -f`
