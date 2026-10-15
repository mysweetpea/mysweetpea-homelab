# moonbase-lite

Moonfin `/Moonfin/*` shim for media servers that run **AIOStreams** instead of
Jellyfin + the Moonbase plugin. One stdlib Python file, loopback-only, fronted
by nginx (`upstream moonbase_lite { 127.0.0.1:3002; 127.0.0.1:3000 backup; }` —
the backup makes it fail silent to the old 404 behaviour when the shim is down).

Serves: Ping, Defaults, Settings (scoped, Resolved, Profile push, delete,
Admin/PushDefaults, SSE Stream), Messages (read + audience filter), MDBList
ratings (proxy + cache), Themes (list/serve/upload), Notification prefs, web
stubs. Everything else 404s — clients degrade gracefully (same as no plugin).

## Files
- `moonbase_lite.py` — the service (deploy to `/opt/moonbase-lite/`)
- `config.example.json` → `config.json` (mdblist server key lives here, chmod 600)
- `seed-defaults.py` — builds `data/defaults.json` (fleet defaults) from a
  Moonfin settings export: keeps the `global` scope, strips API keys, filters
  home rows to `resume,latestmedia`, pins the theme
- `themes/` — theme JSONs served to clients (incl. `mysweetpea_obsidian`)
- `moonbase-lite.service`, `nginx-moonbase-lite-proxy.snippet.conf`
- `test-moonbase-lite.sh` — smoke matrix (needs `TOKEN` = a real device token)

## Ops
```bash
sudo -S -p '' systemctl restart moonbase-lite && journalctl -u moonbase-lite -n 20
sudo -S -p '' nginx -t && sudo -S -p '' systemctl reload nginx
python3 seed-defaults.py settings.json data/defaults.json --theme mysweetpea_obsidian
sudo -S -p '' systemctl restart moonbase-lite   # after defaults.json change (no restart needed for themes/users)
```
Data: `/opt/moonbase-lite/data/{defaults.json,users/,themes/,messages.json,notifications/,mdblist-cache.json}`

## Family personas (rollout)
Personas live in the AIOStreams user config under `jellyfin.personas`
(`{id, name, avatar?, history, hidden?, address?, lock}` — `lock` IS the PIN,
6+ digits; `address` = own sign-in code for hidden users: `/jellyfin/p/<address>`).
Add via the dashboard **Users** tab, or by merging into `config.jellyfin.personas`
through `PUT /api/v1/user`. Instance setting `jellyfin.pinSignIn=true` is already
enabled, so members sign in with their name + PIN (5 wrong tries = 15 min lockout);
PINs must be 6+ digits. Vendor note: pin-only sign-in on a *public* instance leans
on the unguessable config/address URLs — keep PINs long.

## Known v1 limits
- Admin checks (PushDefaults, Admin/Themes) are best-effort: treat any valid
  device token as admin (household-trust model). Tighten if the server ever
  goes multi-tenant.
- Messages are read-only from the file `data/messages.json`; no write API.
- Seerr / Calendars / TMDb enrichment deliberately not ported (not needed on
  a catalog server; catalogs are curated in AIOStreams itself).

## Provisioning bridge (Authentik account creation -> media persona)
`POST /Moonfin/Admin/Provision` (header `X-Admin-Token: <config.admin_token>`), body
`{username, name, tier?, pin?, remove?}`:
- creates persona `{id: slug(username), name, history: own, lock: pin}` via the
  AIOStreams config API (creds in config.json: `config_uuid`, `config_password`);
  server bcrypt-hashes the PIN; returns `{success, persona, pin}` (pin once).
- idempotent (`exists: true` on repeat), `remove: true` deletes; `tier` gated by
  `config.provision_tiers` (default `["sweetpea"]`; other tiers get `skipped`).
- one automatic retry on HTTP 429 (AIOStreams write burst limiter).

Wired into n8n **Invite Code Redemption** (workflow 7MAu6rokN81F31kH): after
Set Password it calls this endpoint and the Welcome Email gains a "Your media
PIN" block (hidden when empty). Members sign in with their name + PIN on the
picker address; wrong PIN = 401; 5 wrong = 15 min lockout.

## OIDC SSO — removed (owner decision, Oct 13 2026)
Wired then removed: the AIOStreams dashboard is operator-only and the owner
prefers the local password there; members cannot use OIDC anyway (headless
clients) — their pairing is the provisioning bridge above. Env lines stripped
+ authentik provider/application/scope-mapping deleted, verified
(`/api/v1/auth/oidc/start` -> `login?error=oidc_disabled`). To restore: create
an authentik OAuth2 provider (redirect `https://aiostreams.mysweetpea.cc/api/v1/auth/oidc/callback`,
scopes incl. `groups` + a custom groups scope mapping), set the
`AIOSTREAMS_OIDC_*` env vars, recreate the container.

## Client-compat stubs + SSE cadence (added 2026-10-14)
- Stub endpoints (answer the Moonbase plugin's own "feature off" shapes, so Moonfin
  stops logging 404s / probe loops on a server that has no such features):
  `GET Games/Libraries` -> `[]`; `GET Seerr/Status` -> `{enabled:false,authenticated:false,url:null}`;
  `GET Seerr/Config` -> `{enabled:false,url:null,displayName:"Seerr",variant:"seerr",userEnabled:true}`;
  `GET AnimeMarkers/Items` -> `{enabled:false,items:{}}`; `GET AnimeMarkers/Series` -> `{enabled:false,matched:false,episodes:{},seasons:{}}`.
  Feature-OFF answers, not implementations (no Seerr/requests on this server by owner decision).
- `Settings/Stream` (SSE) holds ~22s then closes CLEANLY; clients reconnect (same cadence
  the Moonbase plugin produces on Jellyfin). Never hold it open indefinitely: the Moonfin
  web build (2.7.0) cannot read past its 30s receiveTimeout -> client error loops.

## Fleet home rows (2026-10-14)
Shim defaults enable 12 home rows (client sorts ascending by .order):
1 resume, 2 nextup, 3-5 sinceyouwatched1-3, 6 rewatch, 7 genres, 8 latestmedia,
9 collections, 10 favoritemovies, 11 favoriteepisodes, 12 playlists.
Inside the latestmedia section the rows follow catalogModifications order:
Trending/Popular first, then streaming services (popularity order), then
Collections/Marvel last (updated 2026-10-14 round 3).
`navbarAlwaysExpanded=false` (compact icon-only navbar; stock client default).
JF-parity rows impossible on this server (plugin-backed): seerr_*, radarr/sonarr_calendar, imdb_*, tmdb_*.

## Per-catalog rows + search fan-out trim (2026-10-14)
- `mergeRecentRowsByType=false` pinned in fleet defaults + the user override (Resolved verified):
  the client renders one "Recently Added <catalog>" row per library instead of merging them
  into single Movies/TV rows. This is what makes the streaming-service rows (Netflix, Hulu,
  Crunchyroll, HBO Max, ...) visible on the home page. Flip back to `true` to collapse them.
- AIOStreams search fan-out trim (server-side; lives in the config's catalogModifications, not
  here): 34 catalogs got `disableSearch: true` — all streaming-catalogs entries, Marvel,
  collections, AniList and tmdb year/language. Cold full search dropped ~13s -> ~1s; the app's
  8s request timeout was killing the first search of every new term. Searchable set kept:
  `[tmdb.search movie, tmdb.search series, collections search]`. Pre-change config backup:
  `/tmp/aios-ud-current-copy.json` on the VPS; patched copy `/tmp/aios-ud-patched.json`.
- Do not disable search on the tmdb.search catalogs — they ARE the search sources.

## Next-episode badge: /Moonfin/Tmdb/NextEpisode (2026-10-14)
- Client feature: series pages show a "Next: Today/Tomorrow/<weekday>/<Mon D> (S2:E1)"
  badge when the series item has `ProviderIds.Tmdb` (AIOStreams items do) and the
  endpoint answers. The endpoint was unimplemented here, so the badge never appeared.
- Implemented in moonbase_lite.py: GET `/Moonfin/Tmdb/NextEpisode?tmdbId=` → mirrors the
  Moonbase plugin's TmdbNextEpisodeResponse: `{success, airDate, seasonNumber,
  episodeNumber, name}` (from TMDB `next_episode_to_air`), 6h in-memory cache,
  token-gated, reachable via root + mount routes.
- TMDB key lookup order (per request, no restart): `/opt/moonbase-lite/tmdb.key`
  (ubuntu:ubuntu, 600) → caller's resolved synced `tmdbApiKey` → `config.json`'s
  `tmdb_key`. No key → `{"success": false, "error": "No TMDB API key configured on
  the server."}` (client shows no badge; harmless).
- `detailMetadataOrderDesktop/Mobile/Tv` pinned in defaults.json with
  `upcomingEpisodeDate` first (client only renders the badge if it's in that order):
  `upcomingEpisodeDate,year,parentalRating,runtimeAndSeasons,status,genres`.
- `Tmdb/EpisodeRating` is WIRED (2026-10-15): per-episode `vote_average` from TMDB,
  response mirrors the Moonbase plugin exactly (`success, voteAverage, voteCount, name,
  airDate, seasonNumber, episodeNumber, stillPath`), 6h in-memory cache. Client reads
  only `voteAverage`. `SeasonRatings` stays unwired (zero call demand recorded).
- `Tmdb/ProductionCompanies` + `Tmdb/StudioImage/{id}` are WIRED (2026-10-15 night):
  companies + TV networks ride one list (`{id, name, hasLogo}`; networks use NEGATIVE
  ids — C# `NetworkCacheId => -id`). `hasLogo` is optimistic (logo_path present, not a
  download check). StudioImage resolves the logo via TMDB `/company|network/{id}/images`
  (png-only, language-neutral first, w500), served `image/png` w/ immutable cache;
  in-memory logo dict (restart = refetch once per company). `_caller` now also accepts
  `?ApiKey=` / `?api_key=` query tokens — image widgets cannot set headers.
- `Notifications/Register` (POST/DELETE) is an accepted no-op: the client only checks
  the HTTP status and this stack has no push delivery path (in-app Messages is the
  channel). Kills the retry queue that produced ~41 404s/day.

## Media bar pool (2026-10-14)
- `mediaBarLibraryIds` = 7 catalogs as a JSON LIST: Trending M/S, Popular M/S,
  Netflix Movies, Netflix Top 10 Movies/Shows (Global). `mediaBarExcludedGenres` =
  Reality / Talk Show / Game Show / Soap. Both are SyncedFields — edit in defaults +
  user files (seed-defaults.py pins them).
- Client mechanics: pools ~40 newest per catalog (SERIAL fetches, ~0.3s each — keep the
  list ≤7), filters (backdrop required, no BoxSets, genre filter, parental), shuffles,
  shows `mediaBarItemCount` (10) slides; fresh random cut every app launch.

## 2026-10-15 — Since-you-watched rows 4/5 fleet-wide
- `sinceYouWatchedNumRows=5` + `sinceYouWatched4/5Enabled` + the full 67-entry row list are now **PINS** entries (merged last = push-proof; devices re-push their own homeSections which otherwise shadow defaults.json). Row list source of truth: `moonbase_lite.py` PINS.
- `seed-defaults.py --keep` updated to keep `sinceyouwatched4,sinceyouwatched5`.
