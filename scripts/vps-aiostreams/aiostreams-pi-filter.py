#!/usr/bin/env python3
"""AIOStreams edge filter (sidecar) — two jobs:

1. Browser H.264 source filter: for browser UAs (not Moonfin/Stremio/Dart/etc.)
   every MediaSources array is reduced to H.264 entries only (when any exist).
2. Series ChildCount fix: AIOStreams puts the EPISODE total in ChildCount, but
   Jellyfin semantics are ChildCount = number of seasons (Moonfin renders it as
   "N Seasons"). For single-Series item responses we replace it with the real
   season count from /Shows/{id}/Seasons (6h cache).

JSON is space-padded back to its original byte length so Content-Length stays
valid; any parse/upstream issue passes through byte-for-byte.
"""
import http.client
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 3000)
BROWSER = re.compile(r"Mozilla/5\.0")
NATIVE = re.compile(r"(Moonfin|Stremio|Dart|okhttp|Dalvik|Lavf)")
UID_RE = re.compile(r"/Users/([0-9a-fA-F]{32})/Items/")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "accept-encoding"}

_SEASONS = {}          # series id -> (count, ts)
_SEASONS_TTL = 21600
_SEASONS_LOCK = threading.Lock()


def _is_h264(src):
    for st in src.get("MediaStreams") or []:
        if st.get("Type") == "Video":
            return st.get("Codec") in ("h264", "avc1")
    return False


def _filt_obj(obj):
    if not isinstance(obj, dict):
        return False
    ms = obj.get("MediaSources")
    if isinstance(ms, list) and ms:
        keep = [s for s in ms if _is_h264(s)]
        if 0 < len(keep) < len(ms):
            obj["MediaSources"] = keep
            return True
    return False


def _season_count(series_id, uid, auth):
    now = time.time()
    with _SEASONS_LOCK:
        hit = _SEASONS.get(series_id)
        if hit and now - hit[1] < _SEASONS_TTL:
            return hit[0]
    path = "/jellyfin/Shows/%s/Seasons" % series_id
    if uid:
        path += "?UserId=" + uid
    try:
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=6)
        hdrs = {"Accept": "application/json"}
        if auth:
            hdrs["Authorization"] = auth
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        n = int(body.get("TotalRecordCount") or len(body.get("Items") or []))
    except Exception as e:
        sys.stderr.write("pi-filter: seasons lookup failed %s (%s)\n" % (series_id, e))
        return None
    if n:
        with _SEASONS_LOCK:
            if len(_SEASONS) > 4000:
                _SEASONS.clear()
            _SEASONS[series_id] = (n, now)
    return n


def _fix_series(j, path, auth):
    """ChildCount on a Series = seasons, not episodes."""
    if not isinstance(j, dict):
        return False
    if j.get("Type") != "Series":
        return False
    cc = j.get("ChildCount")
    if not isinstance(cc, int) or cc <= 0:
        return False
    sid = j.get("Id")
    if not isinstance(sid, str):
        return False
    m = UID_RE.search(path)
    n = _season_count(sid, m.group(1) if m else None, auth)
    if not n or n == cc:
        return False
    j["ChildCount"] = n
    return True


def _filter(data, ua, path, auth):
    try:
        j = json.loads(data)
    except Exception:
        return data, False
    changed = _fix_series(j, path, auth)
    if BROWSER.search(ua) and not NATIVE.search(ua):
        if isinstance(j, dict):
            if _filt_obj(j):
                changed = True
            for it in j.get("Items") or []:
                if _filt_obj(it):
                    changed = True
        elif isinstance(j, list):
            for it in j:
                if _filt_obj(it):
                    changed = True
    if not changed:
        return data, False
    out = json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode()
    if len(out) > len(data):
        return data, False          # safety: never emit a mismatched Content-Length
    out += b" " * (len(data) - len(out))
    return out, True


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
                data, changed = _filter(
                    data,
                    self.headers.get("User-Agent", ""),
                    self.path,
                    self.headers.get("Authorization", ""),
                )
                if changed:
                    sys.stderr.write("pi-filter: rewrote %s\n" % self.path.split("?")[0])
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
            sys.stderr.write("pi-filter: proxy error (%s)\n" % e)
            try:
                self.send_error(502, "pi-filter upstream error")
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = _proxy


def _selftest():
    # _fix_series only rewrites Series dicts; everything else passes through.
    assert _fix_series({"Type": "Movie", "ChildCount": 3}, "/x", "") is False
    assert _fix_series({"Type": "Series", "ChildCount": 0, "Id": "a"}, "/x", "") is False
    # _filter returns original bytes when nothing changes
    raw = b'{"Type":"Movie","Id":"a"}'
    assert _filter(raw, "Mozilla/5.0", "/x", "")[0] == raw
    # source filter keeps h264 only, preserves length
    body = json.dumps({"MediaSources": [
        {"MediaStreams": [{"Type": "Video", "Codec": "hevc"}]},
        {"MediaStreams": [{"Type": "Video", "Codec": "h264"}]},
    ]}).encode()
    out, ch = _filter(body, "Mozilla/5.0", "/x", "")
    assert ch and len(out) == len(body) and b"hevc" not in out
    # native UA keeps its sources
    out2, ch2 = _filter(body, "Moonfin/2.7.0", "/x", "")
    assert not ch2 and out2 == body
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", 3001), Handler)
    sys.stderr.write("pi-filter: listening on 127.0.0.1:3001\n")
    srv.serve_forever()
