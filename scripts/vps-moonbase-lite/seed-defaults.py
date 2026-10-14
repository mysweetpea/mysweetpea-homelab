#!/usr/bin/env python3
"""Build moonbase-lite defaults.json (fleet DUS) from a Moonfin user settings file.

Keeps the 'global' scope, strips per-user API keys, filters home rows to
media-server-backed types, pins the theme.
usage: seed-defaults.py <settings.json> <out.json> [--theme ID] [--keep t1,t2]
"""
import argparse, json

STRIP = ("seerrApiKey", "tmdbApiKey", "mdblistApiKey")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--theme", default="mysweetpea_obsidian")
    ap.add_argument("--keep", default="resume,nextup,sinceyouwatched1,sinceyouwatched2,sinceyouwatched3,genres,rewatch,collections,favoritemovies,favoriteepisodes,playlists,latestmedia")
    a = ap.parse_args()
    doc = json.load(open(a.src))
    g = dict(doc.get("global") or {})
    for k in STRIP:
        g.pop(k, None)
    keep = [t.strip() for t in a.keep.split(",") if t.strip()]
    hs = g.get("homeSections")
    if isinstance(hs, list):
        # keep-list order wins; order numbers normalize to 1..N (client sorts ascending).
        by_type = {h.get("type"): h for h in hs if isinstance(h, dict)}
        out = []
        for i, t in enumerate(keep):
            h = dict(by_type.get(t) or {"kind": "builtin", "type": t})
            h["enabled"] = True
            h["order"] = i + 1
            out.append(h)
        g["homeSections"] = out
    if a.theme:
        g["customThemeId"] = a.theme
        g["visualTheme"] = "custom"
    # Media bar walks these catalogs one-by-one on every load; all 33 = ~8s of
    # serial fetches. Point it at five hero-worthy catalogs instead (~1.3s).
    # View ids are stable hashes of (catalog id, type) from the manifest.
    if "mediaBarLibraryIds" in g:
        g["mediaBarLibraryIds"] = ",".join([
            "a2b3d475a9842e952002bbf66ada706f",  # Trending Movies
            "a26d1a33e2d56da6265981f9d902a374",  # Trending Shows
            "a2170aa89ada673e50b5011031296a24",  # Popular Movies
            "a20e1577e1543cd8707bba16912f48a4",  # Popular Shows
            "a25ccf8702de5678f2c1a04ca104f30d",  # Netflix Movies
        ])
    # Compact icon-only top navbar (client stock default; labels expand on demand).
    g["navbarAlwaysExpanded"] = False
    # One "Recently Added <catalog>" home row per library (Netflix/Hulu/... rows),
    # instead of merging them into single Movies/TV rows (owner request 2026-10-14).
    g["mergeRecentRowsByType"] = False
    # No Moonbase plugin on the AIOStreams side: keep Seerr surfaces dark.
    g["seerrEnabled"] = False
    g["jellyseerrEnabled"] = False
    g["displaySeerrRows"] = False
    json.dump(g, open(a.out, "w"), ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {a.out}: {len(g)} fields, "
          f"{len(g.get('homeSections', []))} home rows, theme={g.get('customThemeId')}")


if __name__ == "__main__":
    main()
