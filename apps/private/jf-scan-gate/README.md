# jf-scan-gate

Coalescing gate in front of Jellyfin's `/Library/Refresh`. The Sonarr + Radarr
"Jellyfin Library Scan" webhooks point here instead of Jellyfin directly.

- Forwards at most **1 refresh per 600s** (nginx `limit_req`, one shared bucket for all callers).
- Extra requests get an instant `200 "debounced"` so the arr's webhook history stays green.
- `GET /health` for monitoring; everything else 404s.

Root cause it fixes (2026-10-12): during bulk import waves (repair re-grabs, megashow
seasons) every completed import fired a full library scan - measured 93 scan requests /
90 min with overlapping + cancelled scans holding the library lock almost continuously;
every client browse queued behind it (p50 ~10s vs 0.9s baseline; worst calls 40-120s).
Jellyfin's own scheduled scan remains as the ultimate fallback.
