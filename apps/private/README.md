# Private — LAN / Internal Services

This zone holds services that do **not** need to be on the public internet.
They serve the household and community over the LAN (or through the tunnel
with SSO where remote access is useful).

## Why a private zone?

Most of these services handle personal data — photos, files, passwords,
watch history. Keeping them off the public internet means:

- **No exposure** — they are unreachable from outside the network by default.
- **Defense in depth** — even if the DMZ were compromised, the private zone
  has its own default-deny policies (12 NetworkPolicies).
- **Performance** — media streaming and file sync stay on the LAN, no tunnel
  overhead.

## What's in here

### Media stack (see [media-stack.md](./media-stack.md) for the full pipeline)

| Service | What it does |
|---------|--------------|
| `jellyfin/` | Media server (Moonfin UI + 34 plugins) |
| `seerr/` | Request portal (in DMZ, talks to the arrs here) |
| `radarr/` `sonarr/` | Movie / TV library management |
| `bazarr/` | Subtitle management |
| `prowlarr/` | Indexer management |
| `qbittorrent/` | Download client |
| `aiostreams/` | Stremio-style streaming aggregator |
| `media-storage/` | Shared 200Gi media volume |

### Data & productivity

| Service | What it does |
|---------|--------------|
| `postgresql/` | Shared PostgreSQL (PG18) — database for many services |
| `n8n/` | Automation + webhook backend (powers the website forms) |
| `nextcloud/` | Files / cloud storage |
| `immich/` + `immich-postgresql/` | Photo library |
| `gotify/` | Self-hosted push notifications |
| `open-webui/` | AI chat frontend (Ollama backend) |
| `affine/` | Notes (in DMZ) |

### Tools & AI

| Service | What it does |
|---------|--------------|
| `flaresolverr/` | Cloudflare-bypass proxy for indexers |
| `nzbdav/` | Usenet access |
| `openclaw/` | AI agent gateway |
| `mcp-server/` | Model Context Protocol server |
| `redis-affine-master/` | Redis for AFFiNE |

## How access works

Private services are reached over the LAN, or remotely through the Cloudflare
Tunnel with Authentik SSO where enabled. The media stack is the busiest part
of the cluster — see the [media pipeline deep-dive](./media-stack.md).
