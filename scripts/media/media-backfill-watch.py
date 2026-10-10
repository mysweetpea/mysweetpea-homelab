#!/usr/bin/env python3
# media-backfill-watch — daily safety net against stalled backfills (Sonarr).
# A monitored series with ~zero files long after being added gets moved to the
# permissive 'Any' profile (best-available up to 1080p) + a missing-episode search.
# Escalates to Gotify (phone, low priority) only when repeated attempts find nothing.
# Usage: python3 media-backfill-watch.py [--dry-run]
import base64, json, os, subprocess, sys, time, urllib.request, datetime

SONARR = 'http://192.168.20.219:8989'
STATE_FILE = '/root/.media-backfill-state.json'
TOKEN_FILE = '/root/.gotify-backup-token'
GOTIFY = 'http://gotify.private.svc.cluster.local/message'
LOG = '/var/log/media-backfill-watch.log'
TARGET_PROFILE = 1          # 'Any'
MIN_AGE_H = 36
STALE_DAYS = 7
LOW_FRAC = 0.05
RETRY_COOLDOWN_H = 72
ESCALATE_ATTEMPTS = 2
ESCALATE_DAYS = 14
MAX_ACTIONS = 8         # cap new backfills per run so Sonarr stays responsive
DRY = '--dry-run' in sys.argv

def log(msg):
    line = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg
    print(line)
    if not DRY:
        with open(LOG, 'a') as f:
            f.write(line + '\n')

def sonarr_key():
    out = subprocess.run(['kubectl', '-n', 'monitoring', 'get', 'secret', 'homepage-secrets',
                          '-o', 'jsonpath={.data.SONARR_API_KEY}'], capture_output=True, text=True)
    return base64.b64decode(out.stdout.strip()).decode().strip()

KEY = sonarr_key()

def api(path, method='GET', body=None):
    req = urllib.request.Request(SONARR + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'X-Api-Key': KEY, 'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

def gotify(title, message, priority=5):
    tok = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(GOTIFY, data=json.dumps({'title': title, 'message': message, 'priority': priority}).encode(),
                                 headers={'X-Gotify-Key': tok, 'Content-Type': 'application/json'}, method='POST')
    urllib.request.urlopen(req, timeout=30)

state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
now = time.time()
acted = 0
for s in api('/api/v3/series'):
    if not DRY and acted >= MAX_ACTIONS:
        break
    st = s.get('statistics') or {}
    total = st.get('episodeCount') or 0
    files = st.get('episodeFileCount') or 0
    if total == 0 or files > max(1, total * LOW_FRAC):
        continue
    sid = str(s.get('id'))
    try:
        age_h = (now - time.mktime(time.strptime(s['added'][:19], '%Y-%m-%dT%H:%M:%S'))) / 3600
    except Exception:
        continue
    if age_h < MIN_AGE_H:
        continue
    entry = state.get(sid, {'attempts': 0, 'last': 0})
    if entry['attempts'] >= ESCALATE_ATTEMPTS:
        if (now - entry['last']) > ESCALATE_DAYS * 86400 and not entry.get('escalated'):
            gotify('MEDIA: backfill stalled', "%s: still %d/%d episodes after %d attempts — likely unavailable on indexers." % (s.get('title'), files, total, entry['attempts']), 5)
            entry['escalated'] = True
            state[sid] = entry
            log('escalated: ' + str(s.get('title')))
        continue
    if now - entry['last'] < RETRY_COOLDOWN_H * 3600:
        continue
    hist = api('/api/v3/history/series?seriesId=%s' % s.get('id')) or []
    cutoff = now - STALE_DAYS * 86400
    grabbed_recent = False
    for ev in hist[-800:]:
        if ev.get('eventType') != 'grabbed':
            continue
        try:
            if time.mktime(time.strptime(ev['date'][:19], '%Y-%m-%dT%H:%M:%S')) > cutoff:
                grabbed_recent = True
                break
        except Exception:
            pass
    if grabbed_recent:
        state.pop(sid, None)
        continue
    log(('WOULD ACT' if DRY else 'acting') + ': %s (%d/%d files, profile %s)' % (s.get('title'), files, total, s.get('qualityProfileId')))
    if not DRY:
        if s.get('qualityProfileId') != TARGET_PROFILE:
            api('/api/v3/series/editor', 'PUT', {'seriesIds': [s.get('id')], 'qualityProfileId': TARGET_PROFILE})
        api('/api/v3/command', 'POST', {'name': 'MissingEpisodeSearch', 'seriesId': s.get('id')})
        state[sid] = {'attempts': entry['attempts'] + 1, 'last': now, 'title': s.get('title')}
        acted += 1
if not DRY:
    json.dump(state, open(STATE_FILE, 'w'))
log('done. acted=%d tracked=%d dry=%s' % (acted, len(state), DRY))
