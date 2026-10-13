#!/usr/bin/env python3
"""AIOStreams browser H.264 source filter (sidecar).

nginx routes PlaybackInfo + item detail/list responses here; we forward to the
AIOStreams app on 127.0.0.1:3000. For browser UAs (not Moonfin/Stremio/Dart/etc.)
every MediaSources array is reduced to H.264 entries only (when any exist), and
the JSON is space-padded back to the original byte length so Content-Length
stays valid. Native clients and any parse issue pass through byte-for-byte.

Note: Moonfin sends mediaSourceId on EVERY PlaybackInfo call - never treat that
as a "user picked a version" signal (v1 did, which made the filter a no-op).
"""
import http.client
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 3000)
BROWSER = re.compile(r"Mozilla/5\.0")
NATIVE = re.compile(r"(Moonfin|Stremio|Dart|okhttp|Dalvik|Lavf)")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "accept-encoding"}


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


def _filter(data, ua):
    if not BROWSER.search(ua) or NATIVE.search(ua):
        return data, False
    try:
        j = json.loads(data)
    except Exception:
        return data, False
    changed = False
    if isinstance(j, dict):
        changed = _filt_obj(j)
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
                data, changed = _filter(data, self.headers.get("User-Agent", ""))
                if changed:
                    sys.stderr.write("pi-filter: filtered %s\n" % self.path.split("?")[0])
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


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", 3001), Handler)
    sys.stderr.write("pi-filter: listening on 127.0.0.1:3001\n")
    srv.serve_forever()
