#!/usr/bin/env python3
"""Reorder the My Media streamer tiles: alternate type (Movies/Shows) while
rotating services so no two neighbouring tiles share a service.

Front block becomes:
  Netflix Movies, Hulu Shows, Crunchyroll Movies, Disney+ Shows, Prime Movies,
  HBO Max Shows, Paramount+ Movies, Peacock Shows, Apple TV+ Movies,
  Discovery+ Shows, Canal+ Movies, Netflix Shows, Hulu Movies,
  Crunchyroll Shows, Disney+ Movies, Prime Shows, HBO Max Movies,
  Paramount+ Shows, Peacock Movies, Apple+ Shows, Canal+ Shows
then everything else keeps its previous order.

Dry-run by default; pass --apply to PUT the config (aios rate-limits config
writes ~10/hr, so exactly one call).
"""
import base64
import json
import sys
import urllib.request
import shutil

cfg = json.load(open("/opt/moonbase-lite/config.json"))
base = "http://127.0.0.1:3000"
auth = base64.b64encode(
    f"{cfg['config_uuid']}:{cfg['config_password']}".encode()).decode()
H = {"Authorization": f"Basic {auth}"}

ud = json.load(urllib.request.urlopen(
    urllib.request.Request(base + "/api/v1/user", headers=H), timeout=20))["data"]["userData"]

ORDER = [
    "tsfe3b0.netflix-movies", "tsfe3b0.hulu-shows", "tsfe3b0.crunchyroll-movies",
    "tsfe3b0.disney-shows", "tsfe3b0.prime-movies", "tsfe3b0.hbo-shows",
    "tsfe3b0.paramount-movies", "tsfe3b0.peacock-shows", "tsfe3b0.apple-movies",
    "tsfe3b0.discovery-shows", "tsfe3b0.canal-movies",
    "tsfe3b0.netflix-shows", "tsfe3b0.hulu-movies", "tsfe3b0.crunchyroll-shows",
    "tsfe3b0.disney-movies", "tsfe3b0.prime-shows", "tsfe3b0.hbo-movies",
    "tsfe3b0.paramount-shows", "tsfe3b0.peacock-movies", "tsfe3b0.apple-shows",
    "tsfe3b0.canal-shows",
]

mods = ud.get("catalogModifications", [])
by_id = {m["id"]: m for m in mods if m.get("id") in ORDER}
assert len(by_id) == len(ORDER), f"expected 21 service mods, found {len(by_id)}"
rest = [m for m in mods if m.get("id") not in ORDER]
new = [by_id[i] for i in ORDER] + rest
assert len(new) == len(mods)

print("new head:")
for m in new[:28]:
    print("  %s %s" % ("ON " if m.get("enabled", True) else "off", m.get("id")))
print("total mods:", len(new), "(was", len(mods), ")")

if "--apply" in sys.argv:
    with open("/tmp/aios-ud-prereorder.json", "w") as f:
        json.dump(ud, f)
    ud["catalogModifications"] = new
    body = json.dumps({"config": ud}).encode()
    req = urllib.request.Request(base + "/api/v1/user", data=body, method="PUT",
                                 headers={**H, "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=30)
    print("PUT ->", resp.status, "backup at /tmp/aios-ud-prereorder.json")
else:
    print("dry run only; add --apply to write")
