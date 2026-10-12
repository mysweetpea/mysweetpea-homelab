# strm-repair-bridge — design + operations

## The problem it closes

Dead `.strm` entries 404 at playback **forever** in this stack. Root cause chain:

1. Decypharr's native repair worker (`repair` config) can *detect* broken
   entries for our zero-storage strm setup (`source: managed` probes every
   managed entry's provider link — 3,461 candidates).
2. Its built-in repair cannot reach the Arrs for strm libraries:
   `collectArrFiles()` resolves Arr file paths via `readSymlinkTarget()`
   (v2.5, `pkg/manager/repair_sweep.go`) and strm files aren't symlinks →
   broken files never get Arr file IDs → **no blocklist, no re-search**.
   `source: arr` therefore enumerates 0 candidates for our library.
   (Beta v2.5.1 adds a file-size fallback, but arr sees ~181-byte strm
   sizes vs Decypharr's real media sizes — still no match.)
3. JellySTRMprobe only deletes (and skips when a full probe wave fails, by
   design) — it cannot trigger a replacement grab.

## How the bridge works

Daily CronJob (`30 6 * * *`, after the 05:30 Decypharr sweep) —

1. **Detections — three sources**:
   a. `GET /api/repair/health` → probe-broken entries (status=broken).
   b. `GET /api/browse/__bad__` → the **sticky-Bad-flag dead zone**
      (see next section — the probe can't see these).
   c. **Empirical sweep lane** — ranged GET on every strm's real webdav URL
      (double-probed). The only source that catches the legacy
      `client_not_found` class, which decypharr's probe reports healthy.
      Skipped when decypharr itself reports >=20% broken (provider outage,
      not per-release death).
   Deduped; probe-broken wins on conflict.
2. **Streamability verification** (before any destructive action): ranged
   GET on the exact `webdav/stream/__all__/<entry>/<file>` URL Jellyfin
   uses (file name pulled from the strm's own URL). 200/206 = alive →
   `FALSE-ALARM ... skipping`; 500/412/451 = dead → proceed. This is what
   prevents false deletion when a flag outlives its failure.
3. **Scans strm contents** for the entry mapping: every strm's first line is
   `http://decypharr.../webdav/stream/__all__/<URL-ENCODED ENTRY>/<file>`, so
   entry→strm is exact (no name fuzziness). 4,726/4,727 joins verified.
4. Joins strm absolute path → Radarr `movieFile.path` / Sonarr
   `episodeFile.path` (both in-container `/data/media/...` absolute).
5. Executes the arr remediation:
   - `DELETE /api/v3/{movie,episode}file/{id}` — remove the dead pointer
   - `POST /api/v3/history/failed/{historyId}` — blocklist the grab
     (arrs have `autoRedownloadFailed=true` → auto re-search)
   - `POST /api/v3/command` `{MoviesSearch, movieIds}` /
     `{EpisodeSearch, episodeIds}` — explicit re-search
6. Writes a ledger at `/decy-state/strm-repair-ledger.json` (on the
   decypharr-state-lh PVC).

## ⚠️ THE STICKY-BAD-FLAG DEAD ZONE (found 2026-09-15, closed by the bridge)

~50 entries returned HTTP 500 at playback with body
`can't repair <name> since it's been marked as bad`. Investigation proved:

- The 500 comes from Decypharr's **`entry.Bad` sticky flag** set by the link
  service when unrestrict fails repeatedly (`markEntryBad` in
  `pkg/manager/link/service.go`). Root cause observed: **RD 451
  "infringing_file"** — content IS on RD (`status: downloaded`) but the
  actual download is DMCA-blocked (confirmed by direct unrestrict on the RD
  API). The old YTS wave is exactly this class.
- **The health probe reports these "healthy"**: the probe's `CheckFile`
  (STAT / `/unrestrict/check`) still passes while the real unrestrict 451s.
  Consequence: the repair sweep never selects them, `/api/repair/fix`
  returns `no fixable broken entries`, and `recheck?fix=true` does NOT clear
  the flag. They'd 404 forever.
- Detection: `GET /api/browse/__bad__` lists them. Remediation: same as any
  dead entry (arr delete + blocklist + re-search) — the bridge handles it.

**If playback 500s with "marked as bad"**: check `__bad__`; the bridge's next
run repairs it, or run a manual job immediately.

## Safety rails

| Rail | Behavior |
|---|---|
| Dry-run default | `--execute` required to act |
| Streamability verify | Only empirically-dead entries are touched (FALSE-ALARM skip) |
| Provider-down guard | If the freshest sweep has ≥20 probed and ≥90% broken, skip (mass failure = outage, not per-release death) |
| Action cap | `--limit` (25/run) bounds first burn-in |
| Settle cooldown | Entries not re-processed within `ARR_SETTLE_HOURS` (6h) |
| Re-fail-only | After acting, an entry is only re-acted when its `last_failed_at` is NEWER than our action (prevents blocklist churn on unrepairable titles) |

## ⚠️ Ownership prerequisite (2026-09-15 incident)

Sonarr's process runs as **uid 1000 (hotio)**, but 271 legacy strm files +
62 dirs were **root-owned** (created by Decypharr's init container in an
older era). Deletion failed with `UnauthorizedAccessException` → the whole
repair chain silently no-op'd (`DELETE ... -> False`). Fixed live with
`chown -R 1000:1000 /data/media/{tv,movies}`.

**If arr deletes start failing again, check ownership first:**
`find /data/media -name "*.strm" -printf "%u\n" | sort | uniq -c`
— anything not `hotio` breaks the chain. New strm files are created by
Decypharr (runs as 1000:1000, `PUID/PGID` env), so the fix is stable unless
an old-style writer returns.

## Verification evidence (2026-09-15)

- Path joins: **4,729/4,730 strms**.
- Adventure Time dead pack (252 files): deleted + 15 blocklist records +
  SeasonSearch → replacement `Adventure.Time.S01...RCVR[UTR]` imported →
  **S01E26 strm plays HTTP 206 in 1.4s**.

## Manual runs

```bash
kubectl create job -n private --from=cronjob/strm-repair-bridge strm-repair-bridge-manual
kubectl logs -n private job/strm-repair-bridge-manual
```

Dry-run outside the cluster (against LB IPs + a strm index file):
`--strm-index <{path: entry}.json> --dry-run`

## Monitoring — dead-man's switch (2026-10-12)

The job beats a Kuma push monitor ("strm-repair-bridge", 12h interval) at the
end of every run. The push URL lives in `/decy-state/kuma-push-url.txt` on the
state PVC — never in git (public repo).

Kuma fires when: (a) the beat goes missing >12h (bridge/cron died — the exact
silent-failure class that let ~4.9k dead files linger), or (b) we push
`status=down` for: sweep probed 0 files / bulk content death (+300 dead vs the
previous run — the RD-removal early-warning) / repairs stalled (2 consecutive
runs with 0 planned actions while candidates exist). Alerts chain: Kuma →
Gotify → phone + Paperclip poller → Groundskeeper triage.
