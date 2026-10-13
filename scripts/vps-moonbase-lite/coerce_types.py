#!/usr/bin/env python3
"""Coerce a moonbase-lite profile JSON to the typed shapes the Moonfin client's
settings-sync applies expect.

Why: the client's apply code is strictly typed (`_applyBool` requires `value is
bool`, `_applyInt` `value is int`, `_applyStringList` `value is List`...). Flat
string values are silently SKIPPED — e.g. mediaBarLibraryIds stored as
"a,b,c" never lands, and the media bar keeps walking every library.

Usage:  coerce_types.py <profile.json|users/*.json> [...]
Map:    codec_map.json = (serverKey -> SyncCodec) extracted from the client's
        lib/data/services/synced_fields.dart (308 fields).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "codec_map.json")) as f:
    MAP = json.load(f)


def coerce(key, v):
    c = MAP.get(key)
    if c is None:
        return v
    if c == "boolean":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
        return v
    if c in ("integer", "textAsInt"):
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        try:
            return int(str(v).strip())
        except Exception:
            return v
    if c == "decimal":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        try:
            return float(str(v).strip())
        except Exception:
            return v
    if c == "csvList":
        if isinstance(v, list):
            return v
        s = str(v).strip()
        return [x for x in s.split(",") if x] if s else []
    return v  # text / enumName / colorAsText / intAsText stay strings


def walk(d, counter):
    for k in list(d.keys()):
        v = d[k]
        if isinstance(v, dict):
            walk(v, counter)
            continue
        after = coerce(k, v)
        if after != v or type(after) is not type(v):
            counter[0] += 1
        d[k] = after


def main(paths):
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        counter = [0]
        walk(d, counter)
        with open(p, "w") as f:
            json.dump(d, f, indent=2)
        print(f"{p}: coerced {counter[0]} fields")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
