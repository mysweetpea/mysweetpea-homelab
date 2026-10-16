#!/usr/bin/env python3
"""Daily home content rows for Moonfin (synthetic playlist engine) + layout refresh.

Home content rows are served through the moonbase-lite shim as synthetic
playlists: nginx routes /Playlists/{id}/Items to the shim (moonbase_lite.py),
and the client renders one titled row per playlist via pluginDynamic entries in
homeSections. This script rebuilds, every day:

  1. /opt/moonbase-lite/data/rows.json - ordered slot list {id, name, items[]}.
     Slots: Trending Now, New Releases, Most Played (when there is data), and
     rotating feature slots (1 anime + 2 combos + 1 mood + 1 curated + 1
     wildcard). Rows are movie+series MIXED and GLOBALLY deduped so the same
     title never appears in two rows of the same home.
  2. defaults.json homeSections - the fleet layout: My Media hub first, then
     personal rows, taste rows (spliced per-user by moonfin-taste.py), engine
     rows with today's names, then genres/collections. latestmedia stays
     explicitly disabled (streamer "Recently Added" rows are retired).
  3. the nginx excludes snippet - retired to a no-op comment (latestmedia off).

Dedupe priority: narrow slots first, big generic slots (Trending, New
Releases) absorb the rest. Candidate order is rotated by the date seed so row
contents drift day to day.

ponytail: full regeneration in one pass, ~20 upstream fetches; incremental
diffing only if row churn ever matters.
"""
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import date

DATA = "/opt/moonbase-lite/data"
ROWS = os.path.join(DATA, "rows.json")
DEFAULTS = os.path.join(DATA, "defaults.json")
SNIPPET = "/etc/nginx/snippets/aiostreams-config-excludes.snippet.conf"
BACKUP = "/root/aiostreams-config-excludes.snippet.conf.bak-rotate"
CREDS = "/root/.aiostreams-warmer-creds.json"
PINS = os.path.join(DATA, "pins.json")
STATE = "/root/.moonfin-rotate-state.json"
LOG = "/var/log/moonfin-rotate.log"

SERVER_ID = "https://aiostreams.mysweetpea.cc/jellyfin"
TOP_N = 30      # items per row (after dedupe)
MIN_N = 8       # drop a row thinner than this
FETCH_N = 100   # candidates pulled per source view

ANIME_POOL = [
    ("Anime: Action & Adventure Movies", "Anime: Action & Adventure Shows"),
    ("Anime: Romance Movies", "Anime: Romance Shows"),
    ("Anime: Comedy Movies", "Anime: Comedy Shows"),
    ("Anime: Fantasy Movies", "Anime: Fantasy Shows"),
    ("Anime: Sci-Fi Movies", "Anime: Sci-Fi Shows"),
    ("Anime: Isekai Movies", "Anime: Isekai Shows"),
    ("Anime: Slice of Life Movies", "Anime: Slice of Life Shows"),
    ("Anime: Sports Movies", "Anime: Sports Shows"),
    ("Anime: Horror Movies", "Anime: Horror Shows"),
    ("Anime: Mecha Movies", "Anime: Mecha Shows"),
]
MOOD_POOL = [
    ("Feel-Good Movies", "Feel-Good Shows"),
    ("Dark & Gritty Movies", "Dark & Gritty Shows"),
    ("Mind-Bending Movies", "Mind-Bending Shows"),
    ("Cozy Movies", "Cozy Shows"),
    ("Epic Movies", "Epic Shows"),
]
CURATED_POOL = [
    ("Critically Acclaimed Movies", "Critically Acclaimed Shows"),
    ("Hidden Gems Movies", "Hidden Gems Shows"),
    ("Best of All Time Movies", "Best of All Time Shows"),
    ("Cult Classics Movies", "Cult Classics Shows"),
]
COMBO_POOL = [
    ("Action Comedy Movies", "Action Comedy Shows"),
    ("Crime Drama Movies", "Crime Drama Shows"),
    ("Dark Comedy Movies", "Dark Comedy Shows"),
    ("Family Adventure Movies", "Family Adventure Shows"),
    ("Fantasy Adventure Movies", "Fantasy Adventure Shows"),
    ("Horror Thriller Movies", "Horror Thriller Shows"),
    ("Psychological Thriller Movies", "Psychological Thriller Shows"),
    ("Romantic Comedy Movies", "Romantic Comedy Shows"),
    ("Sci-Fi Thriller Movies", "Sci-Fi Thriller Shows"),
    ("Superhero Movies", "Superhero Shows"),
]
WILDCARD_POOL = [
    ("Documentaries Movies", "Documentaries Shows"),
    ("Romance Movies", "Romance Shows"),
    ("80s Movies", "80s Shows"),
    ("90s Movies", "90s Shows"),
    ("2000s Movies", "2000s Shows"),
]

DISPLAY = {"Korean": "Korean Picks", "80s": "The 80s", "90s": "The 90s",
           "2000s": "The 2000s"}

# fleet layout; "ENGINE" = today's playlist rows, spliced after favoriteepisodes
BASE_LAYOUT = ["smalllibrarytiles", "resume", "nextup",
               "sinceyouwatched1", "sinceyouwatched2", "sinceyouwatched3",
               "sinceyouwatched4", "sinceyouwatched5", "rewatch",
               "favoritemovies", "favoriteseries", "favoriteepisodes",
               "ENGINE", "genres", "collections"]

CREDS_BASE = ""


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        open(LOG, "a").write(line + "\n")
    except OSError:
        pass


def fetch(path, token=None, data=None):
    headers = {"User-Agent": "Moonfin-Rows/1.0"}
    if token:
        headers["Authorization"] = 'MediaBrowser Token="%s"' % token
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(CREDS_BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def pick(pool, k, seed):
    pool = sorted(pool)
    random.Random(seed).shuffle(pool)
    return pool[:k]


def display_name(mv_name):
    base = mv_name[:-7] if mv_name.endswith(" Movies") else mv_name
    return DISPLAY.get(base, base)


def features_for(seed):
    return (pick(ANIME_POOL, 1, seed * 11 + 1) +
            pick(COMBO_POOL, 2, seed * 11 + 2) +
            pick(MOOD_POOL, 1, seed * 11 + 3) +
            pick(CURATED_POOL, 1, seed * 11 + 4) +
            pick(WILDCARD_POOL, 1, seed * 11 + 5))


def interleave(a, b):
    out = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def build_row(ids, seen):
    picked = []
    for i in ids:
        if i in seen:
            continue
        seen.add(i)
        picked.append(i)
        if len(picked) >= TOP_N:
            break
    return picked


def view_ids(name2id, vname, token, limit=FETCH_N):
    vid = name2id.get(vname)
    if not vid:
        log("WARN: view %r missing" % vname)
        return []
    try:
        d = fetch("/Items?ParentId=%s&Limit=%d" % (vid, limit), token=token)
        return [i.get("Id") for i in (d.get("Items") or []) if i.get("Id")]
    except Exception as exc:
        log("WARN: view fetch %r failed: %s" % (vname, exc))
        return []


def most_played(token):
    """Household top titles by summed PlayCount across users (owner + personas)."""
    counts = {}
    tokens = [token]
    try:
        pins = json.load(open(PINS))
        if isinstance(pins, dict):
            for slug, e in pins.items():
                if isinstance(e, dict) and e.get("pin"):
                    try:
                        body = json.dumps({"Username": e.get("name") or slug,
                                           "Pw": e["pin"]}).encode()
                        tokens.append(fetch("/Users/AuthenticateByName", data=body)["AccessToken"])
                    except Exception as exc:
                        log("mostplayed: persona login failed (%s): %s" % (slug, type(exc).__name__))
    except (OSError, ValueError):
        pass
    for tok in tokens:
        try:
            d = fetch("/Items?Filters=IsPlayed&Recursive=true"
                      "&IncludeItemTypes=Movie,Series&Limit=200", token=tok)
            for it in d.get("Items") or []:
                pc = (it.get("UserData") or {}).get("PlayCount") or 0
                iid = it.get("Id")
                if pc and iid:
                    counts[iid] = counts.get(iid, 0) + pc
        except Exception as exc:
            log("mostplayed: fetch failed: %s" % exc)
    return [i for i, _ in sorted(counts.items(),
                                 key=lambda kv: (-kv[1], kv[0]))]


def build_slots(seed):
    anime, c1, c2, mood, curated, wild = features_for(seed)
    return [
        {"id": "msp-trending", "name": "Trending Now",
         "views": ("Trending Movies", "Trending Shows"), "prio": 90, "kind": "core"},
        {"id": "msp-newreleases", "name": "New Releases",
         "views": ("New Releases Movies", "New Releases Shows"), "prio": 95, "kind": "core"},
        # --- fixed mainstream anchors (2026-10-16) ---
        {"id": "msp-action", "name": "Action Fix",
         "views": ("Action & Adventure Movies", "Action & Adventure Shows"), "prio": 70, "kind": "fixed"},
        {"id": "msp-afterdark", "name": "After Dark",
         "views": ("Horror Movies", "Horror Shows"), "prio": 71, "kind": "fixed"},
        {"id": "msp-crime", "name": "True Crime Files",
         "views": ("True Crime Movies", "True Crime Shows"), "prio": 72, "kind": "fixed"},
        {"id": "msp-kdrama", "name": "K-Dramas",
         "views": ("Korean Movies", "K-Dramas"), "prio": 73, "kind": "fixed"},
        {"id": "msp-anime", "name": display_name(anime[0]),
         "views": anime, "prio": 10, "kind": "feature"},
        {"id": "msp-combo1", "name": display_name(c1[0]),
         "views": c1, "prio": 20, "kind": "feature"},
        {"id": "msp-combo2", "name": display_name(c2[0]),
         "views": c2, "prio": 21, "kind": "feature"},
        {"id": "msp-mood", "name": display_name(mood[0]),
         "views": mood, "prio": 30, "kind": "feature"},
        {"id": "msp-curated", "name": display_name(curated[0]),
         "views": curated, "prio": 40, "kind": "feature"},
        {"id": "msp-wildcard", "name": display_name(wild[0]),
         "views": wild, "prio": 50, "kind": "feature"},
    ]


def build_layout(rows):
    """defaults.json homeSections: base layout + engine rows + explicit disabled latestmedia."""
    hs = []
    for t in BASE_LAYOUT:
        if t == "ENGINE":
            for r in rows:
                hs.append({"kind": "pluginDynamic", "type": "none", "enabled": True,
                           "serverId": SERVER_ID, "pluginSource": "playlists",
                           "pluginSection": "playlist",
                           "pluginAdditionalData": r["id"],
                           "pluginDisplayText": r["name"]})
        else:
            hs.append({"kind": "builtin", "type": t, "enabled": True})
    hs.append({"kind": "builtin", "type": "latestmedia", "enabled": False})
    for i, h in enumerate(hs):
        h["order"] = i + 1
    return hs


def snippet_text():
    return ("# LatestItemsExcludes injection RETIRED 2026-10-16: home rows are\n"
            "# playlist-backed now (moonfin-rotate.py rows.json + moonbase-lite shim)\n"
            "# and the latestmedia section is disabled. No-op file on purpose; the\n"
            "# server-level include in the aios vhost expects it to exist.\n")


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    global CREDS_BASE
    dry = "--dry-run" in sys.argv
    seed = int(os.environ.get("ROTATE_SEED") or date.today().strftime("%Y%m%d"))
    creds = json.load(open(CREDS))
    CREDS_BASE = creds["base"].rstrip("/")
    body = json.dumps({"Username": creds["uuid"], "Pw": creds["password"]}).encode()
    tok = fetch("/Users/AuthenticateByName", data=body)["AccessToken"]
    name2id = {v.get("Name"): v.get("Id")
               for v in fetch("/UserViews", token=tok).get("Items", []) if v.get("Name")}

    slots = build_slots(seed)
    mp = most_played(tok)
    mp_slot = {"id": "msp-mostplayed", "name": "Most Played in Our Garden",
               "views": None, "prio": 60, "kind": "core"}

    order = sorted(slots, key=lambda s: s["prio"])  # narrow first, generic last
    if len(mp) >= MIN_N:
        idx = next((i for i, s in enumerate(order) if s["kind"] == "core"),
                   len(order))
        order.insert(idx, mp_slot)

    seen = set()
    built = {}
    for s in order:
        if s["views"] is None:
            ids = mp
        else:
            mv, tv = s["views"]
            ids = interleave(view_ids(name2id, mv, tok), view_ids(name2id, tv, tok))
        off = (seed * 31 + s["prio"]) % 17
        ids = ids[off:] + ids[:off] if ids else ids
        built[s["id"]] = build_row(ids, seen)

    # display order: Trending, New Releases, [Most Played], then the features
    display = [s for s in slots if s["kind"] == "core"]
    if len(mp) >= MIN_N:
        display.insert(1, mp_slot)
    display += [s for s in slots if s["kind"] == "fixed"]
    display += [s for s in slots if s["kind"] == "feature"]

    rows = []
    for s in display:
        items = built.get(s["id"]) or []
        if len(items) < MIN_N:
            log("drop row %r: only %d items after dedupe" % (s["name"], len(items)))
            continue
        rows.append({"id": s["id"], "name": s["name"], "items": items})

    log("seed=%s rows:" % seed)
    for r in rows:
        log("  %-26s %3d items" % (r["name"], len(r["items"])))

    layout = build_layout(rows)
    if dry:
        print(json.dumps({"rows": [{k: (v if k != "items" else len(v))
                                   for k, v in r.items()} for r in rows]}, indent=1))
        return 0

    write_atomic(ROWS, json.dumps({"generated": time.time(), "seed": seed,
                                   "rows": rows}, indent=1))

    doc = json.load(open(DEFAULTS))
    doc["homeSections"] = layout
    write_atomic(DEFAULTS, json.dumps(doc, indent=1))

    try:
        open(BACKUP, "w").write(open(SNIPPET).read())
    except OSError:
        pass
    write_atomic(SNIPPET, snippet_text())
    t = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if t.returncode != 0:
        log("nginx -t FAILED, restoring previous snippet: %s" % t.stderr.strip()[-300:])
        try:
            open(SNIPPET, "w").write(open(BACKUP).read())
        except OSError:
            pass
        subprocess.run(["systemctl", "reload", "nginx"])
        return 1
    subprocess.run(["systemctl", "reload", "nginx"], check=True)

    json.dump({"seed": seed,
               "slots": [{k: v for k, v in r.items() if k != "items"} for r in rows],
               "counts": {r["id"]: len(r["items"]) for r in rows}, "ts": time.time()},
              open(STATE, "w"), indent=1)

    rt = json.load(open(ROWS))
    got = {r["id"]: len(r["items"]) for r in rt.get("rows") or []}
    ok = all(got.get(r["id"]) == len(r["items"]) for r in rows)
    log("verify: rows.json %d rows %s" % (len(got), "OK" if ok else "MISMATCH"))

    try:
        subprocess.run(["flock", "-n", "/tmp/aiostreams-warmer.lock", "/usr/bin/python3",
                        "/root/aiostreams-warmer.py"], timeout=300, capture_output=True)
    except Exception as exc:
        log("warmer trigger skipped: %s" % exc)
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        f1, f2 = features_for(20261016), features_for(20261016)
        assert f1 == f2 and len(f1) == 6, "features must be deterministic, 6 pairs"
        assert len(set(t[0] for t in f1)) == 6, "no duplicate pairs in a day"
        assert display_name("Anime: Slice of Life Movies") == "Anime: Slice of Life"
        assert display_name("Korean Movies") == "Korean Picks"
        assert display_name("80s Movies") == "The 80s"
        assert display_name("True Crime Movies") == "True Crime"
        assert interleave(["a1", "a2"], ["b1"]) == ["a1", "b1", "a2"]
        seen = set()
        assert build_row(["x", "y", "x", "z"], seen) == ["x", "y", "z"]
        assert seen == {"x", "y", "z"}
        slots = build_slots(20261016)
        fake_rows = [{"id": s["id"], "name": s["name"], "items": ["i" + s["id"]]}
                     for s in slots]
        layout = build_layout(fake_rows)
        types = [h.get("pluginAdditionalData") or h.get("type") for h in layout]
        assert types.index("smalllibrarytiles") == 0, "hub row must be first"
        assert types[-1] == "latestmedia" and layout[-1]["enabled"] is False
        assert [h["order"] for h in layout] == list(range(1, len(layout) + 1))
        i_fav = types.index("favoriteepisodes")
        i_eng = types.index("msp-trending")
        i_gen = types.index("genres")
        assert i_fav < i_eng < i_gen, "engine rows must sit between favorites and genres"
        assert len(ANIME_POOL) == 10 and len(COMBO_POOL) == 10
        assert len(MOOD_POOL) == 5 and len(CURATED_POOL) == 4 and len(WILDCARD_POOL) == 5
        assert len(slots) == 12
        assert [x["id"] for x in slots if x["kind"] == "fixed"] == \
            ["msp-action", "msp-afterdark", "msp-crime", "msp-kdrama"]
        print("selftest ok")
        sys.exit(0)
    sys.exit(main())
