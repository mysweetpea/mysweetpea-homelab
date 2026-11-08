#!/usr/bin/env python3
"""update-sentinel — detect ImageUpdater bumps, health-verify, auto-rollback bad ones.

Runs as a CronJob pod (monitoring ns). Each invocation:
  1. Load state from ConfigMap (cursor + rollback ledger).
  2. Read ImageUpdater CR status.recentUpdates.
  3. For each new entry: locate values.yaml tag key, verify current tag matches
     (else already reverted -> advance cursor), then health-verify with grace.
  4. Healthy -> Gotify OK notice. Unhealthy past grace -> git revert tag to the
     pre-update value + add ignoreTags entry (repo CR source AND live CR patch)
     + push + notify + ledger entry.
Safety: DRY_RUN env (no push/patch), per-alias cooldown, global 24h circuit breaker,
exclusion list, single-flight CronJob. State in ConfigMap makes re-fires idempotent.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ruamel.yaml import YAML

GIT_REPO_URL = os.environ.get("GIT_REPO_URL", "git@github.com:mysweetpea/mysweetpea-homelab.git")
SSH_KEY_FILE = os.environ.get("SSH_KEY_FILE", "/tmp/id_ed25519")
GOTIFY_URL = os.environ.get("GOTIFY_URL", "http://gotify.private.svc.cluster.local")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "")
# Cluster-internal stalls go to the autopilot (Holmes), not the phone. Empty = old
# behave (Gotify only). The dispatcher fail-opens to Gotify itself, and push() falls
# back here if the dispatcher is unreachable.
DISPATCH_URL = os.environ.get("DISPATCH_URL", "")
MIN_INFRA_AGE_S = int(os.environ.get("MIN_INFRA_AGE_S", "180"))
POD_NS = os.environ.get("POD_NS", "monitoring")
STATE_CM = os.environ.get("STATE_CM", "update-sentinel-state")
UPDATER_CR = os.environ.get("UPDATER_CR", "homelab-image-updater")
UPDATER_NS = os.environ.get("UPDATER_NS", "argocd")
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
HEALTH_GRACE_S = int(os.environ.get("HEALTH_GRACE_S", "420"))
# Apps with a k8s readiness probe: k8s itself proves unhealthiness (pod never
# Ready) before we fire, so a short grace suffices. Unprobed apps keep the long
# grace for heavyweight boot variance (e.g. jellyfin plugin stack).
HEALTH_GRACE_S_PROBED = int(os.environ.get("HEALTH_GRACE_S_PROBED", "120"))
HEALTH_GRACE_S_DEFAULT = int(os.environ.get("HEALTH_GRACE_S_DEFAULT", "420"))
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "30"))
PER_APP_COOLDOWN_H = float(os.environ.get("PER_APP_COOLDOWN_H", "24"))
MAX_ROLLBACKS_24H = int(os.environ.get("MAX_ROLLBACKS_24H", "3"))
ROLLBACK_EXCLUDE = {a.strip() for a in os.environ.get("ROLLBACK_EXCLUDE", "").split(",") if a.strip()}
REPO_DIR = Path("/tmp/repo")
HEALTH_MAP_PATH = Path(os.environ.get("HEALTH_MAP_PATH", "/config/health-map.json"))

GIT_ENV_BASE = {
    "GIT_SSH_COMMAND": f"ssh -i {SSH_KEY_FILE} -o StrictHostKeyChecking=no "
                       "-o UserKnownHostsFile=/dev/null -o BatchMode=yes",
    "GIT_AUTHOR_NAME": "update-sentinel",
    "GIT_AUTHOR_EMAIL": "sentinel@mysweetpea.local",
    "GIT_COMMITTER_NAME": "update-sentinel",
    "GIT_COMMITTER_EMAIL": "sentinel@mysweetpea.local",
    "HOME": "/tmp",  # git refuses to guess identity/email without a writable HOME
}

yaml_rt = YAML()
yaml_rt.preserve_quotes = True

FAIL_REASONS = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff",
                "CreateContainerConfigError"}
# Waiting states that are INFRA (volume attach / scheduling), never a bad image.
INFRA_WAITING = {"ContainerCreating", "Pending"}
# Aliases whose verified push is ledger-only (chatty upstreams, e.g. searxng
# publishing several builds/day).
NOTIFY_QUIET_ALIASES = {a.strip() for a in os.environ.get("NOTIFY_QUIET_ALIASES", "").split(",") if a.strip()}
# msp-sentinel-v2


def log(level, msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {level} {msg}",
          flush=True)


def sh(cmd, timeout=60, check=False, env_extra=None, input=None, cwd=None):
    env = dict(os.environ)
    env.update(GIT_ENV_BASE)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                       text=True, timeout=timeout, env=env, input=input, cwd=cwd)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed rc={r.returncode}: {cmd}\n{r.stderr[-400:]}")
    return r



def _has_readiness_probe(app: dict) -> bool:
    """True if the app's main workload pod template has any readinessProbe.
    Probed apps get the short grace: k8s already proved unhealthiness."""
    kind = app.get("workload_kind"); name = app.get("workload"); ns = app.get("ns")
    if not (kind and name and ns):
        return False
    res = f"deploy/{name}" if kind == "Deployment" else (f"sts/{name}" if kind == "StatefulSet" else None)
    if not res:
        return False
    jp = "{.spec.template.spec.containers[*].readinessProbe.httpGet.path}"
    r = sh(["kubectl", "get", res, "-n", ns, "-o", f"jsonpath={jp}"], timeout=30)
    return bool((r.stdout or "").strip())

def notify(title, message, priority):
    """Returns True on confirmed delivery; False otherwise (skipped or failed)."""
    if not GOTIFY_TOKEN:
        log("WARN", "no GOTIFY_TOKEN; skipping notify")
        return False
    body = json.dumps({"title": title[:200], "message": message[:1500],
                       "priority": int(priority)}).encode()
    req = urllib.request.Request(
        f"{GOTIFY_URL}/message?token={GOTIFY_TOKEN}", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        log("INFO", f"notified: {title}")
        return True
    except (urllib.error.URLError, OSError) as e:
        log("WARN", f"notify failed: {e}")
        return False


# ---------------------------------------------------------------- state (ConfigMap)

def cm_manifest(state):
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": STATE_CM, "namespace": POD_NS},
        "data": {"state.json": json.dumps(state)},
    }


def load_state():
    r = sh(["kubectl", "get", "cm", STATE_CM, "-n", POD_NS, "-o", "json"], timeout=30)
    if r.returncode != 0:
        log("INFO", "state ConfigMap absent; creating empty")
        state = {"cursor": None, "rollbacks": [], "verified": {}}
        save_state(state)
        return state
    data = json.loads(r.stdout)
    raw = data.get("data", {}).get("state.json", "{}")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        state = {"cursor": None, "rollbacks": [], "verified": {}}
    state.setdefault("cursor", None)
    state.setdefault("rollbacks", [])
    state.setdefault("verified", {})
    state.setdefault("notified", {})
    return state


def save_state(state):
    m = cm_manifest(state)
    r = sh(["kubectl", "apply", "-f", "-"], input=json.dumps(m), timeout=30)
    if r.returncode != 0:
        log("ERROR", f"state save failed: {r.stderr[:200]}")


def run_input(args, payload, timeout=30, check=True):
    r = subprocess.run(args, input=payload, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd rc={r.returncode}: {' '.join(args)}\n{r.stderr[-300:]}")
    return r


# ---------------------------------------------------------------- updater status

def read_recent_updates():
    r = sh(["kubectl", "get", "imageupdaters.argocd-image-updater.argoproj.io", UPDATER_CR,
            "-n", UPDATER_NS, "-o", "json"], timeout=30, check=True)
    cr = json.loads(r.stdout)
    updates = cr.get("status", {}).get("recentUpdates") or []
    return updates, cr


def updater_config_for_alias(cr, alias):
    """Return (appRef dict, image entry dict) for this alias from the CR spec."""
    for ref in cr.get("spec", {}).get("applicationRefs", []):
        for img in ref.get("images", []):
            if img.get("alias") == alias:
                return ref, img
    return None, None


# ---------------------------------------------------------------- git helpers

def ensure_repo():
    if (REPO_DIR / ".git").is_dir():
        sh(["git", "-C", str(REPO_DIR), "fetch", "origin", "main", "--quiet"],
           timeout=120, check=True)
        sh(["git", "-C", str(REPO_DIR), "checkout", "--force", "origin/main",
            "--quiet"], timeout=60, check=True)
    else:
        REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=False)
        sh(["git", "clone", "--depth", "50", GIT_REPO_URL, str(REPO_DIR)],
           timeout=180, check=True)


def find_values_path(ref, img):
    """Resolve the values.yaml path for an alias from CR writeBackTarget.

    Pure-Python walk (busybox grep has no --include; keep the pod deps minimal)."""
    target = (ref.get("writeBackConfig", {}).get("gitConfig", {})
                 .get("writeBackTarget", ""))
    rel = target.split("helmvalues:", 1)[-1]
    name_pattern = ref.get("namePattern", "")
    for cand in REPO_DIR.glob("apps/*/*/application.yaml"):
        try:
            text = cand.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip() == f"name: {name_pattern}":
                # writeBackTarget is normally repo-relative ("apps/..."). App-of-apps
                # style charts (hindsight) use chart-relative paths ("../values.yaml"
                # from apps/<zone>/<app>/chart) — resolve those against the chart dir.
                if rel.startswith(".."):
                    p = (cand.parent / "chart" / rel).resolve()
                    if not p.is_file():
                        p = (cand.parent / rel).resolve()
                else:
                    p = (REPO_DIR / rel).resolve()
                if p.is_file():
                    return p
                break
    raise RuntimeError(f"no application.yaml named {name_pattern} has values at {rel}")


def get_tag_at_path(path, dotted_key):
    data = yaml_rt.load(Path(path).read_text())
    cur = data
    for part in dotted_key.split("."):
        cur = cur[part]
    return str(cur)


def set_tag_at_path(path, dotted_key, value):
    data = yaml_rt.load(Path(path).read_text())
    cur = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = str(value)
    buf = __import__("io").StringIO()
    yaml_rt.dump(data, buf)
    Path(path).write_text(buf.getvalue())


def find_updater_commit(values_path_rel):
    r = sh(["git", "-C", str(REPO_DIR), "log", "--format=%H %s", "-30", "--",
            values_path_rel], timeout=30, check=True)
    for line in r.stdout.splitlines():
        sha, _, subject = line.partition(" ")
        if re.search(r"automatic update of", subject, re.I):
            return sha, subject
    return None, None


def previous_tag(values_path_rel, dotted_key, sha):
    r = sh(["git", "-C", str(REPO_DIR), "show", f"{sha}^:{values_path_rel}"],
           timeout=30, check=True)
    import io
    data = yaml_rt.load(io.StringIO(r.stdout))
    cur = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        cur = cur[part]
    return str(cur[parts[-1]])


# ---------------------------------------------------------------- health gates

def k8s_gate(app, expect_tag=None):
    """Health gate. When expect_tag is set (update verification), the
    pod running THAT tag is the source of truth — "any pod ready" is
    meaningless during rollouts (the old pod can mask a broken new
    one, and a maxSurge=0 gap looks like downtime).
    Returns (ok, why, cls) where cls:
      ok        — healthy
      pending   — new-version pod not created yet (ArgoCD lag; keep waiting)
      infra     — new pod stuck ContainerCreating/Pending (volume attach)
      bad-image — CrashLoop/ImagePull/Failed on the new pod
      app-broken — new pod Running but probes/HTTP failing
      query     — kubectl errors (transient; treat as pending)
    """
    ns, kind, name = app["ns"], app.get("workload_kind", "Deployment"), app.get("workload", "")
    alias = app["_alias"]
    if kind == "DaemonSet" and not name:
        name = alias
    if not name:
        name = alias
    r = sh(["kubectl", "get", kind, name, "-n", ns, "-o", "json"], timeout=30)
    if r.returncode != 0:
        return False, f"{kind}/{name} query failed: {r.stderr[:120]}", "query"
    w = json.loads(r.stdout)
    # pod-level classification is the source of truth for update health
    sel = (w.get("spec", {}).get("selector", {}) or {}).get("matchLabels", {})
    if not sel:
        return True, "k8s ok (no selector)", "ok"
    sel_arg = ",".join(f"{k}={v}" for k, v in sel.items())
    r = sh(["kubectl", "get", "pods", "-n", ns, "-l", sel_arg, "-o", "json"],
           timeout=30)
    if r.returncode != 0:
        return False, "pod query failed", "query"
    pods = json.loads(r.stdout).get("items", [])
    new_pod = None
    for pod in pods:
        found = None
        for cs in pod.get("status", {}).get("containerStatuses", []):
            img = cs.get("image", "")
            tag = img.rsplit(":", 1)[-1].split("@", 1)[0] if ":" in img else ""
            if ":" in img and "/" not in tag and expect_tag and tag == expect_tag:
                found = cs
                break
        if found is not None:
            new_pod = (pod, found)
            break
    if expect_tag and new_pod is None:
        return False, (f"no pod running {expect_tag} yet "
                       "(rollout pending — ArgoCD lag or waiting slot)"), "pending"
    if new_pod:
        pod, cs = new_pod
        pname = pod["metadata"]["name"]
        if pod.get("status", {}).get("phase") == "Failed":
            return False, f"new-version pod {pname} phase Failed", "bad-image"
        wait = (cs.get("state", {}).get("waiting") or {}).get("reason", "")
        if wait in FAIL_REASONS:
            return False, f"new pod {cs.get('name')}: {wait}", "bad-image"
        if wait in INFRA_WAITING:
            started = pod.get("status", {}).get("startTime", "?")
            return False, (f"new pod {pname} stuck in {wait} since {started} "
                           "(volume attach / infra)"), "infra"
        if not cs.get("ready", False):
            return False, f"new-version pod {pname} not Ready (probes failing)", "app-broken"
        return True, f"new-version pod {pname} Ready", "ok"
    return True, "k8s ok", "ok"


def http_gate(app):
    entry = app.get("http")
    if entry is None:
        return True, "no http gate"
    if app.get("tcp"):
        ok_all, why = True, []
        for t in app["tcp"]:
            host = f"{t['svc']}.{app['ns']}.svc.cluster.local"
            r = sh(["curl", "-s", "-m", "5", "-o", "/dev/null",
                    f"telnet://{host}:{t['port']}"], timeout=10)
            # busybox curl: rc 28 = connected until timeout (success), 7 = connect fail
            if r.returncode not in (0, 28):
                ok_all = False
                why.append(f"{t['svc']}:{t['port']} closed(rc={r.returncode})")
        return (ok_all, "; ".join(why) or "tcp ok")
    code = (app.get("expect") or [200])
    url = entry if entry.startswith("http") else (
        f"http://{app['svc']}.{app['ns']}.svc.cluster.local:{app['port']}{entry}")
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8"]
    if app.get("follow"):
        cmd.append("-L")
    for k, v in (app.get("headers") or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    r = sh(cmd, timeout=15)
    got = r.stdout.strip()
    if got in {str(c) for c in code}:
        return True, f"http {got} ok"
    return False, f"http got {got!r}, expect {code}"


def healthy(app, expect_tag=None):
    ok, why, cls = k8s_gate(app, expect_tag)
    if not ok:
        return False, why, cls
    ok2, why2 = http_gate(app)
    if not ok2:
        return False, why2, "app-broken"
    return True, why2, "ok"


def push(title, message, priority):
    """Sink for cluster-internal faults: hand them to the autopilot, fall back to Gotify
    if the dispatcher is down. Same contract as notify() — True = delivered somewhere."""
    if DISPATCH_URL:
        body = json.dumps({"source": "update-sentinel", "status": "firing",
                           "title": title[:200], "message": message[:1500]}).encode()
        req = urllib.request.Request(DISPATCH_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            log("INFO", f"dispatched to autopilot: {title}")
            return True
        except (urllib.error.URLError, OSError) as e:
            log("WARN", f"dispatch failed ({e}); falling back to gotify")
    return notify(title, message, priority)


def notify_once(state, key, hours, title, message, priority, sink=None):
    """Dedupe: only push when this key has not been pushed within `hours`.
    Records intent in state (persisted by the caller via save_state)."""
    now = datetime.now(timezone.utc)
    seen = state.setdefault("notified", {})
    prev = seen.get(key)
    if prev:
        try:
            if now - datetime.fromisoformat(prev) < timedelta(hours=hours):
                return False
        except ValueError:
            pass
    seen[key] = now.isoformat()
    cutoff = now - timedelta(days=2)
    for k in [k for k, v in seen.items()
              if _parse_ts(v) < cutoff]:
        del seen[k]
    return (sink or notify)(title, message, priority)


def _parse_ts(v):
    try:
        return datetime.fromisoformat(v)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc) - timedelta(days=99)


# ---------------------------------------------------------------- rollback

def add_ignore_tags_repo(alias, bad_tag):
    """Sed-style minimal-diff insert into the ImageUpdater CR source. Full-file ruamel
    re-serialization churns ~1200 lines of unrelated formatting — never dump this file."""
    path = REPO_DIR / "apps/infra/argocd-image-updater/values.yaml"
    text = path.read_text()
    escaped = re.escape(bad_tag)
    m = re.search(rf"(?m)^([ \t]*)- alias: {re.escape(alias)}[ \t]*$\n?", text)
    if not m:
        return False, f"alias {alias} not found in ImageUpdater CR source"
    seg_end = m.end()
    tail = text[seg_end:]
    cm = re.search(r"(?m)^([ \t]*)commonUpdateSettings:[ \t]*$\n?", tail)
    if not cm:
        return False, "commonUpdateSettings not found after alias"
    cm_indent = len(cm.group(1))
    cm_abs_end = seg_end + cm.end()
    after_cm = text[cm_abs_end:]
    nxt = re.search(r"(?m)^([ \t]*)[A-Za-z-][\w-]*[ \t]*:", after_cm)
    block_end = cm_abs_end + (nxt.start() if nxt and len(nxt.group(1)) <= cm_indent
                              else len(after_cm))
    block = text[cm_abs_end:block_end]
    it = re.search(r"(?m)^([ \t]*)ignoreTags:[ \t]*$\n((?:[ \t]+-[^\n]*\n)*)", block)
    if re.search(rf"(?m)^[ \t]+-[ '\"]*{escaped}[ '\"]*\s*$", block):
        return False, "tag already in ignoreTags"
    if it:
        ins_at = cm_abs_end + it.end()
        ind = it.group(1) + "  "
        text = text[:ins_at] + f"{ind}- '{escaped}'\n" + text[ins_at:]
    else:
        ind = cm.group(1) + "  "
        text = (text[:cm_abs_end] + f"{ind}ignoreTags:\n{ind}  - '{escaped}'\n"
                + text[cm_abs_end:])
    path.write_text(text)
    return True, escaped


def patch_live_cr(cr, alias, bad_tag):
    import copy
    live = copy.deepcopy(cr)
    escaped = re.escape(bad_tag)
    touched = False
    for ref in live["spec"]["applicationRefs"]:
        for img in ref.get("images", []):
            if img.get("alias") != alias:
                continue
            settings = img.setdefault("commonUpdateSettings", {})
            tags = settings.get("ignoreTags") or []
            if escaped not in tags:
                tags.append(escaped)
                settings["ignoreTags"] = tags
                touched = True
    if not touched:
        return False
    payload = json.dumps({"applicationRefs": live["spec"]["applicationRefs"]})
    run_input(["kubectl", "patch", "imageupdaters.argocd-image-updater.argoproj.io", UPDATER_CR,
               "-n", UPDATER_NS, "--type=merge", "-p", payload], None, timeout=30)
    return True


def git_commit_push(alias, new_version, old_tag):
    sh(["git", "-C", str(REPO_DIR), "add", "-A"], timeout=30, check=True)
    st = sh(["git", "-C", str(REPO_DIR), "status", "--porcelain"], timeout=30)
    if not st.stdout.strip():
        log("INFO", "no diff to commit")
        return False
    msg = f"sentinel: rollback {alias} {new_version} -> {old_tag} (unhealthy past grace)"
    sh(["git", "-C", str(REPO_DIR), "commit", "-m", msg], timeout=30, check=True)
    r = sh(["git", "-C", str(REPO_DIR), "push", "origin", "main"], timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"push failed: {r.stderr[-300:]}")
    return True


def git_log_scan(state, cr):
    """Durable detection: scan origin/main git log for 'build: automatic update of <app>'
    commits since the cursor. Immune to recentUpdates per-pass eviction."""
    found = []
    if not (REPO_DIR / ".git").is_dir():
        return found
    since = state.get("cursor")
    args = ["git", "-C", str(REPO_DIR), "log", "origin/main", "--format=%H|%s|%cI", "-20"]
    if since:
        args += [f"--since={since}"]
    args += ["--"]
    r = sh(args, timeout=60)
    if r.returncode != 0:
        return found
    # map values.yaml path -> alias
    path_to_alias = {}
    for alias, app in HEALTH_MAP["apps"].items():
        ref, img = updater_config_for_alias(cr, alias)
        if ref is None:
            continue
        target = (ref.get("writeBackConfig", {}).get("gitConfig", {})
                     .get("writeBackTarget", ""))
        rel = target.split("helmvalues:", 1)[-1]
        try:
            values_path = find_values_path(ref, img)
            path_to_alias[str(values_path.relative_to(REPO_DIR)).replace("\\", "/")] = (alias, img, dotted_tag(img), values_path)
        except (RuntimeError, OSError):
            continue
    for line in r.stdout.splitlines():
        try:
            sha, subject, when = line.split("|", 2)
        except ValueError:
            continue
        if "automatic update of" not in subject.lower():
            continue
        # files touched by this commit
        r2 = sh(["git", "-C", str(REPO_DIR), "show", "--name-only", "--format=", sha],
                timeout=30)
        for fpath in r2.stdout.splitlines():
            fpath = fpath.strip().replace("\\", "/")
            if fpath in path_to_alias:
                alias, img, dotted, values_path = path_to_alias[fpath]
                try:
                    new_tag = get_tag_at_path(values_path, dotted)
                except Exception:  # noqa: BLE001
                    continue
                vkey = f"{alias}@{new_tag}"
                if vkey in state.get("verified", {}):
                    continue  # already handled via recentUpdates path
                found.append({"alias": alias, "newVersion": new_tag,
                              "git_tag": new_tag,
                              "updatedAt": when})
    return found


def dotted_tag(img):
    return (img.get("manifestTargets", {}).get("helm", {}) or {}).get("tag", "")


def drift_scan(state, cr):
    """Catch updates the per-pass recentUpdates list evicted before a cycle read them.
    Compare live workload image tag vs git values tag for every managed app; any mismatch
    is treated as an update event (newVersion = live tag). Cheap: 1 kubectl get per app."""
    import re as _re
    found = []
    for alias, app in HEALTH_MAP["apps"].items():
        if alias in ROLLBACK_EXCLUDE:
            continue
        ref, img = updater_config_for_alias(cr, alias)
        if ref is None:
            continue
        dotted = (img.get("manifestTargets", {}).get("helm", {}) or {}).get("tag", "")
        if not dotted:
            continue
        try:
            values_path = find_values_path(ref, img)
        except (RuntimeError, OSError):
            continue
        try:
            git_tag = get_tag_at_path(values_path, dotted)
        except Exception:  # noqa: BLE001 — skip unreadable entries
            continue
        kind = app.get("workload_kind", "Deployment")
        name = app.get("workload") or alias
        r = sh(["kubectl", "get", kind, name, "-n", app["ns"],
                "-o", "json"], timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        w = json.loads(r.stdout)
        image_name = (img.get("imageName") or "").split("/")[-1]  # e.g. 'grafana'
        live_tag = None
        for c in w.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            cimg = c.get("image", "")
            base = cimg.split("/")[-1]                      # 'grafana/grafana:13.2.1' -> 'grafana:13.2.1'
            if base.split(":")[0] == image_name or image_name in base:
                live_tag = base.rsplit(":", 1)[-1] if ":" in base else "latest"
                break
        if not live_tag:
            continue  # managed container not found in this workload

        if live_tag != git_tag:
            found.append({"alias": alias, "newVersion": live_tag, "git_tag": git_tag,
                          "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return found


# ---------------------------------------------------------------- main cycle

def process_alias(alias, entry, state, cr):
    app = HEALTH_MAP["apps"].get(alias)
    if app is None and alias in HEALTH_MAP.get("subaliases", {}):
        app = HEALTH_MAP["apps"].get(HEALTH_MAP["subaliases"][alias])
    if app is None:
        notify("sentinel: unknown alias", f"{alias} updated to {entry['newVersion']} "
               "but has no health-map entry — not monitored", 4)
        return
    app["_alias"] = alias
    new_version = entry["newVersion"]

    if alias in ROLLBACK_EXCLUDE:
        notify("sentinel: update (excluded)", f"{alias} -> {new_version} "
               "detected; auto-rollback excluded for this app", 3)
        return

    ref, img = updater_config_for_alias(cr, alias)
    if ref is None:
        notify("sentinel: alias not in updater CR", alias, 4)
        return
    try:
        values_path = find_values_path(ref, img)
    except RuntimeError as e:
        notify("sentinel: values path error", str(e), 5)
        return
    dotted = (img.get("manifestTargets", {}).get("helm", {}) or {}).get("tag", "")
    if not dotted:
        notify("sentinel: no manifestTargets.tag", alias, 5)
        return
    current = get_tag_at_path(values_path, dotted)
    if current != new_version:
        log("INFO", f"{alias}: git tag {current} != {new_version} — already moved; skip")
        return

    # health verify with grace — the NEW version pod is what must go Ready
    grace_s = HEALTH_GRACE_S_PROBED if _has_readiness_probe(app) else HEALTH_GRACE_S_DEFAULT
    log("INFO", f"grace={grace_s}s (probed={grace_s == HEALTH_GRACE_S_PROBED})")
    deadline = time.time() + grace_s
    last_why = "not checked"
    last_cls = "unknown"
    infra_since = None
    while True:
        ok, why, cls = healthy(app, expect_tag=new_version)
        last_why, last_cls = why, cls
        if ok:
            vkey = f"{alias}@{new_version}"
            if state.setdefault("verified", {}).get(vkey):
                log("INFO", f"{alias} {new_version}: already verified (re-emitted update); skip notify")
            elif alias in NOTIFY_QUIET_ALIASES:
                log("INFO", f"{alias} {new_version}: verified (quiet alias — ledger only, no push)")
                state["verified"][vkey] = datetime.now(timezone.utc).isoformat()
            else:
                sent = notify(f"✅ {alias} {new_version} verified",
                              f"update healthy after verification: {why}", 3)
                if sent:
                    # record only on confirmed delivery; a failed send retries on the
                    # next re-emitted pass (at-least-once for the verified signal)
                    state["verified"][vkey] = datetime.now(timezone.utc).isoformat()
                    if len(state["verified"]) > 200:
                        for k2 in sorted(state["verified"], key=state["verified"].get)[:-200]:
                            del state["verified"][k2]
            return
        if last_cls == "infra":
            # volume-attach / scheduling stall — the IMAGE is fine; rolling back
            # cannot help and loses the update (the n8n 2.40.3 false rollback).
            # Transient attaches clear in ~2-3 min, so wait MIN_INFRA_AGE_S before
            # waking the autopilot — it costs an LLM call, not just a push.
            infra_since = infra_since or time.time()
            if time.time() - infra_since < MIN_INFRA_AGE_S and time.time() < deadline:
                log("INFO", f"{alias} infra stall ({why}); "
                            f"waiting {MIN_INFRA_AGE_S}s before dispatch")
                time.sleep(POLL_INTERVAL_S)
                continue
            break
        if time.time() >= deadline:
            break
        log("INFO", f"{alias} unhealthy ({cls}: {why}); grace poll…")
        time.sleep(POLL_INTERVAL_S)

    if last_cls == "infra":
        # Hand it to the autopilot, not the phone: this is the RWO multi-attach class
        # Holmes can clear (scale the old ReplicaSet to 0). push() falls back to Gotify
        # if the dispatcher is down, so the alert is never lost.
        workload = f"{app.get('workload_kind', 'Deployment')}/{app.get('workload') or alias} in ns {app['ns']}"
        notify_once(state, f"infra:{alias}", 6,
                    f"🧱 {alias} update stuck on infra (NOT rolling back)",
                    f"{last_why}\n"
                    f"workload: {workload}\n"
                    "The new image is fine — this is a cluster/volume issue "
                    "(RWO attach class). Rolling back cannot help. Clear it by scaling "
                    "the OLD ReplicaSet (the one still holding the volume) to 0.", 6,
                    sink=push)
        log("WARN", f"{alias}: infra stall, no rollback ({last_why})")
        return
    if last_cls == "pending":
        # new-version pod never materialized — ArgoCD/updater lag, not a bad
        # image. Old versions keep serving; a rollback would be a no-op or
        # worse (race with the still-in-flight rollout).
        notify_once(state, f"pending:{alias}", 6,
                    f"⏳ {alias} rollout never materialized (NOT rolling back)",
                    f"{last_why}\n"
                    f"after {grace_s}s grace. Likely ArgoCD/updater lag; "
                    "will re-verify if the update re-fires.", 5)
        log("WARN", f"{alias}: rollout pending past grace, no rollback ({last_why})")
        return
    # last_cls in {bad-image, app-broken, unknown} → rollback is meaningful

    # still unhealthy -> rollback with breakers
    now = datetime.now(timezone.utc)
    recent_same = [rb for rb in state["rollbacks"] if rb["alias"] == alias
                   and now - _parse_ts(rb["ts"]) < timedelta(hours=PER_APP_COOLDOWN_H)]
    if recent_same:
        notify_once(state, f"cooldown:{alias}", 6,
                    f"⚠️ {alias} cooldown", f"still unhealthy after {new_version}, but a "
                    "rollback already ran within cooldown — MANUAL attention needed", 7)
        return
    recent_all = [rb for rb in state["rollbacks"]
                  if now - _parse_ts(rb["ts"]) < timedelta(hours=24)]
    if len(recent_all) >= MAX_ROLLBACKS_24H:
        notify_once(state, "circuit-breaker", 6,
                    "⛔ sentinel circuit breaker",
                    f"{MAX_ROLLBACKS_24H} rollbacks in 24h; {alias} still unhealthy on "
                    f"{new_version}. MANUAL INTERVENTION REQUIRED", 9)
        return

    sha, subject = find_updater_commit(str(values_path.relative_to(REPO_DIR)))
    if not sha:
        notify("sentinel: no updater commit", f"cannot locate updater commit for "
               f"{alias}; rollback aborted", 6)
        return
    old_tag = previous_tag(str(values_path.relative_to(REPO_DIR)), dotted, sha)

    if DRY_RUN:
        notify(f"[DRY-RUN] would roll back {alias}",
               f"{new_version} -> {old_tag}; reason: {last_why}", 7)
        log("WARN", f"[DRY-RUN] rollback {alias} {new_version} -> {old_tag} ({last_why})")
        return

    set_tag_at_path(values_path, dotted, old_tag)
    add_ignore_tags_repo(alias, new_version)
    git_commit_push(alias, new_version, old_tag)  # repo first: source of truth
    patch_live_cr(cr, alias, new_version)  # close re-bump window during ArgoCD lag
    state["rollbacks"].append({"ts": now.isoformat(), "alias": alias,
                               "tag": new_version, "restored": old_tag})
    notify(f"⛔ ROLLED BACK {alias} — manual update required",
           f"{alias} {new_version} was unhealthy ({last_why}); reverted to {old_tag}.\n"
           f"The broken tag {new_version} is pinned (ignoreTags in "
           f"apps/infra/argocd-image-updater/values.yaml) so the updater skips it.\n"
           f"TO RETRY LATER: ask Hermes to \"retry {alias} update\" — it removes the "
           f"pin and re-bumps; the sentinel re-verifies and rolls back again if still "
           f"broken. Upstream fixes also arrive automatically once a newer tag "
           f"publishes.", 8)


HEALTH_MAP = {}


def main():
    global HEALTH_MAP
    arg = sys.argv[1] if len(sys.argv) > 1 else "--once"
    HEALTH_MAP = json.loads(HEALTH_MAP_PATH.read_text())
    if arg == "--health-check":
        alias = sys.argv[2]
        app_def = HEALTH_MAP["apps"].get(alias)
        if app_def is None:
            app_def = HEALTH_MAP["apps"].get(HEALTH_MAP.get("subaliases", {}).get(alias))
        if app_def is None:  # sentinel-v2-fix2: unknown alias — clean error, not KeyError
            print(f"{alias}: UNKNOWN — no health-map entry")
            sys.exit(2)
        app = dict(app_def)
        app["_alias"] = alias
        ok, why, cls = healthy(app)
        print(f"{alias}: {'HEALTHY' if ok else 'UNHEALTHY'} [{cls}] — {why}")
        sys.exit(0 if ok else 1)

    state = load_state()
    updates, cr = read_recent_updates()
    ensure_repo()   # clone/fetch: needed by git-log scan, values lookups, and rollbacks
    if updates or state.get("drift_armed"):
        drift = git_log_scan(state, cr) + (drift_scan(state, cr) if cr else [])
    else:
        drift = []
    seen_keys = {f'{u.get("alias")}@{u.get("newVersion")}' for u in updates}
    seen_aliases = {u.get("alias") for u in updates}
    for d in drift:
        key = f'{d["alias"]}@{d["newVersion"]}'
        if d["alias"] in seen_aliases or key in seen_keys or key in state.get("verified", {}):
            continue
        log("INFO", f"git-log/drift caught {d['alias']}: -> {d['newVersion']} (missed by recentUpdates)")
        seen_keys.add(key)
        seen_aliases.add(d["alias"])
        updates.append(d)
    if not updates:
        log("INFO", "no recentUpdates or drift; done")
        return

    def ts_key(u):
        return u.get("updatedAt", "")
    updates_sorted = sorted(updates, key=ts_key)
    newest_ts = updates_sorted[-1]["updatedAt"]

    if not state["cursor"]:
        state["cursor"] = newest_ts
        state["drift_armed"] = True   # drift-scan starts NEXT cycle (baseline first)
        save_state(state)
        notify_once(state, "armed", 87600,
                    "Update Sentinel armed",
                    f"Baseline set at {newest_ts}. Managed apps: "
                    f"{len(HEALTH_MAP['apps'])}.", 3)
        log("INFO", f"armed at cursor {newest_ts}")
        return

    fresh = [u for u in updates_sorted if u["updatedAt"] > state["cursor"]]
    if not fresh:
        log("INFO", "no new updates since cursor")
        return
    log("INFO", f"processing {len(fresh)} new update(s)")
    for u in fresh:
        try:
            process_alias(u["alias"], u, state, cr)
        except Exception as e:  # noqa: BLE001 — one bad app must not kill the sweep
            log("ERROR", f"alias {u['alias']} failed: {e!r}")
            notify("sentinel: per-app error",
                   f"{u['alias']}: {e!r}", 6)
    state["cursor"] = newest_ts
    state["drift_armed"] = True
    # trim ledger
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    state["rollbacks"] = [rb for rb in state["rollbacks"]
                          if _parse_ts(rb["ts"]) > cutoff]
    save_state(state)


if __name__ == "__main__":
    main()
