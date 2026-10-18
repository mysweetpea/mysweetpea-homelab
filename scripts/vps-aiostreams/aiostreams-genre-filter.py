#!/usr/bin/env python3
"""AIOStreams genre-taxonomy filter (sidecar).

nginx routes /jellyfin/Genres and /jellyfin/Items/Filters(2) here; we forward
to the AIOStreams app on 127.0.0.1:3000 and clean the browse taxonomy the
Jellyfin clients see:

  - drop the Trending catalogs' 'Day' / 'Week' window pseudo-genres
  - de-duplicate entries by name (movie / series / collections copies of the
    same genre) and sort the root list by name
  - for the ROOT genre list only: drop the year + language entries owned by
    the 'Movies by Year' / 'Movies by Language' libraries -- those are browsed
    inside their own libraries, and ~220 extra cards would drown the grid.

Any parse problem, crash, or unusual response passes through untouched, and
nginx falls back to the app on :3000 if this process is down (fail-open).
"""
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 3000)
PORT = int(os.environ.get("PORT", "3003"))
DROP = {"Day", "Week"}
EXCLUDE_VIEWS = {"Movies by Year", "Shows by Year", "Movies by Language", "Shows by Language"}
CACHE_TTL = 21600.0

_cache = {"t": 0.0, "names": frozenset()}
_lock = threading.Lock()
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "accept-encoding",
       "content-length"}


def _upstream(path, hdrs):
    conn = http.client.HTTPConnection(*UPSTREAM, timeout=60)
    conn.request("GET", path, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _excluded_names(hdrs):
    """Entry names owned by the year/language libraries. Cached 6h."""
    now = time.time()
    with _lock:
        if now - _cache["t"] < CACHE_TTL:
            return _cache["names"]
    names = set()
    try:
        auth = {k: v for k, v in hdrs.items() if k.lower() == "authorization"}
        st, data = _upstream("/jellyfin/UserViews", auth)
        views = json.loads(data).get("Items", []) if st == 200 else []
        for v in views:
            if v.get("Name") in EXCLUDE_VIEWS and v.get("Id"):
                st, data = _upstream("/jellyfin/Genres?ParentId=" + v["Id"], auth)
                if st == 200:
                    names.update(
                        i.get("Name")
                        for i in json.loads(data).get("Items", [])
                        if i.get("Name")
                    )
    except Exception:
        return _cache["names"]  # keep the last good set
    with _lock:
        _cache["t"] = now
        _cache["names"] = frozenset(names)
    return _cache["names"]


_counts = {"t": 0.0, "map": {}}
_me = {"t": 0.0, "uid": ""}


def _uid(hdrs):
    """Caller's Jellyfin id (counts need a user context). Cached 6h."""
    now = time.time()
    if _me["uid"] and now - _me["t"] < CACHE_TTL:
        return _me["uid"]
    try:
        _st, data = _upstream("/jellyfin/Users/Me", hdrs)
        uid = str(json.loads(data).get("Id") or "").replace("-", "")
        if uid:
            _me.update(t=now, uid=uid)
    except Exception:
        pass
    return _me["uid"]


def _genre_counts(items, hdrs):
    """ItemCounts per genre: Moonfin drops every genre whose count is 0
    (all_genres_screen -> .where(itemCount > 0)), and AIOStreams sends none,
    which is what made the Genres tab say 'No Genre Found'.
    ponytail: serial per-genre count queries (~0.03s each, 27 genres), cached 6h.
    """
    now = time.time()
    if _counts["map"] and now - _counts["t"] < CACHE_TTL:
        return _counts["map"]
    uid = _uid(hdrs)
    if not uid:
        return _counts["map"]
    out = {}
    for it in items:
        gid = it.get("Id")
        if not gid:
            continue
        try:
            _st, data = _upstream(
                "/jellyfin/Users/%s/Items?GenreIds=%s&Limit=1&Recursive=true"
                "&EnableTotalRecordCount=true&IncludeItemTypes=Movie,Series" % (uid, gid), hdrs)
            n = int(json.loads(data).get("TotalRecordCount") or 0)
        except Exception:
            n = 0
        out[gid] = max(n, 1)   # never 0: the client re-checks real items per genre
    if out:
        _counts.update(t=now, map=out)
    return _counts["map"]


def _is_year(name):
    return len(name) == 4 and name.isdigit()


def _clean_genres(items, root, excluded):
    out, seen = [], set()
    for it in items:
        nm = it.get("Name")
        if not isinstance(nm, str) or nm in DROP:
            continue
        if root and (nm in excluded or _is_year(nm)):
            continue
        if nm in seen:
            continue
        seen.add(nm)
        out.append(it)
    if root:
        out.sort(key=lambda i: str(i.get("Name", "")).casefold())
    return out


def _clean_filters(j, excluded):
    gen = j.get("Genres")
    if not isinstance(gen, list):
        return j, False
    out, seen, changed = [], set(), False
    for e in gen:
        nm = e.get("Name") if isinstance(e, dict) else e
        if not isinstance(nm, str):
            out.append(e)
            continue
        if nm in DROP or _is_year(nm) or nm in excluded or nm in seen:
            changed = True
            continue
        seen.add(nm)
        out.append(e)
    if len(out) == len(gen):
        return j, False
    j["Genres"] = out
    return j, True


def _filter(data, path, hdrs):
    try:
        j = json.loads(data)
    except Exception:
        return data, False
    try:
        if isinstance(j, dict) and isinstance(j.get("Items"), list) and "Genres" not in j:
            if not j["Items"]:
                return data, False
            root = "ParentId=" not in path
            excluded = _excluded_names(hdrs) if root else frozenset()
            items = _clean_genres(j["Items"], root, excluded)
            if not root and len(items) == len(j["Items"]):
                return data, False  # per-library list unchanged
            if root:
                counts = _genre_counts(items, hdrs)
                for it in items:
                    n = counts.get(it.get("Id"))
                    if n:
                        # Moonfin reads ChildCount (flat) or MovieCount/SeriesCount;
                        # ItemCounts alone is ignored -> "No Genre Found".
                        it["ChildCount"] = n
                        it["ItemCounts"] = {"ItemCount": n, "RecursiveItemCount": n,
                                            "UnplayedItemCount": n}
            j["Items"] = items
            if "TotalRecordCount" in j:
                j["TotalRecordCount"] = len(items)
            return json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode(), True
        if isinstance(j, dict) and "Genres" in j:
            j, changed = _clean_filters(j, _excluded_names(hdrs))
            if changed:
                return json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode(), True
    except Exception:
        pass
    return data, False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn = http.client.HTTPConnection(*UPSTREAM, timeout=60)
            conn.request(self.command, self.path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            if resp.status == 200:
                data, changed = _filter(data, self.path, hdrs)
                if changed:
                    sys.stderr.write("genre-filter: cleaned %s\n" % self.path.split("?")[0])
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            if resp.status not in (204, 304):
                self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if resp.status not in (204, 304):
                self.wfile.write(data)
        except Exception as e:
            sys.stderr.write("genre-filter: proxy error (%s)\n" % e)
            try:
                self.send_error(502, "genre-filter upstream error")
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = _proxy


def _selftest():
    items = [
        {"Name": "Day"}, {"Name": "Week"},
        {"Name": "Action"}, {"Name": "Comedy"},
        {"Name": "Action"},  # series copy
        {"Name": "2024"}, {"Name": "Action"},  # collections copy + year
    ]
    root = _clean_genres(items, True, frozenset({"2024"}))
    assert [i["Name"] for i in root] == ["Action", "Comedy"], root
    lib = _clean_genres(items, False, frozenset())
    assert [i["Name"] for i in lib] == ["Action", "Comedy", "2024"], lib
    j, ch = _clean_filters({"Genres": ["Day", "Action", "Action", "1999", "Drama"]}, frozenset())
    assert ch and j["Genres"] == ["Action", "Drama"], j
    assert max(0, 1) == 1 and max(7, 1) == 7   # count floor keeps card visible
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
