#!/usr/bin/env python3
# Deployed: /usr/local/bin/vps-update.py on the VPS, root cron 30 5 * * *. Secrets live in /opt/vps-update/ (gotify.key, kuma_push.url) — not in this repo.
"""Oracle VPS auto-updater - the VPS's answer to ArgoCD Image Updater.

Lanes
  A  AIOStreams   daily   GitHub latest release vs compose pin -> sqlite snapshot + compose
                          backup, bump tag, pull, health-gate, rollback (incl. DB restore).
  B  llm-proxy    weekly  digest-gated image refresh (litellm + postgres); health-gate plus
                          a real chat-completion probe; retag rollback to previous images.
  C  report       daily   apt pending / reboot-required / disk ride the Kuma push message.

Notifications (Gotify, Update Sentinel lane) fire only on change or failure - silence means
boring. Every non-check run ends in a Kuma push heartbeat, so a dead updater pages on its own.

Usage:  vps-update.py [--check] [--force]
        --check  report what would happen, change nothing, no heartbeat
        --force  run the weekly lane regardless of weekday
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CHECK = "--check" in sys.argv
FORCE = "--force" in sys.argv

AIOS_DIR = "/opt/aiostreams"
AIOS_COMPOSE = AIOS_DIR + "/compose.yaml"
AIOS_DB = AIOS_DIR + "/data/db.sqlite"
AIOS_REPO = "Viren070/AIOStreams"
AIOS_IMAGE = "ghcr.io/viren070/aiostreams"

LLM_DIR = "/opt/llm-proxy"
LLM_PROBE_MODEL = "deepseek-v4.1-flash:1017"
LLM_PROBE_ALIAS = "hermes"
LLM_IMAGES = ("ghcr.io/berriai/litellm:main-stable", "postgres:17-alpine")

GOTIFY_URL = "https://gotify.mysweetpea.cc/message"
GOTIFY_LAN = "http://192.168.20.210/message"          # traefik over NetBird, Host header supplies routing
GOTIFY_KEY = "/opt/vps-update/gotify.key"
KUMA_PUSH = "/opt/vps-update/kuma_push.url"
UA = "Mozilla/5.0 (X11; Linux x86_64) vps-update/1.0"  # Cloudflare challenges python-urllib's default UA
SNAP_KEEP = 2


def log(msg):
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def sh(cmd, cwd=None, timeout=600):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def readf(path):
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        return ""


def notify(title, message, prio=4):
    key = readf(GOTIFY_KEY)
    if not key:
        log(f"NOTIFY(no key) {title}: {message}")
        return
    body = json.dumps({"title": title, "message": message, "priority": prio}).encode()
    for url, extra in ((GOTIFY_URL, {}), (GOTIFY_LAN, {"Host": "gotify.mysweetpea.cc"})):
        req = urllib.request.Request(
            url, data=body,
            headers={"X-Gotify-Key": key, "Content-Type": "application/json", "User-Agent": UA, **extra},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            log(f"notify p{prio} via {'lan' if extra else 'public'}: {title}")
            return
        except Exception as e:
            log(f"notify via {'lan' if extra else 'public'} failed: {e}")
    log(f"NOTIFY FAILED (all transports) {title}")


def kuma_push(msg):
    url = readf(KUMA_PUSH)
    if not url:
        return
    m = re.match(r"https://([^/]+)(/.*)", url)
    lan = f"http://192.168.20.210{m.group(2)}" if m else ""
    full = lambda base: base + "?status=up&msg=" + urllib.parse.quote(msg[:220]) + "&ping="
    for base, extra in ((url, {}), (lan, {"Host": m.group(1)} if m else {})):
        if not base:
            continue
        try:
            req = urllib.request.Request(full(base), headers={"User-Agent": UA, **extra})
            urllib.request.urlopen(req, timeout=15)
            log("kuma push ok")
            return
        except Exception as e:
            log(f"kuma push failed ({base}): {e}")


def vtuple(tag):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def latest_release(repo):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest", headers={"User-Agent": "vps-update"}
    )
    return json.load(urllib.request.urlopen(req, timeout=20))["tag_name"]


def healthy(name, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, out = sh("docker inspect -f '{{.State.Health.Status}}' " + name)
        if out.strip() == "healthy":
            return True
        time.sleep(5)
    return False


def rotate_snapshots():
    snaps = sorted(
        (os.path.join(AIOS_DIR + "/data", f) for f in os.listdir(AIOS_DIR + "/data")
         if f.startswith("db.sqlite.pre-")),
        key=os.path.getmtime, reverse=True,
    )
    for old in snaps[SNAP_KEEP:]:
        os.remove(old)


# ---------------------------------------------------------------- lane A
def lane_aiostreams():
    txt = open(AIOS_COMPOSE).read()
    m = re.search(re.escape(AIOS_IMAGE) + r":([\w.\-]+)", txt)
    if not m:
        return "AIOStreams: image pin not found in compose - skipped"
    cur = m.group(1)
    try:
        latest = latest_release(AIOS_REPO)
    except Exception as e:
        return f"AIOStreams {cur}: release check failed ({e})"
    if not (vtuple(latest) and vtuple(cur) and vtuple(latest) > vtuple(cur)):
        return f"AIOStreams {cur} (latest {latest})"
    if CHECK:
        return f"AIOStreams {cur} -> {latest} (would update)"

    log(f"updating AIOStreams {cur} -> {latest}")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    snap = f"{AIOS_DB}.pre-{latest}"
    src = sqlite3.connect(AIOS_DB)
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    shutil.copy2(AIOS_COMPOSE, f"{AIOS_COMPOSE}.bak-{ts}")
    open(AIOS_COMPOSE, "w").write(txt.replace(f"{AIOS_IMAGE}:{cur}", f"{AIOS_IMAGE}:{latest}", 1))

    rc, out = sh("docker compose pull aiostreams && docker compose up -d aiostreams", cwd=AIOS_DIR)
    ok = rc == 0 and healthy("aiostreams")
    if ok:
        _, code = sh('curl -sf -o /dev/null -w "%{http_code}" --max-time 20 https://aiostreams.mysweetpea.cc/')
        ok = code.strip() == "200"
    if ok:
        rotate_snapshots()
        notify(f"VPS: AIOStreams {cur} -> {latest}",
               f"Updated, healthy, public 200. Rollback snapshot: {snap}", 4)
        return f"AIOStreams updated {cur} -> {latest}"

    log(f"{latest} failed its gate - rolling back to {cur}")
    sh("docker compose stop aiostreams", cwd=AIOS_DIR)
    open(AIOS_COMPOSE, "w").write(txt)
    shutil.copy2(snap, AIOS_DB)
    sh("docker compose up -d aiostreams", cwd=AIOS_DIR)
    back = healthy("aiostreams")
    notify(f"VPS: AIOStreams {latest} FAILED - rolled back to {cur}",
           f"Health gate failed on {latest}; compose + DB restored. Old version healthy: {back}. "
           f"Pin stays on {cur} until a later release passes.", 8)
    return f"AIOStreams {latest} failed, rolled back to {cur}"


# ---------------------------------------------------------------- lane B
def image_ids(refs):
    _, out = sh('docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}"')
    got = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in refs:
            got[parts[0]] = parts[1]
    return got


def llm_healthy():
    return healthy("llm-proxy-litellm-1", 180) and healthy("llm-proxy-db-1", 120)


def llm_probe():
    key = ""
    try:
        key = json.load(open(LLM_DIR + "/keys.json"))[LLM_PROBE_ALIAS]
    except Exception:
        for line in readf(LLM_DIR + "/.env").splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                key = line.split("=", 1)[1]
    if not key:
        log("llm probe: no key found")
        return False
    body = json.dumps(
        {"model": LLM_PROBE_MODEL, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    ).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        r = json.load(urllib.request.urlopen(req, timeout=90))
        return bool(r.get("choices"))
    except Exception as e:
        log(f"llm probe failed: {e}")
        return False


def lane_llm():
    if datetime.now(timezone.utc).weekday() != 6 and not FORCE:
        return "llm-proxy: weekly lane (Sundays), skipped"
    old = image_ids(LLM_IMAGES)
    if len(old) != len(LLM_IMAGES):
        return "llm-proxy: current image refs not found - skipped"
    if CHECK:
        return "llm-proxy: would pull and compare digests"

    rc, out = sh("docker compose pull", cwd=LLM_DIR)
    if rc != 0:
        return f"llm-proxy: pull failed ({out[-160:]})"
    new = image_ids(LLM_IMAGES)
    changed = [r for r in LLM_IMAGES if new.get(r) != old.get(r)]
    if not changed:
        return "llm-proxy: images already current"
    log("llm-proxy image change: " + ", ".join(changed))
    sh("docker compose up -d", cwd=LLM_DIR)
    ok = llm_healthy() and llm_probe()
    if ok:
        sh("docker image prune -f")
        notify("VPS: llm-proxy images refreshed",
               "Updated: " + ", ".join(c.split("/")[-1] for c in changed) + ". Health + model probe passed.", 4)
        return "llm-proxy updated (" + ", ".join(c.split(":")[-1] for c in changed) + ")"

    log("llm-proxy update failed its gate - restoring previous images")
    for ref, ident in old.items():
        sh(f"docker tag {ident} {ref}")
    sh("docker compose up -d --force-recreate --pull never", cwd=LLM_DIR)
    back = llm_healthy() and llm_probe()
    notify("VPS: llm-proxy update FAILED - rolled back",
           "New image failed health/model probe; previous images restored (serving: " + str(back) + "). "
           "Will retry on next weekly run.", 8)
    return "llm-proxy update failed, rolled back"


# ---------------------------------------------------------------- lane C
def lane_report():
    _, apt = sh("apt-get -s -q upgrade 2>/dev/null | grep -c '^Inst'")
    reboot = "reboot pending" if os.path.exists("/var/run/reboot-required") else "no reboot"
    _, disk = sh("df --output=pcent / | tail -1")
    return f"apt {apt.strip() or '?'} pending, {reboot}, disk {disk.strip()}"


def main():
    results = []
    for name, fn in (("AIOStreams", lane_aiostreams), ("llm-proxy", lane_llm), ("report", lane_report)):
        try:
            r = fn()
            log(f"{name}: {r}")
            results.append(r)
        except Exception as e:
            log(f"{name}: CRASHED {e}")
            results.append(f"{name} lane crashed: {e}")
            if not CHECK:
                notify(f"VPS: {name} lane crashed", str(e), 8)
    if not CHECK:
        kuma_push(" | ".join(results))


if __name__ == "__main__":
    main()
