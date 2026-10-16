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
`aiostreams-warmer.py` (this dir) runs on the VPS from root cron every 2h
(`13 */2 * * *`, flock-guarded) and keeps the hot client surface warm by
replaying the EXACT query shapes Moonfin + the apps send (ground truth from
client traffic): per-view `/Users/{uid}/Items/Latest` rows (Limit 15 + 60,
full Fields list), media-bar `recentlyReleased` windows + SortName count
probes over the 7 curated bar catalogs, Resume/NextUp/Upcoming + common home
rows, and batched `/Items?Ids=` metadata for detail pages (~1635 ids/run,
~24s warm). Catalog/meta TTLs are 6h, so no client ever pays a cold fetch.
The old basic warner (`/usr/local/bin/aios-warm.py` + cron.d `aios-warm`)
was retired 2026-10-14 (both renamed, superseded by this script).
- Creds: `/root/.aiostreams-warmer-creds.json` (chmod 600, config uuid+password)
- State: `/root/.aiostreams-warmer-state.json`  Log: `/var/log/aiostreams-warmer.log`
- Quirk baked in: `/Items/Latest` returns a bare JSON array; `/Items` returns `{Items:[...]}`.

## Item-image cache + HTTP/2 (2026-10-14)
Item images (posters/backdrops/logos/thumbs) are cached at nginx on a dedicated
zone (`aios_images`, 1g / 30d inactive — `nginx-aios-images-cache.zone.conf`)
via a regex location scoped to `/Items/{id}/Images/` in `nginx-aiostreams-site.conf`
(`nginx-aiostreams-images-cache.snippet.conf`). Images are public (no auth) and
immutable per `tag=` param, so the cache cannot serve stale bytes; the relay
upstream is hit once per image shape, then ~6ms HITs. Verified: MISS 33–148ms ->
HIT ~6ms, byte-identical, non-image paths unaffected. The vhost's 443 listeners
also gained HTTP/2 (multiplexes the clients' boot volley; was HTTP/1.1-only).

**Lane switch (2026-10-14, perf):** `$aios_pi_lane` map in the vhost routes
browser-class UAs (Mozilla/5.0 without Moonfin|Stremio|Dart|okhttp|Dalvik|Lavf —
mirrors `aiostreams-pi-filter.py`'s classifier exactly) through the H.264
filter; native clients go straight to :3000 (the filter passes them through
byte-identical anyway, at ~150–200ms/request of Python overhead). Measured:
native single request 272ms -> 64ms; filter CPU +0.00s during a native storm
(vs +0.02s for browsers). `X-Aios-Lane` header (debug) shows the chosen lane.

**Concurrency ceiling + rt logging (2026-10-14, perf):** the container's 1.5-core
CPU limit was CFS-throttling the client boot volley — 37 simultaneous
`Items/Latest` calls drained in ~10s server-side (matching the multi-second
stragglers visible in Moonfin's diagnostics). Raised to 2.0 cores live
(`docker update --cpus=2.0`) and persisted in `compose.yaml`: the same burst
now drains in ~1.1s (p90 884ms, nr_throttled=0). Also: the vhost now logs
`rt=`/`ut=` to `/var/log/nginx/aiostreams.access.log` (the default combined
format had no timings, which made this hunt slow — use it first for any
future latency report).

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
   view, filtered only by `LatestItemsExcludes` from the user config. The home-row half of
   this is now handled server-side by the exclude injection (below, 2026-10-14); the aios
   still never persists view hiding (`POST /Users/{id}/Configuration` whitelists playback
   prefs only), so Moonfin's own library-hiding toggle remains cosmetic. Leave it.
2. 'Marvel Movies'/'Marvel Series' (marvel preset) include historical relics (1944
   Captain America serial - TMDB typo "Captian America" - and the 1977 Hulk TV movie).
   Addon data; drop those two catalogs if it ever bothers.

## Response compression (2026-10-14, perf)
The app serves every JSON response uncompressed; the aios vhost now enables gzip at
the server level (`nginx-aiostreams-site.conf`). Types: JSON/JS/CSS/SVG/text only —
images, SSE (`text/event-stream`) and streams are excluded by type, so the image
cache and the `/Moonfin/*` long-poll paths are untouched. Measured on real row
shapes: `Items/Latest Limit=60` 50,873 -> 7,159 bytes (7.1x), `Limit=15`
26,353 -> 6,493 (4.1x), browser-UA lane identical; plain-request clients still get
uncompressed bytes (per-client negotiation; the only cache on this lane is the
image cache, which never matches gzip types). Biggest win is cellular clients.
`jellyfin_time` log_format in `nginx.conf` gained `ce=$sent_http_content_encoding
gz=$gzip_ratio` so client negotiation is visible in `/var/log/nginx/aiostreams.access.log`.
Backups: `/root/aiostreams.bak-gzip-20261014`, `/root/nginx.conf.bak-gzip-20261014`.
(The media.mysweetpea.cc lane already had per-location gzip blocks — untouched.)

## Media-bar micro-cache (2026-10-14, perf)
The media bar walks 7 catalog fetches per app launch (`/Users/*/Items?...MaxPremiereDate=<ts>`,
the only request shape carrying MaxPremiereDate). Those responses are catalog metadata only -
Moonfin's bar path reads NO user state (`MediaBarSlideItem` has no userData field, verified in
Moonfin-Core) and the body is identical across timestamps (verified) - so nginx caches them for
10 min: the key strips the per-request MaxPremiereDate (`$aios_args_nots` map), uid stays in the
path (per-user entries), requests without an Authorization header bypass the cache, and every
other `/Users/*/Items` call is BYPASS / never-stored (they carry live UserData). Upstream sends
`Cache-Control: no-store`, so the snippet ignores Set-Cookie/Cache-Control/Expires/Vary.
Measured: bar fetch 46ms -> 13ms HIT; Popular Shows (the chronic ~0.7s sort) 695ms -> 12ms;
rows/kids unchanged (BYPASS); no-auth = 401 upstream. Re-proven 2026-10-15: two fetches with different fresh MaxPremiereDate ts -> same entry HIT. NOTE: nginx locations match the URI only -
the query-string gate lives in maps, never in a location regex. Files:
`nginx-aios-api-cache.zone.conf` + `nginx-aiostreams-bar-cache.snippet.conf`.
Backup: `/root/aiostreams-site.bak-barcache{,-2}-20261014`.

## Items/Latest row micro-cache (2026-10-15, perf)
The 12 marquee "Recently Added <catalog>" rows were the last uncached class - and the one
real server cost per render: the aios Node app serves them effectively SERIALLY on its
single event loop (~60-80ms each; a 12-parallel burst ramps 0.32 -> 1.02s; a quiet isolated
fetch ~20ms), stretching to 0.8-1.7s each under a full render burst (measured from a real
client diagnostic + live thread-pool probes). New vhost location
`location ~* "/Users/[^/]+/Items/Latest"` (inserted BEFORE the generic
`/Users/[^/]+/(Items|Shows)` block - first regex match wins) includes the pi-proxy snippet
plus `nginx-aios-rows-cache.snippet.conf`: zone `aios_api` reused; key = the full URL
(`"$scheme$host$uri?$args"` - row args are stable, no ts params); TTL 120s; cache_lock on;
ignores Cache-Control/Set-Cookie/Expires/Vary (upstream sends `no-store`); no-auth bypass via
the shared `$aios_bar_bypass` map; `X-Aios-Cache` header for debugging. Every other shape in
the generic location still BYPASS / never-stored (they carry live UserData). TTL=120s is
deliberate: rows carry live UserData (progress badges) - short enough to be invisible, long
enough to absorb refresh-taps and second-device loads within a session; per-item detail/state
reads stay uncached (favourite-revert class). First cold render of the day is unchanged -
this kills repeats and shrinks the burst's event-loop occupancy.
Verified (2026-10-15): rows MISS -> HIT (`rt=0.000`, `x-aios-cache: HIT`, lane header intact);
children/SWY probes still BYPASS; nginx -t + reload clean; error.log silent since reload.
Backup: `/root/aiostreams.bak-rowcache-20261015`; rollback = restore it, drop the include +
snippet, reload.

## Latest-items exclude injection (2026-10-14, perf; re-aimed + fixed 22:15 UTC)
Moonfin's home fetches one `Items/Latest` per visible catalog view - the ~37-fetch boot
wave that everything else (including the media-bar walk) queues behind. The client
skips any view listed in `LatestItemsExcludes` (zero fetches, zero rows), which the
aios server hardcodes empty. `nginx-aiostreams-config-excludes.snippet.conf` patches
the response at the edge (sub_filter) with a 25-view exclude list, KEEPING twelve marquee
rows: Trending / Popular / Netflix / Hulu / Crunchyroll / Disney+ (movies + shows each —
see the add-back note at the end of this section).
Libraries stay browsable; the media-bar pool, collections, genres and all other
sections are untouched. Boot row wave: ~37 fetches -> ~12 (each kept row ~0.2s).

TWO traps cost two failed builds - do not repeat:
1. The config source is GET /Users/Me, NOT /Users/{id}/Configuration. Moonfin's
   JellyfinUsersApi.getUserConfiguration() reads `data['Configuration']` from /Users/Me
   (code-verified in Moonfin-Core); the first build's /Users/{id}/Configuration location
   never matched a client request and was inert. The location now matches
   `~* "/Users/Me$"` (mount + bare lanes both hit it).
2. `sub_filter_types application/json` silently never matches: aios sends
   `application/json; charset=utf-8` and nginx does an exact hash lookup. Use
   `sub_filter_types *;`.

Verified (2026-10-14): /Users/Me through nginx 2249 -> 3363 bytes, `LatestItemsExcludes`
n=31 on bare AND mount lane (JSON-parse clean), MyMediaExcludes untouched [],
gzip lane decodes to the patched body, unauth = 401, Items/bar micro-cache unaffected.
Adjust kept rows via the single sub_filter line + reload; revert = remove the /Users/Me
location from the vhost. Backup: /root/aiostreams-site.bak-usersme-20261014.

Update (2026-10-14 late, owner request - add rows back + concise names):
- Excludes 31 -> 25 (kept 12 rows: + Hulu, Crunchyroll, Disney+ movies+shows). Wave ~6 -> ~12.
- Movie-row catalog names: briefly shortened to bare names (Netflix Movies -> Netflix etc.),
  then REVERTED by owner the same night - final naming stays `X Movies` / `X Shows`.
  Row titles render as "Recently Added " + view name (`l10n.latestLibraryName`, client-side;
  the prefix is NOT server-changeable - the view name is the only lever).
- TRAP: `catalogModifications` edits propagate to /UserViews ONLY after
  `sudo docker restart aiostreams` - the media-server views path caches the resolved catalog
  list in-process; the Stremio manifest endpoint rebuilds fresh and shows new names instantly
  (NOT a valid signal for /UserViews). Restart is cheap: stream caches live in the sqlite
  `cache` table and survive.
- TRAP: the sub_filter line contains the empty-array SEARCH pattern (`"LatestItemsExcludes":[]`)
  on its LEFT side - when editing the id list, parse the REPLACEMENT (right-hand quoted segment).
- Verified: /UserViews serves new names, excludes n=25 live, Hulu/Crunchyroll/Disney+ Latest
  lists return items (12 each).

## Avatar for bare-path clients (2026-10-15)
- 2.5.x builds construct user-image URLs WITHOUT the mount prefix
  (`/jellyfin/Users/<id>/Images/Primary`); AIOStreams cannot route those -> was 8x404/day.
  nginx now serves `/var/www/aiostreams/avatar.png` for that exact shape
  (`root /var/www/aiostreams; try_files /avatar.png =404;` + 1d cache). Mounted
  user-image requests are untouched (AIOStreams answers those 200 itself).


## Artwork placeholder armor (2026-10-15)

Kills the "all artwork dies mid-session" class on Moonfin builds older than the
Oct 7 fix (tag 2.7.0 = Oct 5; fix = commit a40ef7ba, upstream issue #1766).

- Mechanism: the artwork HttpClient keeps `maxConnectionsPerHost = 4`; unread
  404 bodies hold connections. FOUR artwork 404s exhausted all four slots and
  every later image died inside dart:io's queue. Log signature: `art in flight:
  156`, `headerTimeout` at exactly 20.005s, and ZERO image requests in nginx.
- The four 404s were items with no poster (bogus `w500null` Primary tags) whose
  relayed TMDB 404 page (1957B HTML) even POISONED the image cache: TMDB's
  cache headers made nginx cache the relayed 404, and cache hits skip
  error_page. Layer 1 below closes that.
- Fix layers (all in this dir's nginx files):
  1. `proxy_ignore_headers Cache-Control Expires X-Accel-Expires;` in the
     images cache snippet -> only 200s may cache (`proxy_cache_valid 200 30d`).
  2. Armor: `proxy_intercept_errors on; error_page 404 500 502 503 504 =200
     /artwork-missing.png;` -> clients never see 4xx/5xx for item artwork.
  3. `location = /artwork-missing.png` serving a 68B transparent 1x1
     (`/var/www/aiostreams/artwork-missing.png`, 1d client cache).
  4. One-time purge of poisoned entries:
     `find /var/cache/nginx/aios-images -type f -delete`
     NOTE path is `aios-images` (hyphen); zone name is `aios_images` (underscore).
- Real client-side fix: rebuild Moonfin at >= a40ef7ba (any build since Oct 7;
  HEAD as of Oct 14 includes it).
- Verified 2026-10-15: all four formerly-404 items -> 200/68B PNG; repeats
  stay un-poisoned (no cache entry for 404s); normal images MISS then HIT.
- Backups: /root/aios-images-snippet.bak-artworkarmor-20261015,
  /root/aiostreams.bak-artworkarmor-20261015.


## TMDB Search addon (tmdb-search.py) — added 2026-10-15

**Why:** searches waited on the TMDB addon's per-result enrichment: 0.7–12s typical, a
30s stall class (the "quince" incident). TMDB's own API answers the same queries in
0.04–0.12s. This addon is a ~120-line stdlib service querying TMDB directly, returning
Stremio-catalog previews with the SAME `tmdb:<id>` ids the library uses (zero
watch-state split; details/streams pipelines unchanged).

**Measured:** cold search 5–30s → 0.10–0.21s; repeats 0.04–0.07s. (2026-10-15:
zero-result floor 3.77s→0.10s; salak 33 items = 0.13s; old path: mulberry 10.6s,
huckleberry 13.9s, quince 30s stall.)

**Architecture / deployment:**
- Runs as a SECOND SERVICE in /opt/aiostreams/compose.yaml (`tmdb-search`,
  python:3.12-alpine, `command: python /app/aiostreams-tmdb-search.py`) — NOT systemd,
  because the aios container reaches it as a compose-network sibling via DNS
  (`http://tmdb-search:3010`). Host-sidecar setups CANNOT work here: this VPS's Oracle
  iptables REJECTs container→host connections (INPUT rule 10), and the aios rewrites
  any URL on its own BASE_URL origin to loopback BEFORE fetching (rewriteRequestUrl,
  core/src/utils/http.ts) — same-origin addon URLs are unreachable by design. A
  distinct origin (sibling DNS) dodges both.
- Files: script /opt/aiostreams/tmdb-search.py (mounted ro into the container);
  TMDB key = /opt/moonbase-lite/tmdb.key (mounted ro); publishes 127.0.0.1:3010 for
  the smoke suite + public debug lane.
- nginx: `location ^~ /searchaddon/ -> 127.0.0.1:3010` on the aiostreams vhost
  (debug/verification lane; the aios itself never uses it).
- aios config: preset `tsf` {name: TMDB Search, manifestUrl:
  http://tmdb-search:3010/manifest.json, resources: [catalog], timeout: 4000}.

**disableSearch TRAP:** `disableSearch: true` is a NO-OP when the catalog's search
extra is `isRequired: true` (setup.ts:645 `canDisableSearch`). Search-only catalogs
can only be removed from the search fan-out with **`enabled: false`**. Currently
disabled via enabled:false: `f87e3b0.tmdb.search` ×2 + `tcle3b0.ctmdb.search` —
search fan-out is now ONLY the tsf addon (verified: response item counts match the
addon's returns exactly).

**Ops:**
- Smoke: `bash test-tmdb-search.sh` (13 asserts: search + 21 deep catalogs + skip paging, hits 127.0.0.1:3010).
- Addon logs: `sudo docker logs tmdb-search`.
- Restart: `cd /opt/aiostreams && sudo -S -p '' docker compose up -d --force-recreate tmdb-search`.
- Rollback to old search: enabled:true + remove disableSearch on those three mods
  via the config API, restart aiostreams.
- Config API: GET/PUT https://aiostreams.mysweetpea.cc/api/v1/user, Basic auth =
  config_uuid:config_password from /opt/moonbase-lite/config.json.
- Backups: /opt/aiostreams/{.env,compose.yaml}.bak-*, /root/aiostreams.bak-tmdbsearch-20261015.
- After ANY API-side config edit: hard-refresh any open configure tab (stale tabs
  revert changes on save).

## 2026-10-15 — pi-lane response buffers + fleet rows update
- snippets/aiostreams-pi-proxy.conf: added `proxy_buffer_size 16k; proxy_buffers 8 16k; proxy_busy_buffers_size 32k;` — fixes intermittent 502 ("upstream sent too big header") on large `/Items?Ids=` batch calls.

## 2026-10-15 — deep service catalogs (tmdb-search v1.1.0) + BoxSet-children micro-cache
- tmdb-search.py v1.1.0: +21 deep per-service catalogs backed by TMDB `discover`
  (popularity sort, watch-provider filter, Stremio `skip` paging → ~1000+ titles each).
  Provider facts verified live: Paramount+ = **2303** (531 = legacy junk, 4 results),
  Prime = **9** (119 is re-keyed/empty in the US region), Canal+ needs
  `watch_region=FR` (US → 0 results). Single-segment URL form
  `/catalog/<type>/<id>.json` carries `.json` on the id segment — strip before lookup.
- Fleet wiring: the 21 old streamer catalogs (`75de3b0.nfx/amp/dnp/hbm/hlu/pmp/pcp/atp/cru/dpe/cpd`)
  were swapped in place via catalogModifications → `tsfe3b0.<svc>-movies|-shows` (enabled),
  old entries appended `enabled:false` (un-referenced catalogs default-enable — they MUST
  stay referenced as disabled). Net: same names + display order, deeper content.
  `mediaBarLibraryIds` Netflix-Movies id updated in defaults.json + user file +
  seed-defaults.py. Rollback = flip the pairs back (enable old ids, disable tsfe ids).
- BoxSet-children micro-cache: `conf.d/aios-api-cache.conf` gains `$aios_is_child`
  (ParentId=`b…` + no IncludeItemTypes) + `$aios_no_meta`; bar-cache snippet bypass now
  uses `$aios_no_meta` and TTL is 120s for both shapes (children carry live UserData —
  same tradeoff as the row cache; the bar shares the location and inherits the fresher TTL).
- Similar+ (the /Items/<id>/Similar expansion from 20 → 60) is moonbase-lite side — see
  `scripts/vps-moonbase-lite/README.md`; its nginx fail-open location lives in
  `nginx-aiostreams-site.conf`.

## 2026-10-15 late: library depth cap
- `.env` += `JELLYFIN_MAX_CATALOG_ITEMS=1000` (upstream default 250 caps every catalog-backed library walk; per-request Limit clamp = min(500, max(100, V)); walk guard <=60 pages/request; 0 disables).
- Env change needs a container recreate: `cd /opt/aiostreams && docker compose up -d`.
- Verified: Prime Movies deep pages served to exactly 1000 items (start=240 -> 60 items; start=995 -> 5 items, total 1000; ~1s deep fetch warm).

### 2026-10-15 warmer: detail-page prewarm
The warmer now also pre-fetches the per-item DETAIL shape (Items/{id}?Fields=Chapters,MediaSources,...) for each user's NextUp + Resume items (cap 20/run). First open of a likely-next item: ~2s cold -> ~10ms warm.

### 2026-10-15 late — cache TTL alignment (cold boot waves fixed)
Symptom: after idle, the home boot wave (~24 `/Items/Latest` rows + ~14 bar fetches + ~50 BoxSet
children) hit the single-threaded app all at once -> 3-15s tail (last row 14.7s, bar catalog #2 14.6s
measured); media bar's serial client-side walk is worst-hit.
Root causes: edge TTLs were 120s while the warmer ran every 2h (everything expired between runs);
the warmer warmed bar shapes as `IncludeItemTypes=Movie,Series` but the client fetches `Movie` and
`Series` SEPARATELY (bar was never actually edge-cached); BoxSet children were never warmed; zone
`inactive=30m` < warm gap so even fresh entries could be swept.
Fix: warmer cron `13 */2` -> `13 *` (hourly); rows/bar/children `proxy_cache_valid 120s -> 10800s`;
`proxy_cache_path inactive=30m -> 4h`; warmer warms per-type bar shapes + all BoxSet children
(`children prewarm ok=50`). LAW (CORRECTED 2026-10-15 v3 - original claim was wrong; see v3 section at EOF): edge TTL alone does not keep entries fresh - an nginx cache HIT does NOT extend an entry's TTL (rows/children carry UserData; browse-row staleness
<=1h accepted, the live CW/NextUp rows are uncached anyway). Verified: cold MISS 45-1233ms ->
post-warm HIT 11-15ms on every shape; warmer run ~195s (was ~100s).
Backups on VPS: `*.bak-hotrows-20261015` + `/root/crontab.bak-hotrows-20261015`.

### 2026-10-15 late — cache-key token normalization (the real "still slow" root cause)
Each device/persona mints its OWN mount token (encpw) for the same config: same content, different
URL. Cache keys included it, so warmers/other devices filled caches nobody read (client boots kept
showing cs=EXPIRED/MISS). FIX: `map $uri $aios_key_uri` in aios-api-cache.zone.conf strips the encpw
segment from cache keys (uuid kept); rows/bar/images snippets key on it. The warmer additionally now
calls via a MOUNT path (`/<uuid>/<encpw>` read from .warm-encpw) — it used to call bare
`/jellyfin/Users/...`, which normalized to yet another key space. VERIFY ALWAYS WITH THE CLIENT'S
REAL TOKEN (most-frequent mount token in /var/log/nginx/aiostreams.access.log) - never the warmer's
or a fresh login's; same-token checks once "verified" a phantom cache.
`test-cache-hit-client-token.py` does exactly that. E2E: rows/bar/children/images all HIT ~12ms
under the client token; cross-token store/read proven in both directions.

### 2026-10-15 v2: cross-lane canonical keys + uid-less rows + dynamic per-user warmer
Two gaps still made certain devices/platforms slow even after token normalization:
1. **Bare lane**: older builds (the owner's iOS 2.5.x) stored the bare server URL
   `.../jellyfin` - requests without a `<uuid>/<encpw>` prefix normalized to a DIFFERENT key
   space and never matched warmed entries (rows cs=MISS/-, ~412 uid-less `/Items/Latest`
   calls/day). `$aios_key_uri` now canonicalizes BOTH lanes to `/jellyfin/<config-uuid>/<rest>`.
2. **UID-less row shape**: 2.5.x fetches rows as `/Items/Latest?ParentId=...` (no
   `/Users/<uid>/` segment); no location matched it, so it was never cached (cs=-). New
   location `~* "/Items/Latest"` gives it the rows cache; the warmer warms that shape too
   (owner only).

Warmer v3 (dynamic users): users discovered from the media server's own `/Users` list every
run (owner + personas as they are provisioned - nothing to configure), plus per-user PINs from
`/opt/moonbase-lite/data/pins.json` (written by the moonbase-lite provision API; see that
README). LAW: the media server scopes UserData by TOKEN, not by the uid in the path - warming a
persona's paths with the owner's token bakes the OWNER's progress into the personas entries
(proved: persona path + owner token returned the owner's 39s resume position). Each persona is
warmed with ITS OWN token (login with its stored PIN); personas without a stored pin are skipped
with a log line. Children prewarm now hydrates ALL BoxSets (83 today) in BOTH client shapes
(`Recursive=true&Fields=UserData` + `Fields=BasicSyncInfo`), not the first 50. Meta `/Items?Ids=`
batches stay owner-token (uncached; primes the apps internal meta cache for the owner).
Verified: cached bytes == fresh compute for both users; per-shape HITs with each users own token;
bare/mount lanes share entries; one-off `too big header` 502 on a 150-id meta batch (Oct 15
16:08Z) not reproducible (same batch replays 200, ~680B headers) - lane has 16k header buffers.
Backups on VPS: `aios-api-cache.conf.bak-keyv2-20261015`, `/root/aiostreams-site.bak-uidless-20261015`,
`/root/aiostreams-warmer.py.bak-v1-20261015` / `.bak-v2-20261015`.

### 2026-10-15 late — playback failover depth 12 (the "[AIOStreams] Failed to Download" card)
User report chain: "AIOStreams error download during playback" -> the app's notice card
(`/static/download_failed.mp4`, 307-redirected from the stream URL when a release fails usenet
availability verification) -> "still got the same error" -> deep audit.
Root cause: the failover chain = clicked + same-release variants + the NEXT N items of the ranked
list. Old shows front-load the SAME dead families on every episode (Adventure Time: EPSiLON/iVy
2010-era posts — gone or RAR-corrupt), and the chain depth was 4-6 — it exhausted INSIDE the dead
block, right before the live mid-band (Smad/PoF/DEiMOS sit at slots ~5-8).
Not a regression: zero engine lines for the show before the first card (the card had never been
served before), provider streaming fine — new content + shallow rescue, not a broken system.
FIX: `.env` += `MAX_FAILOVER_ATTEMPTS=12` (container recreate needed) + user config
`failover.maxAttempts: 12` (PUT /api/v1/user, readback verified). Chain = 13 slots.
E2E: replaying the user's exact clicks (S01E01 Slumber Party Panic + S01E04 Tree Trunks):
307 -> /api/v1/usenet/stream in <1s, real MKV bytes; member path proven with a throwaway persona
(same list, same resolve + bytes; persona removed after). Timing: <1s when dead candidates are
verdict-cached, ~5-10s on a first-ever click of a graveyard episode.
Self-heal context: dead verdicts (release blocklist) drop releases from FRESH list builds — Tree
Trunks' list went 25 -> 11 versions after verdicts + restart; the list cache is 24h
(`STREAM_CACHE_TTL`/`PIPELINE_CACHE_TTL=86400`), so failover carries the lag.
Backup on VPS: `.env.bak-deepfailover-20261015`.


### 2026-10-15 v3 - warmer bypass-read (the "went back to uncached" fix)
Symptom: a client boot 34 min after a successful warmer run saw ~25 row fetches as `cs=EXPIRED`
(0.3s-14.6s each; two 60-item walks at 12.3s/14.6s) - "went back to being uncached".
Root cause: a cache HIT does NOT extend an entry's TTL. Rows stored 20:38-21:11 by a client boot
expired at 23:38 (3h TTL); the 22:13/23:13 warmer runs logged `cs=HIT rt=0.000` (no re-store);
the next client boot at 23:47 fell into the expiry window and re-fetched everything live.
FIX: the warmer now sends `X-Aios-Warm: 1` on every request (warmer `call()` helper) and the rows +
bar snippets add `proxy_cache_bypass ... $aios_warm` (map in conf.d/aios-api-cache.conf). Bypass
READS, still STORES - every hourly warmer run re-stores fresh entries; expiry windows gone.
Verified: warmer run all-BYPASS (362x, 0 HIT); entries re-stored 23:51-23:52; replaying a
warmer-warmed row as a client request = `X-Aios-Cache: HIT`, 0.004s.
Backups: *.bak-bypasswarm-20261015 on the VPS.

## 2026-10-16 — discovery families + search intents + daily row rotation (Phase 1)

**tmdb-search.py v1.2.0**: +44 discover-backed catalogs (sub-genre / mood / curated):
- Anime hub + 10 sub-genre pairs (Action & Adventure, Romance, Comedy, Fantasy, Sci-Fi,
  Isekai, Slice of Life, Sports, Horror, Mecha — keyword ids verified live: anime=210024,
  isekai=237451, mecha=10046, slice-of-life=9914, sport=6075, horror=315058).
- Moods: Feel-Good, Dark & Gritty, Mind-Bending, Cozy, Epic.
- Curated: Critically Acclaimed, Hidden Gems, **Best of All Time** (the planned "Award
  Winners" keyword — TMDB award keywords have only ~1-3 items of coverage; vote_average.desc
  + vote_count floors is the real data), Cult Classics.
- Specials: True Crime pair, K-Dramas (series), Korean Movies.
- Search intents: category queries resolve via TMDB discover instead of literal title
  matches ("anime", "adventure anime", "k-drama", "true crime", "feel good", plain genres,
  "<genre> movies/shows" suffixes, "romantic comedy", "science fiction"...). Verified on the
  public lane: "anime" 10 junk -> Spirited Away/Doraemon/Bleach/Demon Slayer; title search
  unchanged ("the matrix" still returns The Matrix). Selftest: `python3 tmdb-search.py --selftest`.

**Fleet wiring** (config API, 2026-10-16): +44 catalogModifications entries (explicit
enabled:true; anime block after Trending/Popular, moods/curated/specials after the streamers,
before Year/Language). Views 40 -> 86. `.env` `JELLYFIN_MAX_LIBRARIES=40 -> 150` (views were
at the cap). Container recreate required for env.

**Daily rotation** (`moonfin-rotate.py`, root cron `0 7 * * *`, flock):
Writes `nginx-aiostreams-config-excludes.snippet.conf` from the LIVE /UserViews each run:
stable core (14 views: Trending/Popular/Netflix/Disney+/Hulu/Crunchyroll + Anime hub
movies+shows) stays visible; each day a date-seeded set rotates in — 2 anime sub-genre pairs
+ 1 mood pair + 1 curated pair + 1 special pair (10 views). Everything else is excluded
(62 of 86 today). nginx -t gates the reload; a bad snippet self-restores from `.bak-rotate`.
State (incl. `visible_names`) at `/root/.moonfin-rotate-state.json`; log
`/var/log/moonfin-rotate.log`. After a successful rotation the script triggers the warmer
(flock) so the new rows are warm. ⚠️ The snippet is now GENERATED — edit `moonfin-rotate.py`,
not the snippet. Verified E2E: /Users/Me LatestItemsExcludes=62 (independent fresh-login
check), feature views serve real content (Best of All Time -> Breaking Bad/Avatar/One Piece;
Anime: Fantasy -> Spirited Away...). NOTE: a draining nginx worker can serve the pre-reload
body for a few seconds — the script sleeps 5s before its own verify.

**Warmer v4** (rotation-aware): warms ONLY the views the home actually shows
(`visible_names` from the rotation state; falls back to all views when the state file is
missing), `VIEW_LIMIT 60 -> 120` as the fallback cap. Run dropped 371s/60 views ->
50s/24 views.

**Ops traps re-confirmed**: catalogModifications -> /UserViews needs a container restart
(Oct 14 trap); config PUT body `{"config": userData}`; hard-refresh any open configure tab
after API-side edits (stale tab save reverts).

**Rotation v2 (2026-10-16 evening) — mixed daily sets**: the daily feature set is now a
deliberate Netflix-style mixture — 1 anime sub-genre pair + 2 cross-genre combo pairs +
1 mood pair + 1 curated pair + 1 wildcard pair (True Crime / Korean / 80s / 90s / 2000s).
tmdb-search v1.3.0 (+26 catalogs): 10 combo pairs (Romantic Comedy, Action Comedy,
Crime Drama, Sci-Fi Thriller, Psychological Thriller, Fantasy Adventure, Family Adventure,
Horror Thriller, Dark Comedy, Superhero) + 3 decade pairs. Views 86 -> 112; mods 140;
visible 26 (14 core + 12 features); excludes 86/112. Params verified live: romcom TV =
genre 35 + romance kw 9840 (TV romance genre alone ~6 titles); sci-fi thriller TV =
10765 + thriller kw 316362; horror TV = 9648 + horror kw 315058; superhero kw 9715;
dark comedy kw 10123; psych thriller kw 12565. Search intents extended to match
(psychological thriller / dark+black comedy / superhero / crime+action comedy /
sci-fi thriller / "80s|90s|2000s").

## 2026-10-16 — Home redesign: personal rows + hub + taste (Phase 1.5)

Layout is now owned by the server in three places; devices adopt at boot and
their own homeSections pushes are DROPPED shim-side (moonbase_lite.py merges
now filter `homeSections`), so the fleet list is authoritative.

1. **Base layout** — `data/defaults.json` homeSections (16 rows):
   resume, nextup, syw1-5, rewatch, favoritemovies, **favoriteseries**,
   favoriteepisodes, playlists, genres, latestmedia, collections,
   **smalllibrarytiles** ("My Media" hub row). PINS no longer carries
   homeSections; genre-row sort is pinned fleet-wide:
   `genresRowSortBy=communityRating, genresRowSortOrder=descending,
   genresRowItemFilter=both` (client default was alphabetical — useless).
2. **Taste rows** — `moonfin-taste.py` (root cron `10 7 * * *`): per-user
   top-2 genres from resume+played items -> pluginDynamic genre rows
   ("<Genre> for You") spliced after 'playlists' in the user's file.
   Owner-only until personas exist (aios scopes user data BY TOKEN; personas
   need pins.json entries + their own login).
3. **New Releases** — tmdb-search v1.4.0 adds a rolling 8-week
   release-window pair (dates computed per request); registered as mods
   `tsfe3b0.new-releases-movies/-shows` at the FRONT of the catalog array.
4. **Rotation CORE** — streamer + Popular pairs REMOVED from home
   (netflix/hulu/crunchyroll/disney/popular; still browsable via the hub).
   CORE = Trending m/s, New Releases m/s, Anime m/s; features unchanged
   (1 anime + 2 combos + 1 mood + 1 curated + 1 wildcard). visible 18/114.
5. **Most-Played row — parked**: aios supports neither playlist nor
   collection creation (POST /Playlists + /Collections -> 404), and household
   play counts are still ~zero. Future option: intercept /Playlists* at nginx
   and serve synthetic playlists from the shim (custom-titled arbitrary rows).

## 2026-10-16 (v3) — Playlist rows engine: sparse home, mixed rows, no repeats

Home content rows are now **synthetic playlists** (the "engine"), served by the
moonbase-lite shim — no more per-catalog "Recently Added X" rows (latestmedia
is disabled fleet-wide).

- **nginx**: `nginx-playlists-route.snippet.conf` routes
  `/jellyfin[/<uuid>/<tok>]/Playlists...` to the shim (bare + mount lanes).
- **shim** (`moonbase_lite.py`): serves `/Playlists` (list) and
  `/Playlists/{id}/Items` from `data/rows.json`; forwards the caller's token to
  aios `/Items?Ids=` so UserData is per-user; items returned in playlist order.
- **generator** (`moonfin-rotate.py`, rewritten; root cron `0 7 * * *`): rebuilds
  `data/rows.json` + the defaults.json layout daily. Rows are movie+series
  MIXED, GLOBALLY deduped (a title appears in at most one row per home), 30
  items each, min 8. Slots: Trending Now, New Releases, [Most Played when data
  exists], 1 anime + 2 combos + 1 mood + 1 curated + 1 wildcard (rotating,
  date-seeded, slight per-day content drift). Also retires the old
  LatestItemsExcludes snippet (comment-only).
- **taste job** (`moonfin-taste.py`): taste rows splice after
  `favoriteepisodes`, before the engine rows.
- **layout (defaults.json)**: smalllibrarytiles (My Media hub) FIRST → personal
  → taste → engine rows → genres → collections; latestmedia explicit-disabled.
- **warmer** (`aiostreams-warmer.py`): warms `/Playlists/{id}/Items` per user
  instead of per-view `/Items/Latest`.
- **mods order**: streaming-service catalogs moved to the FRONT of
  catalogModifications (Netflix → Netflix Top 10 → Prime → Disney+ → HBO Max →
  Hulu → Paramount+ → Peacock → Apple TV+ → Crunchyroll → Discovery+ → Canal+ →
  everything else), so the My Media hub + Libraries show services first.

### 2026-10-16 (v3.1) — Playlist-row edge cache (per-device)

Every playlist row was fetched TWICE per boot (first paint + the settings-apply
reload) and cold again on each relaunch; rows took 0.4-2.8s each under the boot
wave. The `/Playlists/{id}/Items` location now edge-caches at nginx (zone
`aios_api`, see `nginx-playlists-route.snippet.conf`):

- **Key** = canonical URI + args + `$http_authorization`. This path has NO
  per-user uid (unlike rows/bar/children), so the auth header is the per-user
  discriminator: entries are per-DEVICE and never shared across members (no
  UserData bleed). Verified: same token MISS->HIT (~15ms); different token
  MISSes its own entry, then HITs.
- **TTL** 1h. Warmer runs (`X-Aios-Warm: 1`) skip read AND store.
- **`proxy_buffering off` blocks cache stores** - this location was created
  with buffering off (SSE habit); stores only began after flipping it ON.
  Keep `proxy_buffering on` here.


### 2026-10-16 (v4) — mainstream taxonomy + 4 fixed rows

**tmdb-search** (86 discover catalogs / 111 total): +8 mainstream top-level
genre pairs (16 views) — Action & Adventure, Comedy, Drama, Sci-Fi & Fantasy,
Horror, Thriller, Romance, Documentaries. Views went 114 -> 130.
(Grounded in Nielsen H1-2026: top streaming titles cluster in sci-fi/fantasy,
drama, comedy, kids, romance; unscripted rising.)

**rows engine (moonfin-rotate.py)**: 8 -> 12 playlist rows. Four NEW fixed
anchor rows after Trending/New Releases/[Most Played]:
Action Fix (Action & Adventure), After Dark (Horror), True Crime Files,
K-Dramas (Korean Movies + K-Dramas). The rotating block (anime + 2 combos +
mood + curated + wildcard) is unchanged; wildcard pool rebalanced — True
Crime and Korean were promoted to permanent rows and replaced by
Documentaries + Romance (pool stays 5: those two + 80s/90s/2000s).
- NOTE: manual `moonfin-rotate.py` runs must be followed by `moonfin-taste.py`
  (the taste job re-splices taste rows onto the new defaults base and rewrites
  per-user files; the 07:00/07:10 cron pair does this automatically).

### 2026-10-16 (v5) — genre-shape edge cache + warmer genre prewarm
- `conf.d/aios-api-cache.conf`: new `$aios_genre` map — shapes carrying `GenreIds=`
  (genre grid pages, genre-chip randoms, "X for You" rows) now cache 3h like
  rows/children (same live-UserData tradeoff; the hourly warmer bounds staleness
  <=1h). Per-item detail (`Ids=`) and played-seed queries carry no GenreIds and
  still BYPASS. Verified live: grid MISS->HIT (0.11s -> 0.04s), detail/seeds BYPASS.
- `aiostreams-warmer.py`: (a) genre-row warm now replays the client-EXACT shape
  (`IncludeItemTypes=Movie,Series&ExcludeItemTypes=Episode&SortBy=CommunityRating,SortName
  &SortOrder=Descending&StartIndex=0&Limit=15&Recursive=true&Fields=F_LONG&GenreIds=...
  &EnableImageTypes=...&ImageTypeLimit=1`) — cache keys are byte-exact `$args`, the old
  simplified shape never matched; (b) new genre-grid prewarm block: page-1 of the top
  `GENRE_PREWARM=30` genres from the client's /Genres list (a cold grid query costs
  1.6-4.2s on the single-threaded app; warmed = HIT ~40ms).

### 2026-10-16 (v6) — searchable libraries + Continue Watching dedupe
- **tmdb-search v1.5.0**: every library is now searchable by name — the 8
  mainstream genre pairs, ALL anime sub-genre pairs, moods, curated, combos,
  decades, streamers (netflix / hulu / disney+ / hbo max / prime / apple tv+ /
  paramount+ / peacock / crunchyroll / discovery+ / canal+), plus "new
  releases" (rolling window, computed per request) and "trending". Suffix
  handling now RECURSES, so "slice of life shows" / "anime horror shows" /
  "netflix movies" all resolve. Two fixes: (a) `_search_term()` — segments are
  percent-decoded before parsing, so splitting on '&' truncated terms
  ("law & order" -> "law", "sci-fi & fantasy" -> "sci-fi"); the term is now
  taken from its own segment whole. (b) `anime-romance-shows` was empty — TV
  has no TMDB genre 10749; series side is now keyword 9840. Smoke suite:
  **22/22** (manifest assert updated 23 -> 111 catalogs; the deep sweep covers
  all 109 catalogs).
- **aiostreams-resume-filter.py** (NEW sidecar, port 3005, systemd unit
  `aiostreams-resume-filter`): nginx routes `.../UserItems/Resume` through it.
  It keeps ONLY the first entry per series — the list is LastPlayedDate
  descending, so first == most recently watched episode — which makes the
  merged "Continue Watching & Next Up" row show ONE card per show (was: every
  partially-watched episode). Non-episodes keep unique Ids; any parse problem
  passes through untouched; fail-open to :3000 (upstream `aiostreams_resume`
  with 3000 backup); CORS headers preserved. Verified live: 7 -> 3 items.
- **nginx**: upstream `aiostreams_resume` + location
  `~* "(useritems|users/[^/]+/items)/resume"` ->
  `snippets/aiostreams-resume-dedupe.snippet.conf`. Vhost backup:
  `/root/aiostreams-site.bak-resumededupe-20261016`.
