#!/usr/bin/env python3
"""AceStream -> Jellyfin playlist + guide sync.

Mirrors the AceStream engine's channel index into
  * an M3U playlist for Jellyfin's (core) M3U tuner
  * a minimal XMLTV file (the index carries only the *current* programme per
    channel, so this is a now/next snapshot rebuilt every run)

Runs as an in-cluster CronJob: writes both files into a ConfigMap through the
Kubernetes API (stdlib only, no kubectl in the image) and asks Jellyfin to
re-run its guide refresh only when the content actually changed.

Env: ENGINE_URL ACEXY_URL CM_NAME CM_NAMESPACE JF_URL JF_KEY KUMA_PUSH_URL
     EXCLUDE_CATS CATEGORIES LANGUAGES EN_COUNTRIES MIN_AVAIL DRY_RUN
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENGINE = os.environ.get("ENGINE_URL", "http://acestream.private.svc:6878").rstrip("/")
ACEXY = os.environ.get("ACEXY_URL", "http://acestream.private.svc:8080").rstrip("/")
CM_NAME = os.environ.get("CM_NAME", "acestream-livetv")
CM_NS = os.environ.get("CM_NAMESPACE", "private")
JF_URL = os.environ.get("JF_URL", "http://jellyfin.private.svc:8096").rstrip("/")
JF_KEY = os.environ.get("JF_KEY", "")
KUMA = os.environ.get("KUMA_PUSH_URL", "")
EXCLUDE = {c.strip().lower() for c in os.environ.get("EXCLUDE_CATS", "erotic_18_plus,amateur").split(",") if c.strip()}
# Allowlist of index categories to keep; empty string = keep everything.
CATEGORIES = {c.strip().lower() for c in os.environ.get("CATEGORIES", "sport").split(",") if c.strip()}
LANGUAGES = {c.strip().lower() for c in os.environ.get("LANGUAGES", "eng").split(",") if c.strip()}
# ~570 of the index's entries carry no language at all; those are kept only when
# their country is English-speaking, never guessed from the name.
EN_COUNTRIES = {c.strip().lower() for c in
                os.environ.get("EN_COUNTRIES", "us,uk,gb,ca,au,ie,nz").split(",") if c.strip()}
MIN_AVAIL = float(os.environ.get("MIN_AVAIL", "0.2"))
DRY = os.environ.get("DRY_RUN", "") == "1"

# Jellyfin parses group-title but never exposes it (no channel-groups endpoint),
# so ordering comes from tvg-chno: sports occupy 100-299 in sport order, the
# rest follow. (regex, label, first number) - specific patterns before generic.
SPORT_TYPES = [
    (r"cricket", "Cricket", 210),
    (r"tennis", "Tennis", 220),
    (r"\bnba\b|basketball", "Basketball", 200),
    (r"\bnfl\b|american football", "American Football", 260),
    (r"\bmlb\b|baseball", "Baseball", 270),
    (r"\bnhl\b|hockey", "Hockey", 280),
    (r"\bf1\b|formula|motogp|nascar|indycar|motorsport", "Motorsport", 230),
    (r"golf", "Golf", 250),
    (r"\bufc\b|boxing|\bwwe\b", "Combat", 240),
    (r"football|soccer|skysp ?pl|sky sp ?pl|premier league|\bepl\b|\bmls\b", "Football", 100),
]
NON_SPORT = {
    "informational": ("News", 300), "tv": ("TV", 400), "entertaining": ("Entertainment", 450),
    "movies": ("Movies", 500), "kids": ("Kids", 600), "music": ("Music", 700),
    "religion": ("Religious", 800), "religious": ("Religious", 800),
    "teleshop": ("Shopping", 950),
}


def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "acesync/1"})
    with urllib.request.urlopen(req, timeout=timeout, context=None) as r:
        return r.read()


def fetch_index():
    out, page, total = [], 0, None
    while page < 400:
        try:
            d = json.loads(http(f"{ENGINE}/search?query=&page={page}", timeout=25))
        except Exception as e:  # a dead page must not wipe the playlist
            print(f"page {page} failed: {e}", file=sys.stderr)
            if out:
                break
            raise SystemExit(3)
        r = d.get("result") or {}
        total = total if total is not None else r.get("total")
        res = r.get("results") or []
        if not res:
            break
        out.extend(res)
        page += 1
    print(f"index: pages={page} groups={len(out)} reported_total={total}", flush=True)
    return out


def cats_of(group):
    s = set()
    for it in group.get("items") or []:
        if isinstance(it, dict):
            s.update(str(c).lower() for c in (it.get("categories") or []) if str(c).strip())
    return s


def langs_of(g):
    return {str(x).lower() for i in (g.get("items") or []) if isinstance(i, dict)
            for x in (i.get("languages") or [])}


def countries_of(g):
    return {str(x).lower() for i in (g.get("items") or []) if isinstance(i, dict)
            for x in (i.get("countries") or [])}


def classify(name, cats):
    """-> (group label, first channel number)."""
    low = name.lower()
    if "sport" in cats:
        for pat, label, base in SPORT_TYPES:
            if re.search(pat, low):
                return f"Sport: {label}", base
        return "Sport: Multi-Sport", 150
    cat = next(iter(sorted(cats))) if cats else "other"
    return NON_SPORT.get(cat, ("Other", 900))


def build(groups):
    seen_names, used_ids = set(), set()
    channels = []
    dropped = 0

    def slug(name):
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "ch"
        cand, k = base, 2
        while cand in used_ids:
            cand, k = f"{base}-{k}", k + 1
        used_ids.add(cand)
        return cand

    for g in groups:
        name = str(g.get("name") or "").strip()
        items = [i for i in (g.get("items") or []) if isinstance(i, dict) and i.get("infohash")]
        if not name or not items:
            continue
        if cats_of(g) & EXCLUDE:
            continue
        if CATEGORIES and not (cats_of(g) & CATEGORIES):
            continue
        lang = langs_of(g)
        if lang:
            if not (lang & LANGUAGES):
                dropped += 1
                continue
        elif not (countries_of(g) & EN_COUNTRIES):
            dropped += 1
            continue
        best = max(items, key=lambda i: (float(i.get("availability") or 0), int(i.get("bitrate") or 0)))
        if float(best.get("availability") or 0) < MIN_AVAIL:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)
        group, base = classify(name, cats_of(g))
        icon = ""
        for ic in (g.get("icons") or []):
            if isinstance(ic, dict) and ic.get("url"):
                icon = str(ic["url"])
                break
        channels.append({
            "id": slug(name),
            "name": name,
            "hash": str(best["infohash"]),
            "group": group,
            "base": base,
            "icon": icon,
            "epg": [e for e in (g.get("epg") or []) if isinstance(e, dict) and e.get("start")],
        })
    if dropped:
        print(f"language filter dropped {dropped}", flush=True)
    channels.sort(key=lambda c: (c["base"], c["name"].lower()))
    # ponytail: 50-wide number bands; a >50-channel group bleeds into the next
    # band (cosmetic only) - widen the bands if that ever happens.
    nxt = {}
    for c in channels:
        c["num"] = nxt.get(c["group"], c["base"])
        nxt[c["group"]] = c["num"] + 1
    return channels


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def xmltv_time(ts):
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(int(float(ts))))


def render(channels):
    m3u = ["#EXTM3U"]
    for c in channels:
        attrs = (f'tvg-id="{c["id"]}" tvg-chno="{c["num"]}" tvg-name="{xml_escape(c["name"])}" '
                 f'group-title="{xml_escape(c["group"])}"')
        if c["icon"]:
            attrs += f' tvg-logo="{xml_escape(c["icon"])}"'
        m3u.append(f'#EXTINF:-1 {attrs},{c["name"]}')
        m3u.append(f'{ACEXY}/ace/getstream?infohash={c["hash"]}')
    x = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
         '<tv generator-info-name="acesync">']
    now = time.time()
    for c in channels:
        x.append(f'  <channel id="{c["id"]}"><display-name>{xml_escape(c["name"])}</display-name>'
                 + (f'<icon src="{xml_escape(c["icon"])}"/>' if c["icon"] else "") + "</channel>")
    # One programme per channel so Jellyfin's "On Now" home row always has
    # something to show: the index's programme when one is actually airing,
    # otherwise a clearly-labelled placeholder. No event is ever invented.
    for c in channels:
        live = next((e for e in c["epg"]
                     if float(e["start"]) <= now < float(e.get("stop") or 0)), None)
        if live:
            start = float(live["start"])
            stop = float(live.get("stop") or start + 3600)
            title = live.get("name") or c["name"]
            desc = live.get("description") or ""
        else:
            start, stop = now - 300, now + 3 * 3600
            title = re.sub(r"\s*\[[A-Z]{2}\]$", "", c["name"])
            desc = "Live sports feed - no published schedule"
        x.append(f'  <programme start="{xmltv_time(start)}" stop="{xmltv_time(stop)}" channel="{c["id"]}">')
        x.append(f'    <title lang="en">{xml_escape(title)}</title>')
        if desc:
            x.append(f'    <desc lang="en">{xml_escape(desc)}</desc>')
        x.append("  </programme>")
    x.append("</tv>")
    return "\n".join(m3u) + "\n", "\n".join(x) + "\n"


def k8s(method, path, body=None):
    tok = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read().strip()
    ca = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    ctx = ssl.create_default_context(cafile=ca)
    url = "https://kubernetes.default.svc" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.loads(r.read() or b"{}")


def cm_apply(data):
    path = f"/api/v1/namespaces/{CM_NS}/configmaps/{CM_NAME}"
    body = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": CM_NAME, "namespace": CM_NS,
                     "labels": {"app.kubernetes.io/name": "acestream",
                                "app.kubernetes.io/component": "livetv"}},
        "data": data,
    }
    try:
        old = k8s("GET", path)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        k8s("POST", f"/api/v1/namespaces/{CM_NS}/configmaps", body)
        return None
    if (old.get("data") or {}) == data:
        return False
    k8s("PUT", path, body)
    return True


def jf_refresh():
    if not JF_KEY:
        return "no-key"
    h = {"Authorization": f"MediaBrowser Token={JF_KEY}", "Accept": "application/json"}
    tasks = json.loads(http(f"{JF_URL}/ScheduledTasks", headers=h))
    tid = next((t["Id"] for t in tasks
                if str(t.get("Key", "")).lower() == "refreshguide" or str(t.get("Name", "")).lower() == "refresh guide"), None)
    if not tid:
        return "no-task"
    http(f"{JF_URL}/ScheduledTasks/Running/{tid}", data=b"", headers={**h, "Content-Type": "application/json"}, timeout=15)
    return "triggered"


def main():
    channels = build(fetch_index())
    m3u, xml = render(channels)
    print(f"channels={len(channels)} m3u={len(m3u)}B xmltv={len(xml)}B", flush=True)
    if DRY:
        open("/tmp/acestream.m3u", "w").write(m3u)
        open("/tmp/acestream.xmltv", "w").write(xml)
        return 0
    changed = cm_apply({"acestream.m3u": m3u, "acestream.xmltv": xml})
    if changed:
        # kubelet needs up to ~2min to project a ConfigMap change into the
        # Jellyfin pod. Refreshing sooner reads the PREVIOUS file, and Jellyfin
        # then caches that for an hour (XmlTvListingsProvider _maxCacheAge).
        time.sleep(int(os.environ.get("MOUNT_SETTLE_SECONDS", "120")))
    print(f"configmap_changed={changed} refresh={jf_refresh() if changed else 'skipped'}", flush=True)
    if KUMA:
        try:
            print("kuma:", http(KUMA, timeout=10)[:40].decode(errors="replace"), flush=True)
        except Exception as e:
            print(f"kuma push failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
