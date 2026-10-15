#!/usr/bin/env python3
"""TMDB-direct search addon for AIOStreams.

Why: the enrichment-heavy TMDB addon takes 0.7-12s+ per cold search (30s stalls seen);
TMDB's own API answers the same query in ~0.05-0.15s. This addon serves page-1 results
in the exact `tmdb:<id>` preview format so items merge with the rest of the library.

Endpoints (Stremio catalog protocol):
  GET /manifest.json
  GET /catalog/{movie|series}/search/search=<term>.json

ponytail: single-process ThreadingHTTPServer is plenty (<1 req/s); no config knobs.
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
LIMIT = 20

MANIFEST = {
    "id": "org.mysweetpea.tmdb-search",
    "version": "1.0.0",
    "name": "TMDB Search",
    "description": "Fast TMDB search for AIOStreams",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "catalogs": [
        {"type": "movie", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
        {"type": "series", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
    ],
}

_cache = {}  # ponytail: unbounded, but keys are human-typed search terms


def _key():
    with open(KEY_FILE) as f:
        return f.read().strip()


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
    stype = "movie" if kind == "movie" else "series"
    out = []
    for m in (data.get("results") or [])[:LIMIT]:
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
    _cache[ck] = (time.time(), out)
    print(f"tmdb-search: {kind} {term!r} -> {len(out)}", flush=True)
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
            kind = {"movie": "movie", "series": "tv"}.get(parts[1])
            if kind:
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
            return self._send({"metas": []})  # unknown catalog type/id
        if not parts or parts == [""]:
            return self._send({"ok": True, "service": "tmdb-search"})
        self._send({"err": "not found"}, code=404)

    def log_message(self, fmt, *args):  # quiet default logging; we print our own
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
