#!/usr/bin/env python3
"""Per-member taste engine -> playlist rows for Moonfin.

Runs nightly on the VPS. For every user it builds, from that user's own watch
state, four rows and writes:

  /opt/moonbase-lite/data/users/rows-<sha1(uid)[:16]>.json   item lists
  /opt/moonbase-lite/data/users/<sha1(uid)[:16]>.json        home layout
                                                             (homeSections splice)

The moonbase-lite shim serves the rows file to the matching caller, so the
same playlist id returns a different list per member on every platform, no
client change.

Signals: played / resume / favorites -> genre + decade weights (recency decay).
Pool: the 27 genre listings (list shapes drop Genres) hydrated via ?Ids=.
No TMDB calls: features come from the server at one consistent granularity.

ponytail: genre+decade only. Add cast/keyword vectors if rows feel samey.

usage: moonfin-for-you.py [--dry-run] [--selftest]
"""

import argparse
import gzip
import hashlib
import json
import math
import os
import pwd
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://aiostreams.mysweetpea.cc/jellyfin"
CREDS = "/root/.aiostreams-warmer-creds.json"
DATA = "/opt/moonbase-lite/data"
PINS = os.path.join(DATA, "pins.json")
ROWS_JSON = os.path.join(DATA, "rows.json")
LOG = "/var/log/moonfin-for-you.log"

SID = "https://aiostreams.mysweetpea.cc/jellyfin"

ROW_SPEC = [
    ("msp-foryou", "Picked for You", 30),
    ("msp-because", "Because You Watched", 24),   # name completed with the seed title
    ("msp-deepcuts", "Deep Cuts for You", 24),
    ("msp-newforme", "New in Your Genres", 24),
]

F_ITEM = "Genres,ProductionYear,CommunityRating,UserData,Type,SeriesName,DateCreated"


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def http(url, token=None, method="GET", body=None, timeout=90):
    hdr = {"Accept-Encoding": "gzip"}
    if token:
        hdr["Authorization"] = 'MediaBrowser Token="%s"' % token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", "replace"))


def q(s):
    return urllib.parse.quote(s, safe="")


def items_of(doc):
    if isinstance(doc, list):   # /Users answers a bare array
        return doc
    return (doc or {}).get("Items") or []


def login(username, password):
    d = http(BASE + "/Users/AuthenticateByName", method="POST",
             body={"Username": username, "Pw": password})
    return d["AccessToken"], d["User"]["Id"]


# --- taste -------------------------------------------------------------------

def _decay(stamp, now):
    """Played 6 months ago counts half as much as played today."""
    try:
        t = time.mktime(time.strptime((stamp or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 1.0
    age_days = max(0.0, (now - t) / 86400.0)
    return 0.5 ** (age_days / 180.0)


def taste_profile(played, resume, favorites, now=None):
    """-> {'genres': {g: w}, 'decades': {d: w}} with weights normalised 0..1."""
    now = now or time.time()
    g, dec = {}, {}

    def add(item, weight):
        d = _decay(((item.get("UserData") or {}).get("LastPlayedDate")), now)
        for name in item.get("Genres") or []:
            g[name] = g.get(name, 0.0) + weight * d
        yr = item.get("ProductionYear")
        if isinstance(yr, int) and 1900 < yr < 2100:
            k = (yr // 10) * 10
            dec[k] = dec.get(k, 0.0) + weight * d

    for it in played:
        add(it, 1.0)
    for it in resume:
        add(it, 0.7)
    for it in favorites:
        add(it, 1.2)

    def norm(d):
        if not d:
            return {}
        top = max(d.values())
        return {k: v / top for k, v in d.items()} if top else {}

    return {"genres": norm(g), "decades": norm(dec)}


def score_item(item, taste, global_ids, played_ids):
    """Taste fit 0..1 plus decade/rating nudges. Played items are excluded."""
    iid = item.get("Id")
    if not iid or iid in played_ids:
        return None
    genres = item.get("Genres") or []
    if not genres:
        return None
    gw = taste["genres"]
    fit = sum(gw.get(x, 0.0) for x in genres) / float(len(genres))
    if fit <= 0.0:
        return 0.0
    yr = item.get("ProductionYear")
    dmatch = 0.0
    if isinstance(yr, int):
        dmatch = taste["decades"].get((yr // 10) * 10, 0.0)
    try:
        rating = float(item.get("CommunityRating") or 0.0)
    except (TypeError, ValueError):
        rating = 0.0
    rfac = max(0.0, min(1.0, (rating - 6.5) / 3.5)) if rating else 0.0
    return fit + 0.15 * dmatch + 0.15 * rfac


def jaccard(a, b):
    a, b = set(a or []), set(b or [])
    return len(a & b) / float(len(a | b)) if (a or b) else 0.0


def build_rows(taste, pool, played_ids, seed_item, global_ids, new_ids, limit_map):
    """-> [{'id','name','items'}] in ROW_SPEC order, deduped across rows."""
    used = set()
    sizes = dict(limit_map)
    rows = []

    scored = []
    for it in pool:
        s = score_item(it, taste, global_ids, played_ids)
        if s is not None and s > 0.0:
            scored.append((s, it))
    scored.sort(key=lambda p: p[0], reverse=True)

    def take(cands, rid, name, cap):
        out = []
        for _, it in cands:
            iid = it.get("Id")
            if iid in used or iid in played_ids:
                continue
            used.add(iid)
            out.append(iid)
            if len(out) >= cap:
                break
        if out:
            rows.append({"id": rid, "name": name, "items": out})
        return out

    # 1. Picked for You - straight taste ranking, movies and series interleaved
    movies = [(s, i) for s, i in scored if i.get("Type") == "Movie"]
    series = [(s, i) for s, i in scored if i.get("Type") == "Series"]
    mixed = []
    for a, b in zip(movies, series):
        mixed += [a, b]
    mixed += movies[len(series):] + series[len(movies):]
    take(mixed, "msp-foryou", "Picked for You", sizes["msp-foryou"])

    # 2. Because You Watched <seed> - nearest neighbours of the latest finish
    if seed_item:
        sg = seed_item.get("Genres") or []
        near = [p for p in sorted(
            ((jaccard(sg, it.get("Genres")), it) for it in pool),
            key=lambda p: p[0], reverse=True) if p[0] > 0]   # shared genre or skip
        name = "Because You Watched %s" % (seed_item.get("Name") or "It")
        take(near, "msp-because", name, sizes["msp-because"])

    # 3. Deep Cuts - good taste fit that the rotation rows never surface
    deep = [(s, i) for s, i in scored
            if i.get("Id") not in global_ids
            and (i.get("CommunityRating") or 0) >= 7.0]
    take(deep, "msp-deepcuts", "Deep Cuts for You", sizes["msp-deepcuts"])

    # 4. New in Your Genres - recent releases that match taste
    fresh = []
    for it in pool:
        if it.get("Id") in new_ids:
            s = score_item(it, taste, global_ids, played_ids) or 0.0
            if s > 0.0:
                fresh.append((s, it))
    fresh.sort(key=lambda p: ((p[1].get("ProductionYear") or 0), p[0]), reverse=True)
    take(fresh, "msp-newforme", "New in Your Genres", sizes["msp-newforme"])

    return rows


# --- data --------------------------------------------------------------------

def fetch_user_state(token, uid):
    played = items_of(http("%s/Users/%s/Items?Filters=IsPlayed&Recursive=true"
                           "&IncludeItemTypes=Movie,Series,Episode&SortBy=DatePlayed"
                           "&SortOrder=Descending&Limit=400&Fields=%s"
                           % (BASE, uid, q(F_ITEM)), token))
    resume = items_of(http("%s/UserItems/Resume?MediaTypes=Video&Limit=60&Fields=%s"
                           % (BASE, q(F_ITEM)), token))
    favs = items_of(http("%s/Users/%s/Items?Filters=IsFavorite&Recursive=true"
                         "&IncludeItemTypes=Movie,Series&Limit=200&Fields=%s"
                         % (BASE, uid, q(F_ITEM)), token))
    return played, resume, favs


def build_pool(token, uid, taste, extra_ids=()):
    """Loved-genre listings + the rotation rows -> ids, hydrated in batches.

    Listing shapes drop Genres; the ?Ids= batch shape carries them, so the
    listing is only used to discover ids (and to tag deep-cut candidates).
    ponytail: 6 genre queries (each ~2-4s on the single-threaded app). Widen if
    members complain the pool feels small.
    """
    wanted = [g for g in sorted(taste["genres"], key=taste["genres"].get,
                                reverse=True)[:6] if taste["genres"].get(g, 0) > 0]
    genres = [g for g in items_of(http(
        "%s/Genres?UserId=%s&Limit=40&IncludeItemTypes=Movie,Series&Fields=ItemCounts"
        % (BASE, uid), token)) if (g.get("Name") in wanted)]
    ids = [i for i in extra_ids if isinstance(i, str)]
    tag = {}
    for g in genres:
        gid, gname = g.get("Id"), g.get("Name")
        if not gid or not gname:
            continue
        try:
            doc = http("%s/Users/%s/Items?GenreIds=%s&Recursive=true"
                       "&IncludeItemTypes=Movie,Series&Limit=100&Fields=%s"
                       % (BASE, uid, q(gid), q(F_ITEM)), token)
        except Exception as exc:
            log("    genre %s failed: %s" % (gname, type(exc).__name__))
            continue
        for it in items_of(doc):
            iid = it.get("Id")
            if not iid:
                continue
            if iid not in tag:
                tag[iid] = set()
                ids.append(iid)
            tag[iid].add(gname)
    pool, seen = [], set()
    for x in range(0, len(ids), 100):
        chunk = ids[x:x + 100]
        try:
            doc = http("%s/Items?Ids=%s&Fields=%s"
                       % (BASE, ",".join(q(i) for i in chunk), q(F_ITEM)), token)
        except Exception as exc:
            log("    hydrate batch failed: %s" % type(exc).__name__)
            continue
        for it in items_of(doc):
            iid = it.get("Id")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            if not it.get("Genres"):
                it["Genres"] = sorted(tag.get(iid) or [])
            pool.append(it)
    return pool


def global_row_ids():
    try:
        doc = json.load(open(ROWS_JSON))
    except Exception:
        return set(), set()
    rows = doc.get("rows") or []
    all_ids = {i for r in rows for i in (r.get("items") or [])}
    new_ids = {i for r in rows if r.get("id") == "msp-newreleases"
               for i in (r.get("items") or [])}
    return all_ids, new_ids


# --- layout ------------------------------------------------------------------

def user_key(uid):
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:16]


def settings_path(uid):
    return os.path.join(DATA, "users", user_key(uid) + ".json")


def rows_path(uid):
    return os.path.join(DATA, "users", "rows-" + user_key(uid) + ".json")


def splice_home(base_hs, rows):
    """Insert our rows after 'favoriteepisodes' (where the old taste rows sat)."""
    ours = {"msp-foryou", "msp-because", "msp-deepcuts", "msp-newforme"}
    hs = [dict(h) for h in base_hs
          if str(h.get("pluginSection") or "") not in ("taste_0", "taste_1")
          and str(h.get("pluginAdditionalData") or "") not in ours]
    add = [{"kind": "pluginDynamic", "type": "none", "enabled": True,
            "serverId": SID, "pluginSource": "playlists",
            "pluginSection": "playlist", "pluginAdditionalData": r["id"],
            "pluginDisplayText": r["name"]} for r in rows]
    pos = len(hs)
    for i, h in enumerate(hs):
        if h.get("type") == "favoriteepisodes":
            pos = i + 1
            break
    hs = hs[:pos] + add + hs[pos:]
    for i, h in enumerate(hs):
        h["order"] = i + 1
    return hs


def _chown(path):
    try:
        ub = pwd.getpwnam("ubuntu")
        os.chown(path, ub.pw_uid, ub.pw_gid)
    except Exception:
        pass


def _atomic(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, path)
    _chown(path)


def write_rows(uid, rows):
    _atomic(rows_path(uid), {"generated": int(time.time()), "rows": rows})


def write_layout(uid, rows):
    path = settings_path(uid)
    doc = {}
    if os.path.exists(path):
        try:
            doc = json.load(open(path))
        except Exception:
            doc = {}
    # The fleet layout (moonfin-rotate.py -> defaults.json) is authoritative:
    # it carries the current rotating row ids, which change shape between
    # releases. Fall back to the user's own copy only if defaults is missing.
    fleet = []
    try:
        fleet = json.load(open(os.path.join(DATA, "defaults.json"))).get("homeSections") or []
    except Exception:
        fleet = []
    base = fleet or ((doc.get("global") or {}).get("homeSections") or [])
    for scope in ("global", "desktop", "mobile", "tv"):
        if not isinstance(doc.get(scope), dict):
            doc[scope] = {}
    for scope in ("desktop", "mobile", "tv"):
        doc[scope].pop("homeSections", None)
    doc["global"]["homeSections"] = splice_home(base, rows)
    doc["schemaVersion"] = 1
    doc["lastUpdated"] = int(time.time() * 1000)
    _atomic(path, doc)


# --- main --------------------------------------------------------------------

def run(dry_run=False):
    creds = json.load(open(CREDS))
    owner_user = creds.get("uuid") or creds.get("config_uuid")
    owner_pw = creds.get("password")
    owntok, owner_uid = login(owner_user, owner_pw)
    pins = {}
    try:
        pins = json.load(open(PINS)) or {}
    except Exception:
        pass
    users = items_of(http(BASE + "/Users", owntok))
    gids, newids = global_row_ids()
    log("run start: %d users, pool rows global=%d news=%d, dry=%s"
        % (len(users), len(gids), len(newids), dry_run))

    for u in users:
        uid, name = u.get("Id"), (u.get("Name") or "")
        if not uid:
            continue
        try:
            if uid == owner_uid:
                token = owntok
            else:
                entry = pins.get(name) or pins.get(name.lower()) or {}
                pin = entry.get("pin") if isinstance(entry, dict) else None
                if not pin:
                    log("  %-18s skip: no pin on file" % name)
                    continue
                token, _ = login(name, pin)
            played, resume, favs = fetch_user_state(token, uid)
            if not (played or resume or favs):
                log("  %-18s skip: no watch history yet" % name)
                continue
            taste = taste_profile(played, resume, favs)
            pool = build_pool(token, uid, taste, gids)
            played_ids = {i.get("Id") for i in played if i.get("Id")}
            seed = next((i for i in played if i.get("Genres")), None) or \
                next((i for i in resume if i.get("Genres")), None)
            limits = {rid: cap for rid, _, cap in ROW_SPEC}
            rows = build_rows(taste, pool, played_ids, seed, gids, newids, limits)
            log("  %-18s taste=%s pool=%d rows=%s" % (
                name, ",".join(sorted(taste["genres"], key=taste["genres"].get,
                                      reverse=True)[:4]) or "-",
                len(pool), [(r["id"], len(r["items"])) for r in rows]))
            if dry_run:
                continue
            if rows:
                write_rows(uid, rows)
                write_layout(uid, rows)
        except Exception as exc:
            log("  %-18s FAILED: %s: %s" % (name, type(exc).__name__, exc))
    log("run done")


# --- selftest ----------------------------------------------------------------

def _selftest():
    now = time.time()
    today = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    played = [{"Name": "Alien", "Type": "Movie", "Genres": ["Horror", "Science Fiction"],
               "ProductionYear": 1979, "UserData": {"LastPlayedDate": today}},
              {"Name": "Aliens", "Type": "Movie", "Genres": ["Horror", "Science Fiction"],
               "ProductionYear": 1986, "UserData": {"LastPlayedDate": today}}]
    taste = taste_profile(played, [], [])
    assert taste["genres"]["Horror"] == 1.0, taste
    assert taste["decades"][1970] == 1.0

    pool = [
        {"Id": "h1", "Type": "Movie", "Genres": ["Horror"], "ProductionYear": 1980,
         "CommunityRating": 8.1},
        {"Id": "r1", "Type": "Movie", "Genres": ["Romance"], "ProductionYear": 1980,
         "CommunityRating": 8.1},
        {"Id": "p1", "Type": "Movie", "Genres": ["Horror"], "ProductionYear": 1980,
         "CommunityRating": 8.1},   # already played
        {"Id": "d1", "Type": "Series", "Genres": ["Horror", "Thriller"],
         "ProductionYear": 1981, "CommunityRating": 7.4},
        {"Id": "d2", "Type": "Series", "Genres": ["Horror"], "ProductionYear": 1985,
         "CommunityRating": 7.9},
        {"Id": "n1", "Type": "Movie", "Genres": ["Horror"], "ProductionYear": 2026,
         "CommunityRating": 6.9},
    ]
    seed = {"Name": "Alien", "Genres": ["Horror", "Science Fiction"]}
    limits = {rid: 1 for rid, _, _ in ROW_SPEC}   # one item each: forces the split
    rows = build_rows(taste, pool, {"p1"}, seed, {"h1"}, {"n1"}, limits)
    by_id = {r["id"]: r for r in rows}
    seen = [i for r in rows for i in r["items"]]
    assert "p1" not in seen, "played items must never appear: %s" % seen
    assert len(seen) == len(set(seen)), "rows must not repeat items: %s" % seen
    assert by_id["msp-foryou"]["items"][0] == "h1", by_id["msp-foryou"]
    assert by_id["msp-because"]["name"] == "Because You Watched Alien"
    # because-row only carries shared-genre neighbours, never the romance odd-one-out
    assert "r1" not in by_id["msp-because"]["items"], by_id["msp-because"]
    assert by_id["msp-newforme"]["items"] == ["n1"], by_id["msp-newforme"]
    hs = splice_home([{"type": "resume", "order": 1},
                      {"type": "favoriteepisodes", "order": 2},
                      {"type": "genres", "order": 3},
                      {"type": "pluginDynamic", "pluginSection": "taste_0"}], rows)
    kinds = [h.get("pluginAdditionalData") or h.get("pluginSection") or h.get("type")
             for h in hs]
    assert "taste_0" not in kinds, "old taste rows must be stripped: %s" % kinds
    assert kinds[:2] == ["resume", "favoriteepisodes"], kinds
    assert [h["order"] for h in hs] == list(range(1, len(hs) + 1)), hs
    print("selftest OK: %d rows, %d items, layout splice correct" %
          (len(rows), len(seen)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        run(dry_run=a.dry_run)
