# October Usenet Cutover — Plug-and-Play Runbook

**When:** 2026-10-10 — user purchases Usenet credentials (Newshosting + NewsGroupDirect block), then pastes them to Hermes for wiring.
**RD expires:** 2026-10-17 (re-verified via RD API 2026-10-04: expiration 2026-10-17T05:56Z).
**Prereq:** OCT budget buys = Newshosting intro ($29.85) + NZBPlanet ($15) + NewsGroupDirect block ($16). NZBGeek comes in NOV.

Everything below is **already staged and scratch-verified** (2026-09-15). The
cutover itself is a credentials drop + arr priority flip + indexer adds.

---

## What is already in place (verified 2026-09-15, re-verified live 2026-10-04)

| Piece | State |
|---|---|
| Decypharr usenet streaming (direct NNTP, no SABnzbd) | Built into v2.5, running (image `cy01/blackhole:v2.5`, pod up 16d) |
| `usenet.providers: []` in live config.json | Confirmed empty — no 503 gate trip; tunables staged (max_conn 15, read_ahead 16MB, buffer 512MB) |
| Provider env schema | `DECYPHARR_USENET__PROVIDERS__N__*` — all fields present in v2.5 binary |
| Sealed secret `decypharr-secrets.yaml` | Currently has ONLY arr tokens + RD api key (grep DECYPHARR_USENET = 0) — merge-into will add the 15 usenet keys at cutover |
| NNTP egress (port 563) | Open in `allow-internet-egress` netpol (re-verified live) |
| SAB-mock client in Radarr + Sonarr | id=3 both, **live-tested 2026-10-04: test returns `{}` = green** (usenet client prio=2, RD prio=1 — flip at cutover) |
| Decypharr state persistence | `decypharr-state-lh` PVC Bound 5Gi |
| auth.json preservation | Init container no longer overwrites (token stable across restarts) |
| Repair chain | Decypharr sweep (ran 2026-10-04 05:30: probed 360, broken 314 flagged YTS-dead class, bridge skipped 309 no-linkage) + strm-repair-bridge cron 06:30 live in `private` ns |
| Download dirs | `/data/decypharr-downloads/{Radarr,Sonarr}` exist (case matches categories) |
| Prowlarr indexers | Currently Nyaa/NZBIndex/YTS/Zilean — NZBGeek + NZBPlanet + ameNZB get ADDED at cutover (need purchased accounts first) |
| Prowlarr→arr sync | Radarr + Sonarr fullSync both linked |

**Scratch-proven credential mechanics** (docker test, 2026-09-15):
- Full creds via env → both providers materialize, `backup:true` accepted, SAB mock 200, gated paths 200
- Partial creds (host only) → `503 "usenet provider password is required"` — **all fields must land together**
- Port 563 auto-forces SSL; BACKBONE auto-lowercased

---

## Cutover steps (exact order)

### 1. Fill the sealed secret (NEW values, all at once)

On the master (kubeseal + cluster key live there):
```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd mysweetpea-homelab

# Build the PLAIN secret delta (keys not yet in the sealed secret):
cat > /tmp/decy-usenet.yaml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: decypharr-secrets
  namespace: private
stringData:
  DECYPHARR_USENET__PROVIDERS__0__HOST: news.newshosting.com
  DECYPHARR_USENET__PROVIDERS__0__PORT: "563"
  DECYPHARR_USENET__PROVIDERS__0__USERNAME: <user>
  DECYPHARR_USENET__PROVIDERS__0__PASSWORD: <pass>
  DECYPHARR_USENET__PROVIDERS__0__BACKBONE: Omicron
  DECYPHARR_USENET__PROVIDERS__0__SSL: "true"
  DECYPHARR_USENET__PROVIDERS__0__MAX_CONNECTIONS: "40"
  DECYPHARR_USENET__PROVIDERS__0__PRIORITY: "1"
  DECYPHARR_USENET__PROVIDERS__1__HOST: news.usenetexpress.com
  DECYPHARR_USENET__PROVIDERS__1__PORT: "563"
  DECYPHARR_USENET__PROVIDERS__1__USERNAME: <user>
  DECYPHARR_USENET__PROVIDERS__1__PASSWORD: <pass>
  DECYPHARR_USENET__PROVIDERS__1__BACKBONE: UsenetExpress
  DECYPHARR_USENET__PROVIDERS__1__SSL: "true"
  DECYPHARR_USENET__PROVIDERS__1__MAX_CONNECTIONS: "20"
  DECYPHARR_USENET__PROVIDERS__1__PRIORITY: "2"
  DECYPHARR_USENET__PROVIDERS__1__BACKUP: "true"   # block account = fallback tier only
EOF

# MERGE into the existing sealed secret (preserves the RD/arr tokens):
kubeseal --controller-name sealed-secrets --controller-namespace kube-system \
  --merge-into sealed-secrets/private/decypharr-secrets.yaml < /tmp/decy-usenet.yaml

rm /tmp/decy-usenet.yaml   # scrub plaintext
# verify the merged file now has the new keys:
grep -c "DECYPHARR_USENET" sealed-secrets/private/decypharr-secrets.yaml   # → expect 15
```

**⚠️ Backbone facts (researched):** Newshosting = Omicron; UsenetExpress (theCubeNet/NewsGroupDirect
block) = UsenetExpress backbone. `backbone` lets Decypharr skip same-backbone providers after
423/430 article-not-found. `backup: true` = only consulted when ALL primaries fail (block-billing
protection).

### 2. Push + sync + restart

```bash
git add -f sealed-secrets/private/decypharr-secrets.yaml   # gitignored by default
git commit -m "decypharr: usenet provider credentials (phase 2 cutover)"
git push
# ArgoCD syncs the SealedSecret -> Secret; then restart to pick up env:
kubectl rollout restart deploy/decypharr -n private
```

### 3. Verify the cutover (the exact checks)

```bash
# a) providers materialized
kubectl exec -n private deploy/decypharr -- cat /app/config.json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(len(d['usenet']['providers']), 'providers')"
# → expect 2

# b) SAB mock answers (via LB or in-cluster)
curl -s "http://decypharr.private.svc.cluster.local:8282/sabnzbd/api?mode=version"  # {"version":"4.5.0"}
# c) arr clients re-test (both should return {} = no errors)
#    Radarr: Settings → Download Clients → Decypharr (Usenet) → Test
#    (or API: POST /api/v3/downloadclient/test with the client body)
# d) live grab — grab one movie via Radarr search, then:
kubectl logs -n private deploy/decypharr --tail=50 | grep -i "nzb\|usenet"
# e) playback — open the movie in Jellyfin; strm should resolve (206)
```

### 4. Flip arr priorities (usenet first, RD as fallback)

In both arrs: set Decypharr (Usenet) priority **1**, Decypharr (RD) priority **2**.
(The RD client stays enabled as a safety net; usenet now wins every grab.)
- Radarr: `PUT /api/v3/downloadclient/3` priority=1; `PUT .../2` priority=2
- Sonarr: same ids.

### 5. Add the 3 indexers to Prowlarr (5070 enabled for anime)

NZBGeek + NZBPlanet + one more (abNZB/altHUB). **Enable category 5070 (TV/Anime) on all three**
plus 2000-series movies. Sync to both arrs after adding.

Anime pipeline (researched): the key groups ([DKB], [ASW], [LoliHouse]) ARE mirrored to Usenet
via ameNZB; add ameNZB as a Generic Newznab (`https://amenzb.moe/api` + profile key, IP-pinned)
if anime coverage gaps appear.

### 6. Connection budget + tuning (20-user scale)

**The math that matters** (from Sep 2026 research): your account pool = Newshosting 100 + UsenetExpress 50 = **150 NNTP connections total**. Each actively-streaming file holds `usenet.max_connections` (15) connections — so ~10 simultaneous streams consume the whole pool. Two protections are already in place:

| Knob | Value now | Why |
|---|---|---|
| `usenet.max_connections` | 15 | Per-**stream** NNTP width. 15 × 10 streams = 150 = full pool. If 15-user concurrency ever happens, lower to 8-10 and raise `read_ahead` instead (bandwidth comes from read-ahead, not raw conn count). |
| `usenet.buffer_memory` | 512MB (explicit) | Host-wide RAM cap across ALL open streams — the researched **real concurrency ceiling** (decoded-article budget, not connections). Raise to 1-2GB after burn-in if streams stall while RAM is idle. |
| `usenet.read_ahead` | 16MB | Prefetch window; 32MB if first-byte feels slow under load. |
| `max_active_downloads` | 5 | Shared torrent+NZB concurrent *non-streaming* jobs (imports/parses). Fine to keep. |
| provider `max_connections` | 100 (NH) + 50 (UX) at cutover | Must match your actual account limits — never exceed, providers ban over-limit accounts. |

Watch `NzbDAV #477`-class risk (unbounded conn growth) does NOT apply here — Decypharr pools per provider; but during cutover burn-in, watch `/api/queue` + provider dashboards for a few days to confirm connection counts stay ≤ account limits.

Jellyfin-side guard (already live): `RemoteClientBitrateLimit` 12 Mbps per stream → caps how many heavy streams can pile up.

| Symptom | Lever |
|---|---|
| Slow first byte | `usenet.read_ahead` 16MB→32MB; check provider RTT; raise `socket_read_buffer` |
| 430 not-found | Check BACKBONE values; verify backup=true flag; the bridge's re-search grabs an alternate release automatically |
| Stalls mid-play | Raise `buffer_memory` 512MB→1GB; check `processing_timeout` (10m ok) |
| RD still grabbing | Priorities didn't save — re-check both arr clients |

---

## Rollback (if usenet misbehaves)

1. Flip arr priorities back (RD=1, usenet=2) — RD still works until 2026-10-17.
2. Or disable the usenet client in both arrs entirely.
3. Decypharr keeps RD configured (validation needs ≥1 provider of either kind).

## Known limitations (researched, accepted)

- **v2.5 DMCA gotcha**: a dead NZB returns HTTP 500 to the arr → Sonarr reads "Download Client
  Unavailable" (issue #284, open). Mitigation = the strm-repair-bridge (blocklist+re-search) +
  queue_cleanup rules (already configured). Beta v2.5.1 fixes this properly.
- **No PAR2 repair in v2.5** (beta-only). Incomplete releases rely on the arr grabbing a replacement.
- **Priority ≠ backup by default** — that's why the block provider is marked `backup: true`.
- **First byte is contention-dependent** (0.3–19s reported under parallel load). Keep provider
  connections generous.

## Upstream watch

- ~~`cy01/blackhole:beta`~~ **MOOT (2026-10-08): v2.6 stable (Oct 6) superseded the
  beta line and the image-updater already rolled the pod to v2.6.** v2.0's
  repair-v2/DFS/PAR2 work all landed in stable. Do NOT pin `beta` (it is now
  OLDER than stable).
- ⚠️ **v2.6 compat-API auth break** (found + fixed 2026-10-08): the updater's
  v2.5→v2.6 bump silently 401'd BOTH download clients (RD qbit client id=2 +
  SAB id=3) in both arrs — v2.6 requires the arr FQDN as username and the
  decypharr api_token as password (arr API keys no longer authenticate on
  probes that omit `category`). Symptom was the Oct 8 06:38 HIMYM re-grab storm
  all `downloadFailed` "Unable to connect". Fix = update username/password on
  all 4 clients (see README "Download client config in arrs"); arr Test + live
  grab verified. LESSON: after any decypharr major bump, re-run arr TestAll
  BEFORE the next grab window.
- Image-updater CR currently pins `^\d+\.\d+(\.\d+)?$` → beta/latest tags NOT auto-picked.
