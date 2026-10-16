#!/usr/bin/env python3
"""Taste rows for the Moonfin home — per-user genre union rows.

Signals: resume + played items (both carry Genres). Top-2 genres become
pluginDynamic genre rows ("<Genre> for You"), spliced into the user's
homeSections AFTER 'playlists' in their moonbase-lite user file.

- Layout base = /opt/moonbase-lite/data/defaults.json homeSections (edit that
  to change the fleet row list). moonbase-lite strips homeSections from device
  settings pushes, so the stored list is authoritative (devices adopt at boot).
- aios scopes per-user data BY TOKEN: each user needs their own login. Owner
  creds = warmer creds file; personas (when provisioned) = data/pins.json.
- pluginDynamic rows carry a serverId; with multi-server OFF (current fleet
  state) the client keeps any plugin row regardless of serverId
  (home_view_model._belongsToThisServer), so one constant works for both the
  bare and mount lanes. If multi-server is ever enabled, bare-lane clients
  still match SERVER_ID.

usage: moonfin-taste.py [--dry-run] [--selftest]
cron:  root 10 7 * * *   (after moonfin-rotate.py)
"""
import argparse
import hashlib
import json
import os
import pwd
import sys
import time
import urllib.request
from collections import Counter

BASE = "https://aiostreams.mysweetpea.cc/jellyfin"
SERVER_ID = "https://aiostreams.mysweetpea.cc/jellyfin"
DATA = "/opt/moonbase-lite/data"
CREDS = "/root/.aiostreams-warmer-creds.json"
UA = "Moonfin-Taste/1.0"
MIN_SIGNALS = 3
TOP_N = 2


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def call(path, token=None, method="GET", body=None, timeout=25):
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", 'MediaBrowser Token="%s"' % token)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def login(name, pw):
    d = call("/Users/AuthenticateByName", method="POST",
             body={"Username": name, "Pw": pw})
    return d.get("AccessToken") or d.get("accessToken") or ""


# --- signals ---------------------------------------------------------------

def genre_counts(token, uid):
    counts = Counter()
    for path in (f"/Users/{uid}/Items/Resume?Limit=50&Fields=Genres",
                 f"/Items?UserId={uid}&Filters=IsPlayed&Recursive=true"
                 "&IncludeItemTypes=Movie,Series&Fields=Genres&Limit=200"):
        try:
            items = call(path, token).get("Items") or []
        except Exception as exc:
            log("signal fetch failed %s: %s" % (path.split("?")[0], exc))
            continue
        for it in items:
            for g in it.get("Genres") or []:
                counts[g] += 1
    return counts


# --- layout ----------------------------------------------------------------

def taste_rows(counts, genre_ids):
    rows = []
    picks = [g for g, _ in sorted(counts.items(),
                                  key=lambda kv: (-kv[1], kv[0]))][:TOP_N]
    for i, name in enumerate(picks):
        gid = genre_ids.get(name)
        if not gid:
            log("genre id missing for %r; skipped" % name)
            continue
        rows.append({"kind": "pluginDynamic", "type": "none", "enabled": True,
                     "order": 0, "serverId": SERVER_ID,
                     "pluginSource": "genres", "pluginSection": "taste_%d" % i,
                     "pluginAdditionalData": gid,
                     "pluginDisplayText": "%s for You" % name})
    return rows


def splice(base_hs, taste):
    """Base layout + taste rows after 'playlists', orders renumbered 1..N."""
    hs = [dict(h) for h in base_hs
          if not str(h.get("pluginSection") or "").startswith("taste_")]
    if taste:
        pos = len(hs)
        for i, h in enumerate(hs):
            if h.get("type") == "playlists":
                pos = i + 1
                break
        hs = hs[:pos] + taste + hs[pos:]
    for i, h in enumerate(hs):
        h["order"] = i + 1
    return hs


# --- user files (mirror of moonbase_lite.py: sha1(uid)[:16]) ----------------

def user_key(uid):
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:16]


def user_path(uid):
    return os.path.join(DATA, "users", user_key(uid) + ".json")


def pick_uid(me, cfg_uuid):
    cands = [str(me.get("Id") or ""), (cfg_uuid or "").replace("-", "")]
    for c in cands:
        if c and os.path.exists(user_path(c)):
            return c
    return cands[0]


def write_user(uid, hs):
    path = user_path(uid)
    doc = {}
    if os.path.exists(path):
        try:
            doc = json.load(open(path))
        except Exception:
            doc = {}
    for scope in ("global", "desktop", "mobile", "tv"):
        if not isinstance(doc.get(scope), dict):
            doc[scope] = {}
    for scope in ("desktop", "mobile", "tv"):
        # push-era leftovers must not override global at resolve time
        doc[scope].pop("homeSections", None)
    doc["global"]["homeSections"] = hs
    doc["schemaVersion"] = 1
    doc["lastUpdated"] = int(time.time() * 1000)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    try:
        ub = pwd.getpwnam("ubuntu")
        os.chown(path, ub.pw_uid, ub.pw_gid)  # shim runs as ubuntu
    except Exception:
        pass


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        base = [{"kind": "builtin", "type": t, "enabled": True, "order": i}
                for i, t in enumerate(
                    ["resume", "nextup", "playlists", "genres", "latestmedia"], 1)]
        counts = {"Horror": 5, "Comedy": 4, "Drama": 1}
        rows = taste_rows(counts, {"Horror": "gh", "Comedy": "gc"})
        hs = splice(base, rows)
        types = [h.get("pluginSection") or h.get("type") for h in hs]
        assert types == ["resume", "nextup", "playlists", "taste_0", "taste_1",
                         "genres", "latestmedia"], types
        assert [h["order"] for h in hs] == [1, 2, 3, 4, 5, 6, 7]
        assert rows[0]["pluginDisplayText"] == "Horror for You"
        assert rows[0]["pluginAdditionalData"] == "gh"
        hs2 = splice(hs, [])  # stale taste rows removed when signals gone
        assert len(hs2) == 5 and all(h.get("type") != "taste_0" for h in hs2)
        assert taste_rows({"Mystery": 9}, {}) == []  # unknown genre skipped
        print("selftest ok")
        return

    cf = json.load(open(CREDS))
    token = login(cf["uuid"], cf["password"])
    if not token:
        log("login failed")
        sys.exit(1)
    me = call("/Users/Me", token)
    uid = pick_uid(me, cf.get("uuid"))
    gmap = {g["Name"]: g["Id"]
            for g in call("/Genres?Limit=400", token).get("Items") or []}
    counts = genre_counts(token, uid)
    log("user %r signals: %s" % (me.get("Name"), counts.most_common(8)))
    base = (json.load(open(os.path.join(DATA, "defaults.json"))) or {}).get(
        "homeSections") or []
    rows = taste_rows(counts, gmap) if sum(counts.values()) >= MIN_SIGNALS else []
    hs = splice(base, rows)
    log("taste rows: %s" % [r["pluginDisplayText"] for r in rows])
    if a.dry_run:
        for h in hs:
            print(" ", h.get("order"), h.get("type") or h.get("pluginDisplayText"))
        return
    write_user(uid, hs)
    log("wrote %s (%d sections, %d taste rows)"
        % (user_path(uid), len(hs), len(rows)))


if __name__ == "__main__":
    main()
