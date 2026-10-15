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
