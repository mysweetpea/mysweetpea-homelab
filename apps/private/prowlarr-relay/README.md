# prowlarr-relay

Home-side relay that gives the **VPS-hosted AIOStreams** instance access to
home-restricted usenet indexers — specifically **NZBFinder**, which is
Cloudflare-protected and 403s Oracle datacenter IPs for both search and NZB fetch.

## Why a relay is the only working design (verified 2026-10-13)

- Prowlarr **hardcodes** 302 redirects for usenet indexers — `NewznabController.cs`:
  `if (indexer.Protocol == DownloadProtocol.Usenet || (indexer.SupportsRedirect && indexerDef.Redirect)) return RedirectPermanent(unprotectedLink);`
  The indexer validator additionally refuses `Redirect=false` for usenet
  ("Redirect must be enabled for Usenet indexers", even with `?forceSave=true`).
  → Prowlarr can *never* proxy a usenet NZB download; the client always ends up
  fetching the indexer's own URL, which the VPS cannot reach for NZBFinder.
- Alternative NZBFinder hosts from the VPS: `https://nzbfinder.ws/` → 302 (CF),
  `/api` → 403 (both schemes), `api.nzbfinder.ws` → NXDOMAIN. All dead ends.

## What it does

| Route | Behavior |
|---|---|
| `/api/v1/*` | Reverse-proxy to Prowlarr (`prowlarr.private.svc:9696`), with `Host` rewritten to `SELF_HOST` so Prowlarr's returned download URLs point back at this relay. |
| `/{id}/download?...&link=<b64>` | Asks Prowlarr to decode the link (forwards the request, captures its 302 "Location"), checks the target host against `FETCH_ALLOWLIST`, fetches the NZB **from the home IP**, returns the bytes. |
| `/healthz` | `200 ok` |

No public ingress: reachable on the home LAN / NetBird mesh only (MetalLB
`192.168.20.235:9697`). Allow-list keeps it from being a generic SSRF proxy.

## Consumer setup (AIOStreams, VPS)

Addons → Add → **Prowlarr**:
- Prowlarr URL: `http://192.168.20.235:9697`
- API key: the Prowlarr API key
- Indexers: tick **NZBFinder only** (NZBGeek/NZBPlanet keep their direct Newznab presets)

## Ops

- Logs: `kubectl -n private logs deploy/prowlarr-relay`
- Test from the VPS:
  `curl http://192.168.20.235:9697/healthz`
