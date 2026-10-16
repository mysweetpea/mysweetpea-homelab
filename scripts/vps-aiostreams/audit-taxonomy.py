#!/usr/bin/env python3
"""One-off taxonomy accuracy audit (READ-ONLY). Verifies:
 A) every TMDB keyword/genre id the addon uses -> official TMDB name (catches typos)
 B) live catalog samples: top items satisfy the catalog's OWN filter, checked against
    per-item TMDB metadata (ground truth, independent of discover)
 C) today's playlist rows (rows.json): items match the row theme
 D) search-behavior probes ('cartoons', etc.)
Run on the VPS: sudo python3 /root/taxaudit.py
"""
import importlib.util, json, urllib.parse, urllib.request, time, sys

spec = importlib.util.spec_from_file_location("tms", "/opt/aiostreams/tmdb-search.py")
tms = importlib.util.module_from_spec(spec); spec.loader.exec_module(tms)
KEY = tms._key()
CALLS = [0]

def tmdb(path, **q):
    q["api_key"] = KEY
    url = f"https://api.themoviedb.org/3/{path}?" + urllib.parse.urlencode(q)
    last = None
    for _ in (1, 2):
        CALLS[0] += 1
        try:
            with urllib.request.urlopen(url, timeout=12) as r:
                return json.load(r)
        except Exception as e:
            last = e; time.sleep(0.6)
    return {"__err__": str(last)}

_detail = {}
def detail(kind, tid):
    k = (kind, tid)
    if k not in _detail:
        _detail[k] = tmdb(f"{'movie' if kind == 'movie' else 'tv'}/{tid}",
                          append_to_response="keywords")
    return _detail[k]

def dg(d):  return {g["id"] for g in d.get("genres", [])}
def dkw(d):
    kw = d.get("keywords", {})
    return {k["id"] for k in (kw.get("keywords") or kw.get("results") or [])}

def satisfies(kind, params, tid):
    d = detail(kind, tid)
    if "__err__" in d:
        return "ERR", d["__err__"]
    g, kw = dg(d), dkw(d)
    for part in str(params.get("with_genres", "")).split(","):
        if not part:
            continue
        opts = {int(x) for x in part.split("|")}
        if not (g & opts):
            return "FAIL", f"genre {part} missing (has {sorted(g)})"
    for kid in str(params.get("with_keywords", "")).split(","):
        if not kid:
            continue
        if int(kid) not in kw:
            return "FAIL", f"kw {kid} missing"
    return "PASS", ""

def addon_cat(kind, cid, n=6):
    url = f"http://127.0.0.1:3010/catalog/{kind}/{cid}.json"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r).get("metas", [])[:n]

def decode_aid(s):
    """aios item id: a1<12|22><01|02>000000<3B BE tmdb>ffffffff000000"""
    if not s or len(s) != 32:
        return None, None
    try:
        kind = "movie" if s[2:4] == "12" else "series"
        return kind, int(s[12:18], 16)
    except ValueError:
        return None, None

# ---------------- A. id -> name ----------------
print("== A. id verification ==")
EXPECT_KW = {
    210024: "anime", 237451: "isekai", 10046: "mecha", 9914: "slice of life",
    6075: "sport", 315058: "horror", 33722: "true crime", 6158: "cult",
    9840: "romance", 282633: "history", 4344: "musical", 193171: "sitcom",
    9672: "true story", 10051: "heist", 470: "spy", 4379: "time travel",
    9882: "space", 12377: "zombie", 10683: "coming of age", 6152: "supernatural",
    208788: "medical", 703: "detective", 9716: "stand-up", 10123: "dark comedy",
    9715: "superhero", 12565: "psychological thriller", 316362: "thriller",
}
badkw = []
for kid, exp in sorted(EXPECT_KW.items()):
    d = tmdb(f"keyword/{kid}")
    name = d.get("name", d.get("__err__", "?"))
    n = name.lower().replace("-", " ").replace(" ", "")
    e = exp.lower().replace("-", " ").replace(" ", "")
    if n and (n in e or e in n):
        print(f"  {kid:>7} = '{name}'  ok (~{exp})")
    else:
        cand = tmdb("search/keyword", query=exp)
        cs = ", ".join(f"{x['id']}:{x['name']}" for x in cand.get("results", [])[:3])
        print(f"  {kid:>7} = '{name}'  <<< EXPECTED ~{exp} — candidates: {cs}")
        badkw.append((kid, exp, name))

gm = {g["id"]: g["name"] for g in tmdb("genre/movie/list").get("genres", [])}
gt = {g["id"]: g["name"] for g in tmdb("genre/tv/list").get("genres", [])}
EXPECT_G = {
    16: "Animation", 10751: "Family", 80: "Crime", 9648: "Mystery", 36: "History",
    10768: "War & Politics", 37: "Western", 10402: "Music", 10764: "Reality",
    10762: "Kids", 28: "Action", 12: "Adventure", 35: "Comedy", 14: "Fantasy",
    878: "Science Fiction", 10765: "Sci-Fi & Fantasy", 10759: "Action & Adventure",
    27: "Horror", 18: "Drama", 99: "Documentary", 53: "Thriller", 10752: "War",
    10749: "Romance",
}
badg = []
for gid, exp in sorted(EXPECT_G.items()):
    name = gm.get(gid) or gt.get(gid) or "?"
    if name != "?" and (exp.lower().replace("&", "").replace(" ", "")
                        in name.lower().replace("&", "").replace(" ", "")
                        or name.lower().replace("&", "").replace(" ", "")
                        in exp.lower().replace("&", "").replace(" ", "")):
        pass
    else:
        print(f"  genre {gid:>6} = '{name}'  <<< EXPECTED ~{exp} (movie:{gm.get(gid)} tv:{gt.get(gid)})")
        badg.append(gid)
print(f"  genres: {len(EXPECT_G) - len(badg)}/{len(EXPECT_G)} ok; keywords: "
      f"{len(EXPECT_KW) - len(badkw)}/{len(EXPECT_KW)} ok")

# ---------------- B. live catalog samples ----------------
print("== B. live catalog samples (top items vs own filter) ==")
SAMPLES = [
    ("movie", "animation-movies"), ("series", "animation-shows"),
    ("series", "sitcoms-shows"), ("movie", "standup-movies"),
    ("movie", "zombie-movies"), ("series", "zombie-shows"),
    ("series", "time-travel-shows"), ("series", "true-crime-shows"),
    ("movie", "true-story-movies"), ("series", "medical-shows"),
    ("series", "history-shows"), ("series", "kdrama-shows"),
    ("movie", "anime-movies"), ("series", "spy-shows"),
    ("series", "detective-shows"), ("series", "reality-shows"),
    ("movie", "coming-of-age-movies"), ("series", "supernatural-shows"),
]
for kind, cid in SAMPLES:
    params = tms.DISCOVER_DEFS.get((kind, cid))
    if not params:
        print(f"  {cid}: NO PARAMS <<<")
        continue
    try:
        metas = addon_cat(kind, cid, 6)
    except Exception as e:
        print(f"  {cid}: fetch failed: {e}")
        continue
    res, names, fails = [], [], []
    for m in metas:
        tid = int(m["id"].split(":")[1])
        st, why = satisfies(kind, params, tid)
        res.append(st)
        names.append(m["name"][:26])
        if st != "PASS":
            fails.append(f"{m['name'][:30]}: {why}")
    ok = res.count("PASS")
    flag = "" if ok == len(res) else "  <<< " + " | ".join(fails[:3])
    print(f"  {cid}: {ok}/{len(res)} pass — {', '.join(names[:4])}{flag}")

# ---------------- C. playlist rows ----------------
print("== C. playlist rows (today) ==")
try:
    rj = json.load(open("/opt/moonbase-lite/data/rows.json"))
except Exception as e:
    rj = None
    print(f"  rows.json unreadable: {e}")
if rj is not None:
    rows = rj.get("rows", rj) if isinstance(rj, dict) else rj
    pairs = list(rows.items()) if isinstance(rows, dict) else [(r.get("id", "?"), r) for r in rows]
    if pairs:
        print(f"  rows.json: {type(rj).__name__}, {len(pairs)} rows, "
              f"row type {type(pairs[0][1]).__name__}")
    FIXED = {"msp-action": "action", "msp-afterdark": "horror",
             "msp-crime": "true-crime", "msp-kdrama": "korean"}
    for rid, row in pairs:
        if not isinstance(row, dict):
            row = {"items": row} if isinstance(row, list) else {}
        items = row.get("items", [])
        name = row.get("name") or row.get("title") or rid
        exp = FIXED.get(rid)
        if exp is None:
            exp = tms.match_intent(str(name).lower().replace("anime:", "anime"))
        side = None
        if exp:
            mv, ser = tms.INTENTS.get(exp, (None, None))
            side = (mv, ser)
        if not exp or (side and side[0] is None and side[1] is None):
            print(f"  {name}: {len(items)} items — MIXED/untagged, skip check")
            continue
        if exp == "new-releases" or exp == "trending":
            print(f"  {name}: {len(items)} items — broad-by-design, skip check")
            continue
        nm = [(i.get("Name") or i.get("name") or "?") if isinstance(i, dict) else "?"
              for i in items[:6]]
        ck, fails = [], []
        for i in items[:6]:
            aid = ((i.get("Id") or i.get("id") or "") if isinstance(i, dict) else str(i))
            ik, tid = decode_aid(aid)
            if not ik:
                fails.append(f"bad id {aid[:12]}")
                continue
            p = side[0 if ik == "movie" else 1]
            if not p:
                fails.append(f"{nm[len(ck)]}: no {ik} side")
                ck.append("N/A")
                continue
            st, why = satisfies(ik, p, tid)
            ck.append(st)
            if st != "PASS":
                fails.append(f"{nm[len(ck) - 1][:30]}: {why}")
        print(f"  {name}: {ck.count('PASS')}/{len(ck)} pass [{exp}] — "
              f"{', '.join(x[:22] for x in nm[:4])}"
              + ("  <<< " + " | ".join(fails[:3]) if fails else ""))

# ---------------- D. search probes ----------------
print("== D. search probes ==")
for kind, kl in (("movie", "movie"), ("tv", "series")):
    for term in ("cartoons", "cartoon", "cartoons shows", "scary", "funny"):
        try:
            out = tms.metas_for(kind, term)
            print(f"  {kl} {term!r} -> {[m['name'][:28] for m in out[:5]]}")
        except Exception as e:
            print(f"  {kl} {term!r} ERR {e}")

print(f"== done: {CALLS[0]} TMDB calls, {len(_detail)} items inspected ==")
