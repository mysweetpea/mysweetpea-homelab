#!/usr/bin/env python3
"""Generate the 16:9 streaming-service tiles for the My Media row.

Each tile: 640x360, #0B0D12 canvas, the official TMDB watch-provider logo
centred (max 460x220). Output goes to ./logos/<slug>.png; upload to
/var/www/aiostreams/logos/ on the VPS. Slugs must match the SERVICES keys in
tmdb-search.py (its DEEP_CATALOGS point at
https://aiostreams.mysweetpea.cc/logos/<slug>.png).

Usage: python3 make-logos.py [tmdb-key-file]   (default /opt/moonbase-lite/tmdb.key)
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "logos")
RAW = os.path.join(OUT, "raw")
os.makedirs(RAW, exist_ok=True)

KEY = open(sys.argv[1] if len(sys.argv) > 1 else "/opt/moonbase-lite/tmdb.key").read().strip()

SERVICES = {
    "netflix": 8, "prime": 9, "disney": 337, "hbo": 1899, "hulu": 15,
    "paramount": 2303, "peacock": 386, "apple": 350, "crunchyroll": 283,
    "discovery": 520, "canal": 381,
}


def get(path, **q):
    q["api_key"] = KEY
    return json.load(urllib.request.urlopen(
        f"https://api.themoviedb.org/3/{path}?" + urllib.parse.urlencode(q), timeout=20))


logos = {}
for region in ("US", "FR"):  # Canal+ is FR-only
    for media in ("tv", "movie"):
        for r in get(f"watch/providers/{media}", watch_region=region).get("results", []):
            logos.setdefault(r["provider_id"], r.get("logo_path"))

for slug, pid in SERVICES.items():
    lp = logos.get(pid)
    assert lp, f"no logo for {slug}"
    raw = os.path.join(RAW, slug + os.path.splitext(lp)[1])
    if not os.path.exists(raw):
        urllib.request.urlretrieve("https://image.tmdb.org/t/p/w500" + lp, raw)
    out = os.path.join(OUT, slug + ".png")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-vf",
                    "scale=460:220:force_original_aspect_ratio=decrease,"
                    "pad=640:360:(ow-iw)/2:(oh-ih)/2:color=0x0B0D12",
                    "-frames:v", "1", out], check=True)
    print(slug, os.path.getsize(out), "bytes")
