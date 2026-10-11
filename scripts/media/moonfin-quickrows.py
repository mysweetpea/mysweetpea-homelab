#!/usr/bin/env python3
# moonfin-quickrows — decision-helper Jellyfin collections for the Moonfin home screen.
# The Moonfin client renders JF collections via its native 'collections' home section.
# Collections (created/refreshed daily):
#   ⚡ Under 30 Minutes · 🍽 Dinner & a Show · 🎬 Movie Night · 🏁 Nearly Done · 🎲 Surprise Me · 🇰🇷 K-Dramas
# K-Dramas also get the tag "kdrama" (findable via search/filters) — detected from Sonarr originalLanguage=ko.
# Usage: python3 moonfin-quickrows.py [--dry-run]   Log: /var/log/moonfin-quickrows.log
import base64, json, os, random, subprocess, sys, urllib.request, urllib.error, urllib.parse, datetime

JF = 'http://10.43.244.254:8096'
ADMIN_UID = '3d75456ef9c5438480195d731242133d'
LOG = '/var/log/moonfin-quickrows.log'
DRY = '--dry-run' in sys.argv
random.seed(datetime.date.today().isoformat())  # stable within a day, rotates daily

def log(msg):
    line = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg
    print(line)
    if not DRY:
        with open(LOG, 'a') as f:
            f.write(line + '\n')

def secret(key):
    out = subprocess.run(['kubectl', '-n', 'monitoring', 'get', 'secret', 'homepage-secrets',
                          '-o', 'jsonpath={.data.%s}' % key], capture_output=True, text=True)
    return base64.b64decode(out.stdout.strip()).decode().strip()

JF_KEY = secret('JELLYFIN_API_KEY')
JH = {'Authorization': 'MediaBrowser Token="%s"' % JF_KEY, 'Content-Type': 'application/json'}

def jf(path, method='GET', body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(JF + path, data=data, headers=JH, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        log('ERR %s %s -> %s %s' % (method, path[:90], e.code, e.read()[:100].decode(errors='replace')))
        return None

def q(path):
    return jf('/Items?userId=%s&Recursive=true&%s' % (ADMIN_UID, path)) or {}

def ensure_collection(name):
    found = q('IncludeItemTypes=BoxSet&SearchTerm=%s' % urllib.parse.quote(name)).get('Items') or []
    for c in found:
        if c.get('Name') == name:
            return c.get('Id')
    if DRY:
        log('WOULD create collection %s' % name)
        return None
    r = jf('/Collections?name=%s' % urllib.parse.quote(name), 'POST')  # JF quirk: query params, not body
    return (r or {}).get('Id')

def set_items(coll_id, want_ids):
    if not coll_id or DRY:
        log('WOULD set %d items' % len(want_ids))
        return
    cur = (jf('/Items?userId=%s&parentId=%s&Recursive=true&Limit=500' % (ADMIN_UID, coll_id)) or {}).get('Items') or []
    cur_ids = {i.get('Id') for i in cur}
    want = set(want_ids)
    add = list(want - cur_ids)
    rem = list(cur_ids - want)
    if add:
        jf('/Collections/%s/Items?ids=%s' % (coll_id, ','.join(add)), 'POST', {})
    if rem:
        jf('/Collections/%s/Items?ids=%s' % (coll_id, ','.join(rem)), 'DELETE')
    log('  %s: +%d -%d (total %d)' % (coll_id[:8], len(add), len(rem), len(want)))

def sample(items, n):
    random.shuffle(items)
    return items[:n]

# ---------- gathers ----------
eps = (q('IncludeItemTypes=Episode&Filters=IsUnplayed&Limit=2000&Fields=RunTimeTicks,SeriesName,ParentId').get('Items') or [])
eps = [e for e in eps if (e.get('RunTimeTicks') or 0) > 0]
movies = (q('IncludeItemTypes=Movie&Filters=IsUnplayed&Limit=1000&Fields=RunTimeTicks,CommunityRating').get('Items') or [])
movies = [m for m in movies if (m.get('RunTimeTicks') or 0) > 0]
resume = {}
for ruid in (ADMIN_UID, '3b21b627048b425a9c1138b1ca8372bb'):
    r = jf('/UserItems/Resume?userId=%s&Limit=80&MediaTypes=Video&Fields=PlayedPercentage' % ruid)
    for it in (r.get('Items') or []):
        if (it.get('UserData') or {}).get('PlayedPercentage', 0) >= 25:
            resume[it.get('Id')] = it.get('Name')

under30 = sample([e for e in eps if (e.get('RunTimeTicks') or 0) <= 30 * 60 * 10**7], 40)
dinner = sample([e for e in eps if (e.get('RunTimeTicks') or 0) <= 40 * 60 * 10**7 and 'comedy' in json.dumps(e).lower()], 40) or under30[:40]
movienight = sample([m for m in movies if 80 * 60 * 10**7 <= (m.get('RunTimeTicks') or 0) <= 120 * 60 * 10**7], 40)
surprise = sample(eps, 20) + sample(movies, 10)

# ---------- K-Dramas (from Sonarr originalLanguage=ko) ----------
k_ids, k_tagged = [], 0
try:
    sk = secret('SONARR_API_KEY')
    req = urllib.request.Request('http://192.168.20.219:8989/api/v3/series', headers={'X-Api-Key': sk})
    series = json.load(urllib.request.urlopen(req, timeout=60))
    ko = [s for s in series if (s.get('originalLanguage') or {}).get('id') == 12 or (s.get('originalLanguage') or {}).get('name') == 'Korean']
    all_series = (q('IncludeItemTypes=Series&Limit=1000').get('Items') or [])
    def norm(t):
        return ''.join(ch for ch in (t or '').lower() if ch.isalnum())
    by_name = {}
    for j in all_series:
        by_name.setdefault(norm(j.get('Name')), j)
    for s in ko:
        j = by_name.get(norm(s.get('title')))
        if j:
            k_ids.append(j.get('Id'))
            if 'kdrama' not in (j.get('Tags') or []):
                if DRY:
                    log('WOULD tag kdrama: %s' % j.get('Name'))
                else:
                    full = jf('/Items/%s?userId=%s' % (j.get('Id'), ADMIN_UID)) or {}
                    full['Tags'] = list(set((full.get('Tags') or []) + ['kdrama']))
                    jf('/Items/%s' % j.get('Id'), 'POST', full)
                    k_tagged += 1
except Exception as e:
    log('kdrama detection failed: %s' % str(e)[:80])

# ---------- genre collections: searchable category hubs ("romance"/"comedy"/"action"... find these) ----------
genre_colls = []
try:
    g = jf('/Genres?userId=%s&IncludeItemTypes=Movie,Series&Recursive=true' % ADMIN_UID)
    genres = [x.get('Name') for x in (g.get('Items') or []) if x.get('Name')]
    for gname in genres[:30]:
        cnt = q('IncludeItemTypes=Movie,Series&Genres=%s&Limit=0' % urllib.parse.quote(gname)).get('TotalRecordCount') or 0
        if cnt < 6:
            continue
        items = (q('IncludeItemTypes=Movie,Series&Genres=%s&Limit=60&SortBy=CommunityRating,SortName&SortOrder=Descending' % urllib.parse.quote(gname)).get('Items') or [])
        if items:
            genre_colls.append((gname, items))
except Exception as e:
    log('genre collections failed: %s' % str(e)[:80])

log('pools: eps=%d movies=%d resume=%d kdrama=%d (tagged %d) | genres: %d' % (len(eps), len(movies), len(resume), len(k_ids), k_tagged, len(genre_colls)))

# ---------- apply ----------
plan = [
    ('⚡ Under 30 Minutes', under30),
    ('🍽 Dinner & a Show', dinner),
    ('🎬 Movie Night', movienight),
    ('🏁 Nearly Done', [{'Id': i} for i in list(resume)[:40]]),
    ('🎲 Surprise Me', surprise),
    ('🇰🇷 K-Dramas', [{'Id': i} for i in k_ids]),
]
plan += genre_colls  # one collection per genre with 6+ titles (search "romance" etc. finds these)
for name, items in plan:
    ids = [i.get('Id') for i in items]
    cid = ensure_collection(name)
    log('%s: %d items' % (name, len(ids)))
    set_items(cid, ids)
log('done. dry=%s' % DRY)
