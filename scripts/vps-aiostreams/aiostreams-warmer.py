#!/usr/bin/env python3
"""AIOStreams media-server pre-warmer.

Keeps the /jellyfin endpoints that Moonfin/Jellyfin clients hit hot:
per-view item rows, Items/Latest rows, and batched item metadata
(meta TTL is 6h; this runs every 3h so nothing ever goes cold on a client).

Root cron (see crontab): 13 */3 * * * flock -n /tmp/aiostreams-warmer.lock python3 /root/aiostreams-warmer.py
"""
import json
import os
import sys
import time
import urllib.request

BASE = 'https://aiostreams.mysweetpea.cc/jellyfin'
CREDS = '/root/.aiostreams-warmer-creds.json'
STATE = '/root/.aiostreams-warmer-state.json'
LOGFILE = '/var/log/aiostreams-warmer.log'

BUDGET_S = 600          # hard cap for the whole run
VIEW_LIMIT = 25         # never more than this many views
IDS_PER_VIEW = 40       # top item ids per view queued for meta warm
META_BATCH = 40         # ids per /Items?Ids= call
MAX_META_BATCHES = 30   # hard cap on meta batches per run


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    print(line, flush=True)
    try:
        with open(LOGFILE, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def call(path, token=None, timeout=90, method='GET', body=None):
    hdr = {'Authorization': 'MediaBrowser Token="%s"' % token} if token else {'Authorization': 'MediaBrowser'}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        hdr['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8', 'replace')
    return (json.loads(raw) if raw.strip() else {}), time.time() - t0


def items_of(d):
    """Jellyfin quirk: /Items returns {Items:[...]} but /Items/Latest and
    /UserViews-style endpoints may return a bare JSON array."""
    return d if isinstance(d, list) else (d or {}).get('Items', [])


def auth():
    creds = json.load(open(CREDS))
    d, _ = call('/Users/AuthenticateByName', method='POST',
                body={'Username': creds['uuid'], 'Pw': creds['password']})
    return d['AccessToken'], d['User']['Id']


def main():
    t_start = time.time()
    ok = bad = meta_ok = meta_bad = 0
    token = None
    if os.path.exists(STATE):
        try:
            token = json.load(open(STATE)).get('token')
        except (ValueError, OSError):
            token = None

    if not token:
        token, _ = auth()

    try:
        views, _ = call('/UserViews', token)
    except Exception:
        token, _ = auth()  # token likely stale
        views, _ = call('/UserViews', token)

    items = views.get('Items', [])[:VIEW_LIMIT]
    log('warm start: %d views, budget %ds' % (len(items), BUDGET_S))

    meta_ids = []
    for v in items:
        if time.time() - t_start > BUDGET_S:
            log('budget hit during rows')
            break
        vid, name = v.get('Id'), v.get('Name', '?')
        for path in ('/Items?ParentId=%s&Limit=60&Recursive=false' % vid,
                     '/Items/Latest?ParentId=%s&Limit=30' % vid):
            try:
                d, _ = call(path, token)
                ok += 1
                meta_ids.extend(i.get('Id') for i in items_of(d) if i.get('Id'))
            except Exception as exc:
                bad += 1
                log('  FAIL %s: %s (%s)' % (name, path.split('?')[0], type(exc).__name__))

    # global latest rail
    try:
        d, _ = call('/Items/Latest?Limit=40', token)
        ok += 1
        meta_ids.extend(i.get('Id') for i in items_of(d) if i.get('Id'))
    except Exception:
        bad += 1

    # dedupe, then warm item metadata in batches
    seen = set()
    uniq = [i for i in meta_ids if not (i in seen or seen.add(i))]
    batches = 0
    for x in range(0, len(uniq), META_BATCH):
        if batches >= MAX_META_BATCHES or time.time() - t_start > BUDGET_S:
            break
        batch = uniq[x:x + META_BATCH]
        try:
            call('/Items?Ids=' + ','.join(batch), token, timeout=120)
            meta_ok += 1
        except Exception:
            meta_bad += 1
        batches += 1

    dur = time.time() - t_start
    log('warm done in %.0fs: rows ok=%d fail=%d | meta batches ok=%d fail=%d | unique ids=%d'
        % (dur, ok, bad, meta_ok, meta_bad, len(uniq)))
    json.dump({'token': token,
               'last': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'views': len(items), 'ids': len(uniq), 'dur': round(dur, 1)},
              open(STATE, 'w'))

    if ok == 0:
        log('WARN: zero rows warmed this run')
        sys.exit(1)


if __name__ == '__main__':
    main()
