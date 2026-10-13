# Zilean — DMM-hash indexer

Zilean indexes [DebridMediaManager](https://github.com/debridmediamanager/debrid-media-manager)
hashlists so the Arr stack can **see which releases are already cached on
Real-Debrid** before grabbing. It exposes a Torznab endpoint that Prowlarr
consumes like any indexer.

## Architecture

```
Prowlarr → Zilean (Torznab, port 8181) → DMM hashlist (hourly scrape)
  → Radarr/Sonarr search → only cached releases returned
  → Decypharr grab → instant (no RD download wait)
```

- Image: `ipromknight/zilean:latest` (upstream v3.5.0, Apr 2025 — upstream is
  dormant; the AyushSehrawat fork has no published image, so upstream it is)
- DB: shared Postgres (`zilean` database + `zilean` role on postgresql-0)
- First sync: downloads the full DMM hashlist + IMDB title.basics.tsv, then
  matches torrents to IMDb IDs — takes ~30-60 min on first boot. Subsequent
  hourly scrapes are incremental.

## Config (values.yaml + sealed secret)

| Setting | Value |
|---|---|
| `Zilean__ApiKey` | sealed (Prowlarr uses it) |
| `Zilean__Database__ConnectionString` | sealed (postgresql.private.svc.cluster.local) |
| `Zilean__Dmm__EnableScraping` | `true` (hourly) |
| `Zilean__Torznab__EnableEndpoint` | `true` |
| `Zilean__Imdb__EnableImportMatching` | `true` (title→IMDb ID) |
| `Zilean__Imdb__NumberOfCores` | `2` (don't starve the node) |

⚠️ **Torznab path is `/torznab/api`** (not `/torznab`) — Prowlarr baseUrl must
be `http://zilean.private.svc.cluster.local:8181/torznab/api`.

## Prowlarr

- Indexer: Torznab, baseUrl above, API key from the sealed secret, categories
  2000+5000, priority 25 (below the private trackers).
- Sync to Radarr/Sonarr after adding.

## Ops

- Health: `curl http://zilean.private.svc.cluster.local:8181/healthchecks/ping`
- Logs: `kubectl logs -n private deploy/zilean -f`
- Data: `/app/data` (emptyDir — the Postgres DB is the durable store; the
  IMDB tsv re-downloads on restart)

> **Removed from the cluster 2026-10-12** - debrid-era indexer (DMM hash caches), never used in the usenet-only pipeline. Manifest renamed `application.yaml.disabled` so the app-of-apps stops deploying it; charts kept for reference.
> **Note:** the `zilean-secrets` sealed secret is NOT Zilean-specific - it holds the shared GitHub token used by the media engine, Jellyfin plugin checks and repo tooling. Keep it.
