#!/usr/bin/env python3
"""AIOStreams edge filter (sidecar) — two jobs:

1. Browser H.264 source filter: for browser UAs (not Moonfin/Stremio/Dart/etc.)
   every MediaSources array is reduced to H.264 entries only (when any exist).
2. Codec gate for players that crash or starve on big sources: browser UAs and
   Roku (4K HEVC 10-bit MKV kills the Roku player) get H.264-only MediaSources.
3. Series ChildCount fix: AIOStreams puts the EPISODE total in ChildCount, but
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
ROKU = re.compile(r"Roku", re.I)
# Phones/TVs stream over the same usenet pipe as everyone else; measured cold
# throughput is ~1.5-2.3 MB/s, so a 19 Mbps 4K source can never feed them.
MOBILE_TV = re.compile(r"Roku|CFNetwork|Darwin|Dalvik|okhttp", re.I)
BITRATE_CAP = 12_000_000
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


def _filt_obj(obj, require_h264=False, max_bitrate=None):
    if not isinstance(obj, dict):
        return False
    ms = obj.get("MediaSources")
    if not isinstance(ms, list) or not ms:
        return False
    keep = ms
    if require_h264:
        keep = [s for s in keep if _is_h264(s)]
    if max_bitrate:
        capped = [s for s in keep if (s.get("Bitrate") or 0) <= max_bitrate]
        keep = capped or keep          # never blank the list over a bitrate cap
    if keep and len(keep) < len(ms):
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


DETAIL_BATCH_MAX = 8


def _fix_series_batch(j, path, auth):
    """Rewrite Series counts inside small Items[] batches (detail fetches).

    Moonfin renders seasonsCount only on the detail screen, and detail rows
    carry 1-3 ids; browse rows carry 25+. Skipping big rows keeps cold-cache
    lookups at 1-3 per request instead of 25.
    """
    if not isinstance(j, dict):
        return False
    items = j.get("Items")
    if not isinstance(items, list):
        return False
    series = [it for it in items
              if isinstance(it, dict) and it.get("Type") == "Series"
              and isinstance(it.get("ChildCount"), int)]
    if not series or len(series) > DETAIL_BATCH_MAX:
        return False
    changed = False
    for it in series:
        if _fix_series(it, path, auth):
            changed = True
    return changed


VIEW_AR = 0.6667   # our logo tiles are 800x1200


def _fix_view_ar(j):
    """My Media tiles: aios reports 16:9 for every view (PrimaryImageAspectRatio
    1.7777) and Moonfin refuses to draw landscape primary art on a poster tile
    (home_screen._resolveRowImageUrl: AR >= 1 -> no image), so the tiles render
    blank. Our logo art is portrait, so report the truth.
    """
    if not isinstance(j, dict):
        return False
    if j.get("Type") != "CollectionFolder":
        return False
    if not (j.get("ImageTags") or {}).get("Primary"):
        return False
    if j.get("PrimaryImageAspectRatio") == VIEW_AR:
        return False
    j["PrimaryImageAspectRatio"] = VIEW_AR
    return True


def _limited(ua):
    """Players that must not be handed a source they cannot decode.

    Roku (DVP) chokes on 4K HEVC 10-bit MKV; browsers cannot decode HEVC at all.
    """
    if ROKU.search(ua):
        return True
    return bool(BROWSER.search(ua)) and not NATIVE.search(ua)


def _filter(data, ua, path, auth):
    try:
        j = json.loads(data)
    except Exception:
        return data, False
    changed = _fix_series(j, path, auth)
    if _fix_series_batch(j, path, auth):
        changed = True
    if _fix_view_ar(j):
        changed = True
    for _it in (j.get("Items") or []) if isinstance(j, dict) else []:
        if _fix_view_ar(_it):
            changed = True
    require_h264 = _limited(ua)
    max_br = BITRATE_CAP if MOBILE_TV.search(ua) else None
    if require_h264 or max_br:
        if isinstance(j, dict):
            if _filt_obj(j, require_h264, max_br):
                changed = True
            for it in j.get("Items") or []:
                if _filt_obj(it, require_h264, max_br):
                    changed = True
        elif isinstance(j, list):
            for it in j:
                if _filt_obj(it, require_h264, max_br):
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
    assert _fix_series_batch({"Items": []}, "/x", "") is False
    assert _fix_view_ar({"Type": "Movie", "ImageTags": {"Primary": "x"}}) is False
    assert _fix_view_ar({"Type": "CollectionFolder", "ImageTags": {"Primary": "x"},
                         "PrimaryImageAspectRatio": 1.7777}) is True
    assert _fix_series_batch(
        {"Items": [{"Type": "Series", "ChildCount": 317, "Id": "a"}] * 9},
        "/x", "") is False  # big rows skipped (no lookups)
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
    # Roku is a limited class too (4K HEVC crashes the player)
    assert _limited("Roku/DVP-15.3 (15.3.4.2402-51)")
    assert MOBILE_TV.search("Moonfin/30000149 CFNetwork/3860.700.2 Darwin/25.6.0")
    assert not MOBILE_TV.search("Mozilla/5.0 (compatible; Moonfin/2.7.0)")
    assert not _limited("Mozilla/5.0 (compatible; Moonfin/2.7.0)")
    assert not _limited("Lavf/62.12.100")
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
