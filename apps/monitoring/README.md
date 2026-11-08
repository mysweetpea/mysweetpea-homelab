# Monitoring — Observability

This zone is the cluster's nervous system: dashboards, status pages, metrics,
and logs. If something breaks, this is where you see it first.

## Why a monitoring zone?

Running 40+ services without observability is flying blind. This zone answers
three questions continuously:

1. **Is it up?** — Uptime Kuma probes every public service and shows live status.
2. **How is it running?** — Grafana + Netdata track CPU, memory, disk, and
   network across all nodes.
3. **What happened?** — Loki + Promtail collect and search logs from every
   namespace.

## What's in here

| Service | What it does |
|---------|--------------|
| `homepage/` | Personal dashboard — all services in one place (LAN only) |
| `uptime-kuma/` | Public status page (status.mysweetpea.cc) with incident history |
| `grafana/` | Metrics dashboards and alerting |
| `loki/` + `promtail/` | Log aggregation and search |
| `netdata/` | Real-time per-node and per-container metrics |
| `holmes/` | HolmesGPT autopilot — investigates alerts and fixes what it can (namespaces, verbs and escalation rules in `values.yaml`) |
| `holmes-dispatcher/` | `POST /alert` — normalizes Kuma/Grafana/Netdata payloads, drops recoveries, calls Holmes, pings Gotify only on escalate |
| `ingress-routes/` | Traefik routes for the monitoring UIs |

## How it fits together

```
Uptime Kuma ──► probes public endpoints ──► status.mysweetpea.cc
Netdata     ──► per-node metrics ────────► Grafana dashboards
Promtail    ──► collects logs ───────────► Loki ──► Grafana explore
```

The homepage dashboard aggregates all of it into one screen for day-to-day
management, while the public status page keeps the community informed.
