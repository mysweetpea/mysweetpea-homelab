#!/usr/bin/env python3
"""Unit tests for sentinel v2 logic (matches actual function signatures)."""
import json, sys, time

import os
SENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.py")
import importlib.util
spec = importlib.util.spec_from_file_location("sentinel", SENT)
sent = importlib.util.module_from_spec(spec)
sent.__name__ = "sentinel"
spec.loader.exec_module(sent)

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")

class FakeR:
    def __init__(self, stdout, returncode=0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""

def pod(name, phase, ready, waiting=None, image="app:v2"):
    cs = {"name": "main", "state": {}, "ready": ready, "image": image}
    if waiting:
        cs["state"]["waiting"] = {"reason": waiting}
    else:
        cs["state"]["running"] = {"startedAt": "2026-09-19T00:00:00Z"}
    return {"metadata": {"name": name}, "status": {"phase": phase, "containerStatuses": [cs]}}

print("== k8s_gate with expect_tag (version-pod verification) ==")
workload = {"spec": {"selector": {"matchLabels": {"app": "x"}}}}
app = {"ns": "x", "workload_kind": "Deployment", "workload": "x", "_alias": "x"}

# new pod stuck ContainerCreating, old pod still serving -> infra, NOT bad-image
pods = {"items": [pod("old", "Running", True, image="app:v1"),
                  pod("new", "Running", False, waiting="ContainerCreating", image="app:v2")]}
sent.sh = lambda cmd, **kw: (FakeR(json.dumps(pods)) if " get pods" in " ".join(cmd)
                             else FakeR(json.dumps(workload)))
ok, why, cls = sent.k8s_gate(app, expect_tag="v2")
check("surge-pod ContainerCreating -> cls=infra (no rollback)", cls == "infra", f"{cls} {why}")
check("not ok", ok is False)

# new pod CrashLoop -> bad-image -> rollback-worthy
pods = {"items": [pod("old", "Running", True, image="app:v1"),
                  pod("new", "Running", False, waiting="CrashLoopBackOff", image="app:v2")]}
sent.sh = lambda cmd, **kw: (FakeR(json.dumps(pods)) if " get pods" in " ".join(cmd)
                             else FakeR(json.dumps(workload)))
ok, why, cls = sent.k8s_gate(app, expect_tag="v2")
check("new pod CrashLoopBackOff -> cls=bad-image", cls == "bad-image", f"{cls} {why}")

# new pod running but not Ready -> app-broken
pods = {"items": [pod("old", "Running", True, image="app:v1"),
                  pod("new", "Running", False, image="app:v2")]}
sent.sh = lambda cmd, **kw: (FakeR(json.dumps(pods)) if " get pods" in " ".join(cmd)
                             else FakeR(json.dumps(workload)))
ok, why, cls = sent.k8s_gate(app, expect_tag="v2")
check("new pod not-Ready -> cls=app-broken", cls == "app-broken", f"{cls} {why}")

# new pod Ready -> ok
pods = {"items": [pod("old", "Running", True, image="app:v1"),
                  pod("new", "Running", True, image="app:v2")]}
sent.sh = lambda cmd, **kw: (FakeR(json.dumps(pods)) if " get pods" in " ".join(cmd)
                             else FakeR(json.dumps(workload)))
ok, why, cls = sent.k8s_gate(app, expect_tag="v2")
check("new pod Ready -> ok", ok is True and cls == "ok", f"{cls} {why}")

# rollout not started (only old pods) -> pending, NOT rolled back
pods = {"items": [pod("old", "Running", True, image="app:v1")]}
sent.sh = lambda cmd, **kw: (FakeR(json.dumps(pods)) if " get pods" in " ".join(cmd)
                             else FakeR(json.dumps(workload)))
ok, why, cls = sent.k8s_gate(app, expect_tag="v2")
check("rollout not started -> cls=pending", cls == "pending", f"{cls} {why}")

print("== notify_once dedupe ==")
sent.notify = lambda t, m, p: True
state = {"notified": {}}
r1 = sent.notify_once(state, "cooldown:searxng", 6, "t1", "m1", 7)
r2 = sent.notify_once(state, "cooldown:searxng", 6, "t2", "m2", 7)
check("first fires", r1 is True)
check("second within 6h suppressed", r2 is False)
from datetime import datetime, timezone, timedelta
state["notified"]["cooldown:searxng"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
r3 = sent.notify_once(state, "cooldown:searxng", 6, "t3", "m3", 7)
check("after 6h fires again", r3 is True)

print("== push() routes cluster stalls to the autopilot ==")
class FakeResp:
    def read(self): return b"{}"
    def __enter__(self): return self
    def __exit__(self, *a): return False

posted = {}
def fake_urlopen(req, timeout=None):
    posted["url"] = req.full_url
    posted["body"] = json.loads(req.data.decode())
    return FakeResp()

notified = {}
sent.notify = lambda t, m, p: notified.setdefault("called", True) and True
import urllib.request
real_urlopen = urllib.request.urlopen
sent.DISPATCH_URL = "http://dispatcher/alert"
urllib.request.urlopen = fake_urlopen
check("dispatch returns True", sent.push("t", "m", 6) is True)
check("posts to dispatcher", posted.get("url") == "http://dispatcher/alert")
check("payload is a firing alert the dispatcher can normalize",
      posted["body"]["status"] == "firing" and posted["body"]["title"] == "t")
check("no gotify on dispatch success", "called" not in notified)

sent.DISPATCH_URL = "http://dispatcher/alert"
def boom(req, timeout=None): raise OSError("down")
urllib.request.urlopen = boom
notified.clear()
check("falls back to gotify when dispatcher is down",
      sent.push("t", "m", 6) is True and notified.get("called") is True)
urllib.request.urlopen = real_urlopen

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
