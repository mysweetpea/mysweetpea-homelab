#!/usr/bin/env python3
"""AIOStreams PlaybackInfo browser filter (sidecar).

nginx routes /PlaybackInfo requests here; we forward to the AIOStreams app
on 127.0.0.1:3000. For browser UAs (not Moonfin/Stremio/Dart/etc.) the JSON
response keeps only H.264 sources when any exist, padded with spaces to the
original byte length so Content-Length stays valid. Native clients and any
parse problem pass through byte-for-byte.
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


def _filter(data, ua):
    if not BROWSER.search(ua) or NATIVE.search(ua):
        return data, False
    try:
        j = json.loads(data)
        srcs = j.get("MediaSources") or []
        keep = [s for s in srcs if _is_h264(s)]
        if 0 < len(keep) < len(srcs):
            j["MediaSources"] = keep
            out = json.dumps(j).encode()
            if len(out) < len(data):
                out += b" " * (len(data) - len(out))
            return out, True
    except Exception as e:
        sys.stderr.write("pi-filter: parse fallback (%s)\n" % e)
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
            filt = "/PlaybackInfo" in self.path and "mediaSourceId" not in self.path
            if filt and resp.status == 200:
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
