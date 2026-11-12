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

NODE DISKS: you have no node SSH and no nodes/proxy RBAC (granting it would hand you the
  kubelet API — deliberately not done). Disk alerts arrive from Netdata with the numbers
  attached; use `kubectl describe node <n>` for capacity and `kubectl top node` for pressure.
  If the numbers are not enough to act on, escalate and say what you would need.

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
    # One retry: a single 500/connection blip (the LiteLLM proxy is on the VPS, reachable
    # only over NetBird — the same link that breaks when it pages us) used to forward the
    # raw alert to the phone as "autopilot down".
    last = None
    for attempt in range(2):
        try:
            return http_json(
                HOLMES_URL + "/api/chat",
                {"ask": prompt, "model": MODEL, "response_format": SCHEMA},
                {"X-API-Key": HOLMES_KEY},
                ASK_TIMEOUT,
            )
        except (urllib.error.URLError, OSError) as e:
            last = e
            if attempt == 0:
                time.sleep(5)
    raise last


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


# ---- page batching -------------------------------------------------------------
# 2026-11-08 23:15: cluster DNS died for ~5 min, ten monitors went DOWN together and the
# phone got ten "Homelab needs you" pushes in 90 seconds. Ten services failing at once is
# ONE incident. First alert of a burst pages immediately (never delayed); everything else
# in the same PAGE_WINDOW ships as a single digest when the window closes.
PAGE_WINDOW = int(os.environ.get("PAGE_WINDOW", "180"))
# A flapping service (openwebui was 11 escalations in 3 days) must not page every time.
# Same alert name: page at most once per REPEAT_HOURS, log the rest.
REPEAT_HOURS = float(os.environ.get("REPEAT_HOURS", "6"))
# Holmes is a single process serving a single model; a 23-alert burst fired 23 concurrent
# requests and most died at ASK_TIMEOUT ("timed out" x40 in 3 days). Queue them instead.
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "6"))
_slots = threading.Semaphore(MAX_CONCURRENT)
_paged_at = {}
_page_seen_lock = threading.Lock()
_pg = {"window_start": 0.0, "extra": [], "timer": None}
_pg_lock = threading.Lock()


def repeat_suppressed(name):
    now = time.time()
    with _page_seen_lock:
        if now - _paged_at.get(name, 0) < REPEAT_HOURS * 3600:
            return True
        _paged_at[name] = now
        return False


def page(title, body, priority=8):
    now = time.time()
    with _pg_lock:
        if now - _pg["window_start"] > PAGE_WINDOW:      # first of a new burst
            _pg["window_start"] = now
            _pg["extra"] = []
            send_now = True
        else:
            _pg["extra"].append((title, body))
            send_now = False
            if _pg["timer"] is None:
                delay = max(1.0, _pg["window_start"] + PAGE_WINDOW - now)
                _pg["timer"] = threading.Timer(delay, flush_pages)
                _pg["timer"].daemon = True
                _pg["timer"].start()
    if send_now:
        gotify(title, body, priority)


def flush_pages():
    with _pg_lock:
        extra, _pg["extra"], _pg["timer"] = _pg["extra"], [], None
    if not extra:
        return
    lines = "\n".join("- %s: %s" % (t.split(": ", 1)[-1], b.splitlines()[0][:150])
                      for t, b in extra)
    log(event="page_digest", count=len(extra))
    gotify("Homelab needs you: %d more in the same window" % len(extra), lines, 7)


def handle(alert):
    with _slots:            # queue behind in-flight analyses instead of flooding Holmes
        _handle(alert)


def _handle(alert):
    started = time.time()
    try:
        resp = ask(alert)
    except Exception as e:
        log(level="error", event="holmes_failed", alert=alert["name"], error=str(e)[:300])
        page(
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
        if repeat_suppressed(alert["name"]):
            log(event="page_suppressed", alert=alert["name"], summary=summary[:200])
        else:
            page("Homelab needs you: %s" % alert["name"], "%s\n\n%s" % (summary, action))
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

        # page batching: first of a burst goes out immediately, the rest become one digest
        sent = []
        gotify = lambda t, m, p=8: sent.append((t, m))
        _pg.update(window_start=0.0, extra=[], timer=None)
        for n in ("a", "b", "c"):
            page("Homelab needs you: %s" % n, "summary %s" % n)
        assert len(sent) == 1 and "a" in sent[0][0], sent
        assert len(_pg["extra"]) == 2
        flush_pages()
        assert len(sent) == 2 and "2 more" in sent[1][0] and "- b:" in sent[1][1], sent
        assert _pg["timer"] is None and _pg["extra"] == []

        # repeat suppression: same alert pages once per REPEAT_HOURS
        _paged_at.clear()
        assert repeat_suppressed("openwebui") is False     # first time -> page
        assert repeat_suppressed("openwebui") is True      # repeat -> logged only
        assert repeat_suppressed("nextcloud") is False     # different alert -> page
        _paged_at["openwebui"] = time.time() - (REPEAT_HOURS * 3600 + 1)
        assert repeat_suppressed("openwebui") is False     # window expired -> page again
        print("selftest ok")
        raise SystemExit(0)

    port = int(os.environ.get("PORT", "8080"))
    log(event="start", port=port, model=MODEL, holmes=HOLMES_URL)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
