# update-sentinel — auto-rollback safety net for ArgoCD ImageUpdater

Closes the loop the image-updater leaves open: it chases "newest tag" with no health
feedback. When upstream publishes a broken release (Nextcloud 35.0.0 shipped as an EMPTY
OCI index on 2026-09-16; OpenClaw 2026.8.x boot-blocking migrations), the cluster goes
down and stays down until a human notices.

## How it works (CronJob, every 3 min)

1. **Detect** — reads `ImageUpdater/homelab-image-updater` `status.recentUpdates`
   (the updater's own event feed; nothing scraped).
2. **Verify** — for each new bump: dual health gate after a 7-min startup grace:
   - k8s gate: Deployment/StatefulSet/DaemonSet `Available` + ready replicas
   - HTTP gate: per-app verified endpoint from `configMaps.health` (empirically
     probed 2026-09-17 from this exact vantage — monitoring ns can reach dmz via
     `allow-monitoring-ingress` and private via RFC1918 ingress allows)
3. **Roll back** (unhealthy past grace): rewrites values.yaml tag to the pre-update
   value, adds the broken tag to `ignoreTags` in the ImageUpdater CR source
   (minimal-diff sed — full-file ruamel re-serialization churns ~1200 lines), pushes,
   then patches the live CR to close the re-bump window during ArgoCD sync. ArgoCD
   auto-sync then rolls the workload back to the last good image.
4. **Notify** — Gotify app "Update Sentinel" (id 13): green on verified, red on rollback,
   amber on cooldown/circuit-breaker/manual-attention states.
   **Exception: infra stalls.** A pod stuck in ContainerCreating on a RWO volume is not a
   rollback case and not a "you" case — that is the multi-attach class Holmes can clear
   (scale the old ReplicaSet to 0). Those go to the autopilot via `DISPATCH_URL`
   (`holmes-dispatcher/alert`) after `MIN_INFRA_AGE_S` seconds, with Gotify as the
   automatic fallback if the dispatcher is unreachable. Rolling back still pages.

## Safety rails

- `DRY_RUN=1` default in the CronJob env — flip to `0` after burn-in
- per-alias rollback cooldown 24h (no flapping)
- global circuit breaker: 3 rollbacks/24h → stops + pages (something systemic is wrong)
- `ROLLBACK_EXCLUDE` env for apps the sentinel must never touch
- state (cursor + ledger) in ConfigMap `update-sentinel-state`; re-fires are idempotent
- runs as root in-pod ONLY because apk needs root at boot to install git/openssh/kubectl
  (stateless python:3.12-alpine); caps dropped, no privileges escalated

## Operations

```bash
# force a cycle now
kubectl -n monitoring create job --from=cronjob/update-sentinel sentinel-manual
kubectl -n monitoring logs -l job-name=sentinel-manual -f

# one-off health check (inside any pod with the scripts CM):
python3 /scripts/sentinel.py --health-check jellyfin

# ignore-list / exclude changes: edit apps/infra/argocd-image-updater/values.yaml
# (allowTags/ignoreTags) or this app's env (ROLLBACK_EXCLUDE) — ArgoCD syncs it.
```

## After a rollback

The broken tag is in the updater's `ignoreTags` (repo + live CR). When upstream
republishes a FIXED release (new tag — e.g. 35.0.1), remove the stale entry from
`apps/infra/argocd-image-updater/values.yaml`; the updater then picks the fix up.
For single-version pins (the Sep-16 `^34\.` nextcloud pin), same file.
