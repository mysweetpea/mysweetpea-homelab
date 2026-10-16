#!/usr/bin/env python3
"""AIOStreams Continue-Watching dedupe (sidecar).

nginx routes .../UserItems/Resume and .../Users/<uid>/Items/Resume here.
/UserItems/Resume is sorted by LastPlayedDate descending, and a show with
several partially-watched episodes lists EVERY one of them - which makes the
merged "Continue Watching & Next Up" home row repeat the same show 2-3 times.
We keep only the FIRST entry per series (== the most recently watched episode),
so each show gets exactly one card.

Only Episode-type items are grouped (key: SeriesId, else SeriesName, else Id);
everything else keeps its unique Id (movies/songs are never collapsed). Any
parse problem, crash, or unusual response passes through untouched, and nginx
falls back to the app on :3000 when this process is down (fail-open).
"""
import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 3000)
PORT = int(os.environ.get("PORT", "3005"))
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "accept-encoding",
       "content-length"}


def _dedupe(items):
    out, seen = [], set()
    for it in items:
        key = None
        if isinstance(it, dict):
            if (it.get("Type") or "") == "Episode":
                key = it.get("SeriesId") or it.get("SeriesName") or it.get("Id")
            else:
                key = it.get("Id")
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(it)
    return out


def _filter(data):
    """Resume payload -> deduped payload. Returns (bytes, changed)."""
    try:
        j = json.loads(data)
    except Exception:
        return data, False
    if not (isinstance(j, dict) and isinstance(j.get("Items"), list) and j["Items"]):
        return data, False
    try:
        items = _dedupe(j["Items"])
        if len(items) == len(j["Items"]):
            return data, False
        j["Items"] = items
        if isinstance(j.get("TotalRecordCount"), int):
            j["TotalRecordCount"] = len(items)
        return json.dumps(j, ensure_ascii=False, separators=(",", ":")).encode(), True
    except Exception:
        return data, False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy(self):
        try:
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn = http.client.HTTPConnection(*UPSTREAM, timeout=60)
            conn.request(self.command, self.path, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            if resp.status == 200:
                data, changed = _filter(data)
                if changed:
                    sys.stderr.write("resume-filter: deduped %s\n" % self.path.split("?")[0])
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
            sys.stderr.write("resume-filter: proxy error (%s)\n" % e)
            try:
                self.send_error(502, "resume-filter upstream error")
            except Exception:
                pass

    do_GET = do_HEAD = _proxy


def _selftest():
    items = [
        {"Type": "Movie", "Id": "m1", "Name": "Ocean"},
        {"Type": "Episode", "Id": "e3", "SeriesName": "Classroom of the Elite", "IndexNumber": 3},
        {"Type": "Episode", "Id": "c2", "SeriesName": "Chowder", "IndexNumber": 2},
        {"Type": "Episode", "Id": "c3", "SeriesName": "Chowder", "IndexNumber": 3},
        {"Type": "Episode", "Id": "e2", "SeriesName": "Classroom of the Elite", "IndexNumber": 2},
        {"Type": "Episode", "Id": "e1", "SeriesName": "Classroom of the Elite", "IndexNumber": 1},
        {"Type": "Episode", "Id": "c1", "SeriesName": "Chowder", "IndexNumber": 1},
    ]
    out = _dedupe(items)
    assert [i["Id"] for i in out] == ["m1", "e3", "c2"], out
    # SeriesId wins when present (future-proofing)
    out2 = _dedupe([{"Type": "Episode", "Id": "x", "SeriesId": "A", "SeriesName": "X"},
                    {"Type": "Episode", "Id": "y", "SeriesId": "A", "SeriesName": "X"}])
    assert [i["Id"] for i in out2] == ["x"]
    # payload shape: TotalRecordCount follows the dedupe
    body = json.dumps({"Items": items, "TotalRecordCount": 7, "StartIndex": 0}).encode()
    out, changed = _filter(body)
    assert changed
    d = json.loads(out)
    assert len(d["Items"]) == 3 and d["TotalRecordCount"] == 3, d["TotalRecordCount"]
    # untouched when nothing collapses
    solo = json.dumps({"Items": [items[0]], "TotalRecordCount": 1}).encode()
    _, changed2 = _filter(solo)
    assert not changed2
    # non-JSON / odd shapes untouched
    _, c3 = _filter(b"not json")
    assert not c3
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
