# HolmesGPT autopilot

`holmes/` runs the agent; `holmes-dispatcher/` is the thing the alert sources talk to.

```
Kuma (webhook) ─┐
Grafana ────────┼─▶ holmes-dispatcher /alert ──▶ HolmesGPT ──┬─ fixed    → Gotify p3
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

## Debugging

```bash
kubectl -n monitoring logs deploy/holmes-dispatcher -f          # every decision
kubectl -n holmes logs deploy/holmes-holmes -f | grep "Running tool"
curl -s -XPOST http://127.0.0.1:8080/alert -H 'Content-Type: application/json' \
  -d '{"title":"smoke","message":"scaled test deployment to 0","status":"firing"}'   # in-pod
```
