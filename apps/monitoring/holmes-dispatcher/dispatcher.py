#!/usr/bin/env python3
"""Homelab autopilot dispatcher.

Alert in -> Holmes investigates and (maybe) fixes -> phone only when it can't.

Endpoints:
  POST /alert    Kuma / Grafana / generic JSON
  GET  /healthz

Design rules:
  * recovery / pending / maintenance events never call the LLM (that is the noise).
  * a duplicate alert inside DEDUPE_SECONDS is ignored.
  * if Holmes errors or times out, the raw alert is forwarded to Gotify (fail-open).
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOLMES_URL = os.environ.get("HOLMES_URL", "http://holmes-holmes.holmes.svc.cluster.local").rstrip("/")
HOLMES_KEY = os.environ.get("HOLMES_API_KEY", "")
MODEL = os.environ.get("HOLMES_MODEL", "deepseek")
GOTIFY_URL = os.environ.get("GOTIFY_URL", "http://gotify.private.svc.cluster.local").rstrip("/")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "")
DEDUPE_SECONDS = int(os.environ.get("DEDUPE_SECONDS", "900"))
ASK_TIMEOUT = int(os.environ.get("ASK_TIMEOUT", "240"))

_seen = {}
_lock = threading.Lock()

RUNBOOK = """You are the autonomous operator of a 3-node k3s homelab (GitOps via ArgoCD).
An alert fired. Investigate it, fix it if it is safe, and report.

BUDGET: at most 10 tool calls. Do not narrate your plan. Act on the first clear diagnosis;
if nothing is clear after 10 calls, escalate.
REPLY: one JSON object only, no markdown, no prose:
{"verdict":"fixed|noop|escalate","summary":"<=40 words","action":"exact command or empty"}

TOOL QUIRKS in this Holmes build — work around them, never retry blindly:
- kubernetes_tabular_query: a '[*]' wildcard in columns/jsonpath crashes the tool (Go OOM).
  Use containerStatuses[0] / conditions[0], or describe / -o wide via run_kubectl_command.
- fetch_pod_logs: ALWAYS pass start_time (e.g. 30 minutes ago). Omitting it fails on a null parser.
- ALWAYS scope kubectl calls to one namespace. Cluster-wide queries are unreliable here and waste the budget.

RULES
- For a Deployment alert compare spec.replicas with status.readyReplicas, then read its ReplicaSets and events.
  If replicas was set to 0 but the service should be up, scale it back.
- Investigate first: pods, events, logs, deployments, replicasets, node status, recent image tags.
- Mutating verbs available to you: kubectl delete | patch | scale | rollout | label | annotate.
- Allowed namespaces: dmz, private, monitoring, argocd, longhorn-system, firecrawl, holmes.
- NEVER touch kube-system, PVCs, Longhorn volumes/backups, or the postgresql/redis StatefulSets.
- Never scale a workload to zero unless it is an old ReplicaSet that is holding an RWO volume.
- Prefer the least destructive fix. One attempt, then verify (ready replicas, pod Running, endpoint answering).

VPS (the Oracle box) IS IN SCOPE — you manage it too. It has no kubectl; go through vps-shell:
  kubectl exec -n holmes deploy/vps-shell -- ssh -i /ssh/id -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@100.82.13.16 sudo <one command>
- ubuntu has passwordless sudo. ONE command per call, no quotes, pipes or $() — the kubectl
  tool rejects shell metacharacters. Need two steps? Make two calls.
- Exec into vps-shell ONLY. Never exec into any other pod.
- VPS playbooks: `systemctl status netbird` / `systemctl restart netbird` (the VPS->LAN mesh route
  dies silently and takes media.mysweetpea.cc down — restarting netbird is the verified fix);
  `nginx -t` then `systemctl reload nginx`; `certbot certificates` (expiry); `df -h`;
  `docker ps`; `docker compose -f /opt/aiostreams/compose.yaml ps`. Never edit nginx config
  without nginx -t, never restart the whole VPS.

NODE DISKS: kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary and read
  node.fs.usedBytes/capacityBytes (kubelet reports it; no SSH needed).

PLAYBOOKS
- Pod ContainerCreating/Pending on a Deployment with a RWO PVC (Longhorn) and a Multi-Attach /
  FailedAttachVolume event: the OLD ReplicaSet's pod still holds the volume. Fix by scaling the OLD
  ReplicaSet to 0 (kubectl scale rs <old-rs> -n <ns> --replicas=0) — never the Deployment. The new pod
  then attaches and goes Ready. Verify, then report fixed.
- Pod ContainerCreating for less than ~5 minutes with no attach error: transient volume attach,
  verdict=noop and state the age. It clears on its own.
- Do NOT rollout restart a Deployment whose rollout is already in progress (updatedReplicas<replicas,
  or a pod already ContainerCreating): that restarts the stall instead of fixing it.
- Bad image (ImagePullBackOff / CrashLoop right after a tag bump): rollout undo.
- High CPU alone is NOT a fault: only act if the pod is also unhealthy (unready, crashlooping, or its own
  health endpoint failing). Otherwise verdict=noop.
- If the fault is outside your reach (router, physical hardware, a full disk, certificate expiry on a
  box you cannot ssh to, backup failure) or needs a human decision, verdict=escalate.
- verdict=fixed ONLY after you verified the result.
"""

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "AutopilotVerdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["fixed", "noop", "escalate"]},
                "summary": {"type": "string", "description": "What you found and did, max 40 words"},
                "action": {"type": "string", "description": "Exact kubectl command run, or empty string"},
            },
            "required": ["verdict", "summary", "action"],
            "additionalProperties": False,
        },
    },
}


def log(**kw):
    kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(kw), flush=True)


def http_json(url, payload, headers, timeout):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def gotify(title, message, priority=8):
    try:
        http_json(
            GOTIFY_URL + "/message",
            {"title": title[:120], "message": message[:3900], "priority": priority},
            {"X-Gotify-Key": GOTIFY_TOKEN},
            15,
        )
    except Exception as e:  # never let a failed notification kill the handler
        log(level="error", msg="gotify push failed", error=str(e)[:200])


def normalize(data, raw_text):
    """Return a list of {source,name,status,detail}; status in firing|recovery|skip."""
    out = []
    if isinstance(data, dict) and "heartbeat" in data:  # Uptime Kuma
        hb = data.get("heartbeat") or {}
        mon = data.get("monitor") or {}
        st = hb.get("status")
        out.append({
            "source": "kuma",
            "name": mon.get("name") or f"monitor-{hb.get('monitorID')}",
            "status": "firing" if st == 0 else ("recovery" if st == 1 else "skip"),
            "detail": data.get("msg") or hb.get("msg") or "",
        })
    elif isinstance(data, dict) and "alerts" in data:  # Grafana
        for a in data.get("alerts") or []:
            labels = a.get("labels") or {}
            ann = a.get("annotations") or {}
            out.append({
                "source": "grafana",
                "name": labels.get("alertname", "grafana-alert"),
                "status": "firing" if a.get("status") == "firing" else "recovery",
                "detail": " | ".join(
                    f"{k}={labels[k]}" for k in ("severity", "namespace", "pod", "instance") if k in labels
                ) + " || " + (ann.get("summary") or ann.get("description") or ""),
            })
    else:  # generic
        d = data if isinstance(data, dict) else {}
        st = str(d.get("status", "firing")).lower()
        out.append({
            "source": d.get("source", "generic"),
            "name": d.get("title") or d.get("name") or "alert",
            "status": "recovery" if st in ("ok", "up", "resolved", "recovered", "clear") else "firing",
            "detail": d.get("message") or d.get("detail") or raw_text[:800],
        })
    return out


def ask(alert):
    prompt = (
        RUNBOOK
        + "\nALERT\nsource: %s\nname: %s\ndetail: %s\n" % (alert["source"], alert["name"], alert["detail"][:1500])
    )
    return http_json(
        HOLMES_URL + "/api/chat",
        {"ask": prompt, "model": MODEL, "response_format": SCHEMA},
        {"X-API-Key": HOLMES_KEY},
        ASK_TIMEOUT,
    )


def parse_verdict(raw):
    """Structured output is not honoured by every model — accept JSON, fenced JSON, or a
    'verdict: fixed' line, in that order."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    candidates = [text]
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        candidates.append(text[i : j + 1])
    for c in candidates:
        try:
            d = json.loads(c)
            if isinstance(d, dict) and str(d.get("verdict", "")).lower() in ("fixed", "noop", "escalate"):
                return d
        except Exception:
            pass
    m = re.search(r"verdict\W{0,15}(fixed|noop|escalate)", raw, re.I)
    if m:
        return {
            "verdict": m.group(1).lower(),
            "summary": re.sub(r"\s+", " ", raw)[:600],
            "action": "",
        }
    return None


def handle(alert):
    started = time.time()
    try:
        resp = ask(alert)
    except Exception as e:
        log(level="error", event="holmes_failed", alert=alert["name"], error=str(e)[:300])
        gotify(
            "Homelab (autopilot down): %s" % alert["name"],
            "Holmes call failed, raw alert follows.\n\n%s" % alert["detail"][:1500],
        )
        return

    raw = (resp.get("analysis") or "").strip()
    verdict = parse_verdict(raw)
    if verdict is None:
        log(level="warn", event="unparsed_answer", alert=alert["name"], head=raw[:400])
        verdict = {"verdict": "escalate", "summary": raw[:600] or "no answer", "action": ""}

    v = str(verdict.get("verdict", "escalate")).lower()
    summary = str(verdict.get("summary", ""))[:700]
    action = str(verdict.get("action", ""))[:300]
    log(event="handled", alert=alert["name"], verdict=v, action=action, seconds=round(time.time() - started, 1))

    if v == "fixed":
        # Silent by design: a fix is not news. `kubectl logs deploy/holmes-dispatcher` has the trail;
        # put the gotify() call back if you ever want a p3 "Homelab fixed: …" ping.
        log(event="fixed", alert=alert["name"], summary=summary)
    elif v == "escalate":
        gotify("Homelab needs you: %s" % alert["name"], "%s\n\n%s" % (summary, action), priority=8)
    # noop: silent


def dedupe(key):
    now = time.time()
    with _lock:
        if now - _seen.get(key, 0) < DEDUPE_SECONDS:
            return False
        _seen[key] = now
        if len(_seen) > 500:
            for k in [k for k, t in _seen.items() if now - t > DEDUPE_SECONDS]:
                _seen.pop(k, None)
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body=b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, b'{"ok":true}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        if self.path.rstrip("/") != "/alert":
            return self._send(404)

        ctype = (self.headers.get("Content-Type") or "").lower()
        try:
            data = json.loads(body) if body.strip().startswith(("{", "[")) else dict(urllib.parse.parse_qsl(body))
        except Exception:
            data = {"message": body}

        self._send(202)  # answer immediately; Kuma must not time out

        for alert in normalize(data, body):
            if alert["status"] == "skip":
                log(event="skipped", alert=alert["name"])
                continue
            if alert["status"] == "recovery":
                log(event="recovery", alert=alert["name"])  # silence is the point
                continue
            if not dedupe("%s|%s|%s" % (alert["source"], alert["name"], alert["status"])):
                log(event="duplicate", alert=alert["name"])
                continue
            threading.Thread(target=handle, args=(alert,), daemon=True).start()

    def log_message(self, *a):  # quiet access log
        pass


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        assert parse_verdict('{"verdict":"fixed","summary":"s","action":"kubectl scale d x --replicas=1"}')["verdict"] == "fixed"
        assert parse_verdict('```json\n{"verdict":"noop","summary":"s","action":""}\n```')["verdict"] == "noop"
        assert parse_verdict("## Verdict: `fixed`\n\nRoot cause ...")["verdict"] == "fixed"
        assert parse_verdict("nothing to see") is None
        kuma = {"heartbeat": {"status": 0}, "monitor": {"name": "jellyfin"}, "msg": "down"}
        assert normalize(kuma, "")[0]["status"] == "firing"
        kuma["heartbeat"]["status"] = 1
        assert normalize(kuma, "")[0]["status"] == "recovery"
        graf = {"status": "firing", "alerts": [{"status": "firing", "labels": {"alertname": "HighCPU"}, "startedAt": "x"}]}
        assert normalize(graf, "")[0]["name"] == "HighCPU"
        print("selftest ok")
        raise SystemExit(0)

    port = int(os.environ.get("PORT", "8080"))
    log(event="start", port=port, model=MODEL, holmes=HOLMES_URL)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
