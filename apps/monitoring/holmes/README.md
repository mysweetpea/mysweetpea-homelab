# HolmesGPT autopilot

`holmes/` runs the agent; `holmes-dispatcher/` is the thing the alert sources talk to.

```
Kuma (webhook) ─┐
Grafana ────────┼─▶ holmes-dispatcher /alert ──▶ HolmesGPT ──┬─ fixed    → log only
Netdata ────────┘        (drops recoveries,                   ├─ noop     → silence
                          dedupes 15 min)                     └─ escalate → Gotify p8
```

## Model

`deepseek-v4.1-flash:1017` through the LiteLLM gateway on the VPS
(`http://100.82.13.16:4000/v1`), key alias `holmes`. The key lives in the sealed
secret `holmes-llm`; `HOLMES_API_KEY` in the same secret is the bearer the
dispatcher sends.

## What the agent may do

`mcpAddons.kubernetesRemediation.config.allowedCommands` = `delete,patch,scale,rollout,label,annotate`
— no `exec`, `run`, `drain`, `taint` or `edit`. `approvalRequiredTools: []` means no human
confirmation, so the verb allowlist *is* the safety boundary. No SSH, no PVC or Longhorn access;
anything outside the cluster (VPS, router, NetBird, hardware, backups) must escalate.

Runbook, namespaces and the no-go list live in `holmes-dispatcher/dispatcher.py` (`RUNBOOK`)
because that is what actually builds the prompt.

## Kuma notification switch (one-time, not in git)

Kuma stores notifications as rows, so this step cannot be expressed in this repo. It was run once:

- notification `2` = webhook → `http://holmes-dispatcher.monitoring.svc.cluster.local/alert`, bound to every monitor
- notification `1` = the old Gotify one, re-enabled but bound **only** to the two watchdog
  monitors (`autopilot-dispatcher`, `autopilot-holmes`) so a dead autopilot still pages

Rollback:

```bash
kubectl -n monitoring exec deploy/uptime-kuma -- node -e 'const{DatabaseSync}=require("node:sqlite");const d=new DatabaseSync("/app/data/kuma.db");d.prepare("update notification set active=1 where id=1").run();d.prepare("update notification set active=0 where id=2").run();'
kubectl -n monitoring delete pod -l app.kubernetes.io/name=uptime-kuma
```

Backup of the pre-change database: `/app/data/kuma.db.bak-holmes-20261108` on the kuma PVC.

## Two things that will waste your afternoon

1. **Callers must send `Accept: application/json`.** Holmes copies request headers into the
   kubectl tool context and a shell-injection guard rejects `*`, which makes every read fail and
   the model report a fake "kubectl OOM". `curl` defaults to `Accept: */*`.
2. **Scope kubectl queries to a namespace.** Cluster-wide reads inside Holmes are unreliable; the
   dispatcher's prompt says so explicitly.
3. **`[*]` wildcards crash the query tool for real.** `containerStatuses[*].ready` in a
   `kubernetes_tabular_query` column or a jsonpath/jq expression takes the tool process out with a
   Go OOM (measured: same query with `[0]` succeeds instantly, `[*]` dies). Unrelated to #1 — both
   look like "OOM" in the transcript. Use `[0]`, `describe`, or `-o wide`.
4. **`fetch_pod_logs` needs an explicit `start_time`.** Without it the tool parses a `null` and
   raises `ParserError: Unknown string format: null`.

## Where alerts come from now

Two feeders, one brain:

| source | path | who acts |
|---|---|---|
| Uptime Kuma (46 monitors) | notification `Holmes autopilot` → `/alert` | Holmes |
| update-sentinel: RWO/volume attach stall on a rollout | `DISPATCH_URL` → `/alert` (falls back to Gotify if the dispatcher is down) | Holmes |

The sentinel still owns the *rollback* decision (bad image) and still pages on that — it knows the
image is fine during an infra stall, so it hands that class over instead of telling you to ask
Hermes. It waits `MIN_INFRA_AGE_S` (180s) first: those attaches usually clear on their own, and an
LLM call is more expensive than a push.

## Page batching

`PAGE_WINDOW` (default 180s) collapses bursts: the **first** escalation of a burst pages immediately,
everything else inside the window ships as one `Homelab needs you: N more in the same window` digest
when it closes. Learned the hard way on 2026-11-08: cluster DNS wobbled for ~5 minutes, ten monitors
went DOWN together and the phone got ten pushes in 90 seconds — ten services failing at once is one
incident. Individual analysis still runs per alert (one of them *was* the real fault: jellyfin's 502
came from a dead NetBird LAN route on the VPS, fixed by restarting netbird), only the paging is merged.
`ask()` also retries once on a connection/5xx error before falling back to "autopilot down", because
the LiteLLM proxy it talks to lives behind that same NetBird link.

## Debugging

```bash
kubectl -n monitoring logs deploy/holmes-dispatcher -f          # every decision
kubectl -n holmes logs deploy/holmes-holmes -f | grep "Running tool"
curl -s -XPOST http://127.0.0.1:8080/alert -H 'Content-Type: application/json' \
  -d '{"title":"smoke","message":"scaled test deployment to 0","status":"firing"}'   # in-pod
```
