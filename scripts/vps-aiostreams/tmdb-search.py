#!/usr/bin/env python3
"""TMDB-direct search + deep streaming catalogs addon for AIOStreams.

Search (2026-10-15): the enrichment-heavy TMDB addon takes 0.7-12s+ per cold
search (30s stalls seen); TMDB's own API answers the same query in ~0.05-0.15s.
Serves page-1 results in the exact `tmdb:<id>` preview format.

Deep catalogs (2026-10-15): per-service browse catalogs backed by TMDB discover
(popularity-sorted, watch-provider filtered; US unless noted) with Stremio
`skip` paging - ~1000+ titles per service instead of the stock ~100-title
snapshots. Provider ids verified live: Paramount+ = 2303 (531 is legacy junk),
Canal+ needs watch_region FR (US returns 0 results).

Endpoints (Stremio catalog protocol):
  GET /manifest.json
  GET /catalog/{movie|series}/search/search=<term>.json
  GET /catalog/{movie|series}/<svc>-movies|-shows.json[/skip=N]

ponytail: single-process ThreadingHTTPServer is plenty (<1 req/s from aios); no config knobs.
"""
import json
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 3010
KEY_FILE = "/opt/moonbase-lite/tmdb.key"
TMDB = "https://api.themoviedb.org/3"
TMDB_TIMEOUT = 4          # hard ceiling: on expiry return empty rather than hang aios search
CACHE_TTL = 600           # per (kind, term); aios layers its own 6h cache above
DEEP_TTL = 21600          # discover pages change slowly; 6h matches aios catalog cache
LIMIT = 20

# (catalog key, tmdb provider id, display name, watch region, has_movies, has_shows)
SERVICES = [
    ("netflix", 8, "Netflix", "US", True, True),
    ("prime", 9, "Prime Video", "US", True, True),
    ("disney", 337, "Disney+", "US", True, True),
    ("hbo", 1899, "HBO Max", "US", True, True),
    ("hulu", 15, "Hulu", "US", True, True),
    ("paramount", 2303, "Paramount+", "US", True, True),
    ("peacock", 386, "Peacock", "US", True, True),
    ("apple", 350, "Apple TV+", "US", True, True),
    ("crunchyroll", 283, "Crunchyroll", "US", True, True),
    ("discovery", 520, "Discovery+", "US", False, True),
    ("canal", 381, "Canal+", "FR", True, True),
]

CATALOG_DEFS = {}   # (stremio_type, catalog_id) -> (provider_id, region)
DEEP_CATALOGS = []
for _k, _pid, _name, _region, _mv, _sh in SERVICES:
    for _kind, _suffix, _has in (("movie", "movies", _mv), ("series", "shows", _sh)):
        if not _has:
            continue
        _cid = f"{_k}-{_suffix}"
        CATALOG_DEFS[(_kind, _cid)] = (_pid, _region)
        DEEP_CATALOGS.append({
            "type": _kind, "id": _cid,
            "name": f"{_name} {'Movies' if _kind == 'movie' else 'Shows'}",
            "extra": [{"name": "skip"}],
        })

MANIFEST = {
    "id": "org.mysweetpea.tmdb-search",
    "version": "1.1.0",
    "name": "TMDB Search",
    "description": "Fast TMDB search and deep streaming catalogs for AIOStreams",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "catalogs": [
        {"type": "movie", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
        {"type": "series", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
    ] + DEEP_CATALOGS,
}

_cache = {}   # ponytail: unbounded, but keys are human-typed search terms
_dcache = {}  # (kind, provider_id, region, page) -> (ts, metas)


def _key():
    with open(KEY_FILE) as f:
        return f.read().strip()


def _metas(results, stype):
    """TMDB result list -> Stremio MetaPreview list (shared by search + discover)."""
    out = []
    for m in (results or [])[:LIMIT]:
        name = m.get("title") or m.get("name")
        if not m.get("id") or not name:
            continue
        item = {"id": f"tmdb:{m['id']}", "type": stype, "name": name}
        if m.get("poster_path"):
            item["poster"] = "https://image.tmdb.org/t/p/w500" + m["poster_path"]
        if m.get("backdrop_path"):
            item["background"] = "https://image.tmdb.org/t/p/w780" + m["backdrop_path"]
        rel = (m.get("release_date") or m.get("first_air_date") or "")[:4]
        if rel:
            item["releaseInfo"] = rel
        out.append(item)
    return out


def metas_for(kind, term):
    """kind: 'movie'|'tv' -> list of Stremio MetaPreview dicts."""
    ck = (kind, term.lower())
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        q = urllib.parse.urlencode(
            {"query": term, "api_key": _key(), "include_adult": "false"})
        with urllib.request.urlopen(f"{TMDB}/search/{kind}?{q}",
                                    timeout=TMDB_TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # fail-soft: search must never hang on us
        print(f"tmdb-search: {kind} {term!r} failed: {e}", flush=True)
        return []  # ponytail: failures not cached, so recovery is immediate
    out = _metas(data.get("results"), "movie" if kind == "movie" else "series")
    _cache[ck] = (time.time(), out)
    print(f"tmdb-search: {kind} {term!r} -> {len(out)}", flush=True)
    return out


def _discover_page(kind, provider_id, region, page):
    ck = (kind, provider_id, region, page)
    hit = _dcache.get(ck)
    if hit and time.time() - hit[0] < DEEP_TTL:
        return hit[1]
    try:
        q = urllib.parse.urlencode({
            "with_watch_providers": str(provider_id), "watch_region": region,
            "sort_by": "popularity.desc", "page": page, "api_key": _key()})
        with urllib.request.urlopen(f"{TMDB}/discover/{kind}?{q}",
                                    timeout=TMDB_TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"tmdb-search: discover {kind}/{provider_id}/{region} p{page} failed: {e}", flush=True)
        return None  # not cached; recovery is immediate
    metas = _metas(data.get("results"), "movie" if kind == "movie" else "series")
    _dcache[ck] = (time.time(), metas)
    return metas


def discover_metas(kind, provider_id, region, skip):
    """Page of a service catalog at Stremio offset `skip` (TMDB pages are 20)."""
    page = skip // LIMIT + 1
    off = skip % LIMIT
    if skip < 0 or page > 500:  # TMDB hard cap
        return []
    first = _discover_page(kind, provider_id, region, page)
    if first is None:
        return []
    out = first[off:off + LIMIT]
    if off and len(out) < LIMIT:
        nxt = _discover_page(kind, provider_id, region, page + 1)
        if nxt:
            out = out + nxt[:LIMIT - len(out)]
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/")]
        if parts == ["manifest.json"]:
            return self._send(MANIFEST)
        if len(parts) >= 3 and parts[0] == "catalog":
            stype = parts[1]
            kind = {"movie": "movie", "series": "tv"}.get(stype)
            if kind:
                cid = parts[2]
                if cid.endswith(".json"):  # Stremio single-segment form: /catalog/movie/<id>.json
                    cid = cid[:-5]
                if cid == "search":
                    term = (urllib.parse.parse_qs(u.query).get("search") or [""])[0]
                    for seg in parts[3:]:
                        for piece in seg.split("&"):
                            if piece.startswith("search="):
                                term = piece[len("search="):]
                    term = term[:-5] if term.endswith(".json") else term
                    term = term.strip()
                    if not term:
                        return self._send({"metas": []})
                    return self._send({"metas": metas_for(kind, term),
                                       "cacheMaxAge": 3600})
                if (stype, cid) in CATALOG_DEFS:
                    skip = 0
                    for seg in parts[3:]:
                        for piece in seg.split("&"):
                            if piece.startswith("skip="):
                                digits = "".join(c for c in piece[5:] if c.isdigit())
                                skip = int(digits) if digits else 0
                    qskip = (urllib.parse.parse_qs(u.query).get("skip") or [""])[0]
                    if qskip.lstrip("-").isdigit():
                        skip = int(qskip)
                    provider_id, region = CATALOG_DEFS[(stype, cid)]
                    return self._send({"metas": discover_metas(kind, provider_id, region, skip),
                                       "cacheMaxAge": 3600})
            return self._send({"metas": []})  # unknown catalog type/id
        if not parts or parts == [""]:
            return self._send({"ok": True, "service": "tmdb-search"})
        self._send({"err": "not found"}, code=404)

    def log_message(self, fmt, *args):  # quiet default logging; we print our own
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
