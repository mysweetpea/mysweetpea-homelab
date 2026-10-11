#!/usr/bin/env python3
# seerr-watchlist-bridge — Seerr watchlist -> auto-request, attributed per user.
#
# When a user adds something to their watchlist (Moonfin's Seerr rows / Seerr UI),
# this bridge turns it into a request FOR THAT USER (auto-approved by policy, so it
# fills immediately). First live run seeds state: pre-existing watchlist items are
# counted and logged but NOT requested (they were saved under old semantics).
#
# Runs */20 from master cron (flock-guarded). Log: /var/log/watchlist-bridge.log
# Usage: python3 seerr-watchlist-bridge.py [--dry-run]
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error, datetime

SEERR_BASES = ['http://10.43.186.155:5055', 'https://request.mysweetpea.cc']
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
STATE_FILE = '/root/.watchlist-bridge-state.json'
LOG = '/var/log/watchlist-bridge.log'
MAX_REQUESTS_PER_RUN = 10
DRY = '--dry-run' in sys.argv

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

def call(base, path, key, method='GET', body=None):
    """Returns (status, payload). status 0 = connection error (try next base)."""
    try:
        req = urllib.request.Request(base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-Api-Key': key, 'Content-Type': 'application/json', 'User-Agent': UA},
            method=method)
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200].decode(errors='replace')
    except Exception as e:
        return 0, str(e)

def seerr(path, key, method='GET', body=None):
    for base in SEERR_BASES:
        st, resp = call(base, path, key, method, body)
        if st != 0:
            return st, resp
    return 0, 'all bases unreachable'

key = secret('SEERR_API_KEY')
state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
seeded = state.get('seeded', False)
users_state = state.setdefault('users', {})

st, users = seerr('/api/v1/user', key)
if st != 200 or not isinstance(users, dict):
    log('cannot list users (%s) - exiting' % st)
    sys.exit(0)
ulist = users.get('results') or users if isinstance(users, list) else users.get('results') or []

pending = []
for u in ulist:
    uid = u.get('id')
    if uid is None:
        continue
    st, wl = seerr('/api/v1/user/%s/watchlist' % uid, key)
    if st != 200 or not isinstance(wl, dict):
        log('watchlist fetch failed for user %s (%s)' % (uid, st))
        continue
    items = wl.get('results') or []
    keys_now = []
    for it in items:
        if it.get('tmdbId') and it.get('mediaType'):
            keys_now.append('%s:%s' % (it['mediaType'], it['tmdbId']))
    prev = users_state.get(str(uid), [])
    new_keys = [k for k in keys_now if k not in prev]
    if not seeded and new_keys:
        log('seed run: user %s has %d pre-existing watchlist item(s) - NOT requesting them' % (uid, len(new_keys)))
    if seeded:
        for k in new_keys:
            title = next((it.get('title') for it in items if '%s:%s' % (it.get('mediaType'), it.get('tmdbId')) == k), '?')
            pending.append((uid, k, title))
    users_state[str(uid)] = keys_now

acted = 0
for uid, k, title in pending[:MAX_REQUESTS_PER_RUN]:
    mt, tmdb = k.split(':', 1)
    body = {'mediaType': mt, 'mediaId': int(tmdb), 'userId': uid}
    if mt == 'tv':
        st, d = seerr('/api/v1/tv/%s' % tmdb, key)
        seasons = [s.get('seasonNumber') for s in ((d or {}).get('seasons') or []) if (s.get('seasonNumber') or 0) >= 1]
        if seasons:
            body['seasons'] = seasons
    if DRY:
        log('WOULD request [%s] %s (user %s)' % (mt, title, uid))
        continue
    st, resp = seerr('/api/v1/request', key, 'POST', body)
    log('auto-request [%s] %s for user %s -> %s' % (mt, title, uid, st))
    if st == 201:
        acted += 1

state['seeded'] = True
if not DRY:
    json.dump(state, open(STATE_FILE, 'w'))
log('done. seeded=%s checked_users=%d pending=%d acted=%d dry=%s' % (seeded, len(ulist), len(pending), acted, DRY))
