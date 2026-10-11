#!/usr/bin/env python3
# media-backfill-watch v2 — continuous safety net against stalled backfills (Sonarr).
# Runs every 30 min from master cron (flock-guarded). For series with missing/aired
# episodes and no grabs in STALE_DAYS: move to 'Any' profile + MissingEpisodeSearch,
# plus SeasonSearch for fully-empty seasons (season packs are often the only source
# for old catalogs like Chowder). Seerr-requested shows jump the queue. Also health-
# checks the whole media pipeline every run and alerts to Gotify after 2 consecutive
# failures. Escalates only when repeated attempts still find nothing (likely
# unavailable on indexers).
# Usage: python3 media-backfill-watch.py [--dry-run]
import base64, json, os, socket, subprocess, sys, time, urllib.request, urllib.error, datetime

SONARR = 'http://192.168.20.219:8989'
RADARR = 'http://192.168.20.218:7878'
# ponytail: ClusterIP hardcodes for seerr/jellyfin (no LB service); refresh if svc recreated
SEERR_BASES = ['http://10.43.186.155:5055', 'https://request.mysweetpea.cc']
JELLYFIN_HEALTH = 'http://10.43.244.254:8096/health'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
STATE_FILE = '/root/.media-backfill-state.json'
TOKEN_FILE = '/root/.gotify-backup-token'
GOTIFY = 'http://gotify.private.svc.cluster.local/message'
LOG = '/var/log/media-backfill-watch.log'
TARGET_PROFILE = 1          # 'Any'
MIN_AGE_H = 36
STALE_DAYS = 2              # v2: catch stalls within ~2 days (was 7)
LOW_FRAC = 0.30            # catch partial stalls too (<30% of episodes have files)
RETRY_COOLDOWN_H = 24      # v2: retry daily (was 72h)
ESCALATE_ATTEMPTS = 2
ESCALATE_DAYS = 14
MAX_ACTIONS = 6            # cap actions per run; runs every 30 min so throughput is fine
SEASON_CAP = 8             # max SeasonSearch commands per run (empty seasons)
HEALTH_ALERT_DEDUPE_H = 6
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

KEY = secret('SONARR_API_KEY')

def api(path, method='GET', body=None, base=SONARR, key=None, timeout=120, ua=False):
    h = {'X-Api-Key': key or KEY, 'Content-Type': 'application/json'}
    if ua:
        h['User-Agent'] = UA
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

def http_alive(url, key=None, timeout=6):
    """Any response (even 401/403) = process is serving; 5xx or connection error = down."""
    try:
        h = {'User-Agent': UA}
        if key:
            h['X-Api-Key'] = key
        urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
        return True
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False

def tcp(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def gotify(title, message, priority=5):
    tok = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(GOTIFY, data=json.dumps({'title': title, 'message': message, 'priority': priority}).encode(),
                                 headers={'X-Gotify-Key': tok, 'Content-Type': 'application/json'}, method='POST')
    urllib.request.urlopen(req, timeout=30)

def seerr_requested_tmdbids():
    """Best-effort: tmdbIds of TV requests, so user-requested shows jump the queue."""
    try:
        se = secret('SEERR_API_KEY')
    except Exception:
        return set()
    for base in SEERR_BASES:
        try:
            reqs = api('/api/v1/request?take=200', base=base, key=se, timeout=20, ua=True)
            out = set()
            for r in (reqs.get('results') or []):
                m = r.get('media') or {}
                if m.get('tmdbId') and m.get('mediaType') == 'tv':
                    out.add(m['tmdbId'])
            return out
        except Exception as e:
            log('seerr base %s failed: %s' % (base, e))
    return set()

def poke_availability_sync(se):
    """Keep Seerr's per-episode availability counts fresh (built-in job runs only 1x/day)."""
    try:
        api('/api/v1/settings/jobs/availability-sync/run', 'POST', base=SEERR_BASES[0], key=se, timeout=15)
    except Exception as e:
        log('availability-sync poke failed: %s' % e)

state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
now = time.time()
health = state.setdefault('_health', {})

# --- pipeline health: alert (deduped) after 2 consecutive failures; skip run if Sonarr down ---
checks = [
    ('Sonarr',    lambda: http_alive(SONARR + '/ping'), 6, True),
    ('Radarr',    lambda: http_alive(RADARR + '/ping'), 5, False),
    ('Jellyfin',  lambda: http_alive(JELLYFIN_HEALTH), 6, False),
    ('Seerr',     lambda: http_alive(SEERR_BASES[0] + '/api/v1/status'), 5, False),
    ('Decypharr', lambda: tcp('192.168.20.215', 8282), 6, False),
    ('Bazarr',    lambda: tcp('192.168.20.223', 6767), 5, False),
]
alerts = health.setdefault('alerts', {})
sonarr_down = False
for name, fn, prio, gating in checks:
    try:
        ok = fn()
    except Exception:
        ok = False
    ck = name.lower() + '_fails'
    health[ck] = 0 if ok else health.get(ck, 0) + 1
    if health[ck] >= 2 and now - alerts.get(name, 0) > HEALTH_ALERT_DEDUPE_H * 3600 and not DRY:
        gotify('MEDIA: %s unresponsive' % name, '%s failed %d consecutive pipeline checks - things will stall until it recovers. Check the pod.' % (name, health[ck]), prio)
        alerts[name] = now
        log('alerted: %s unresponsive x%d' % (name, health[ck]))
    if gating and not ok:
        sonarr_down = True
if sonarr_down:
    log('sonarr /ping FAILED (x%d) - skipping run' % health.get('sonarr_fails', 0))
    if not DRY:
        json.dump(state, open(STATE_FILE, 'w'))
    sys.exit(0)

# --- gather candidates, requested-first, oldest-first ---
try:
    se_key = secret('SEERR_API_KEY')
except Exception:
    se_key = None
if se_key:
    poke_availability_sync(se_key)
req_ids = seerr_requested_tmdbids()
cands = []
for s in api('/api/v3/series'):
    st = s.get('statistics') or {}
    total = st.get('episodeCount') or 0
    files = st.get('episodeFileCount') or 0
    if total == 0 or files > max(1, total * LOW_FRAC):
        continue
    try:
        age_h = (now - time.mktime(time.strptime(s['added'][:19], '%Y-%m-%dT%H:%M:%S'))) / 3600
    except Exception:
        continue
    if age_h < MIN_AGE_H:
        continue
    cands.append(s)
cands.sort(key=lambda s: (0 if s.get('tmdbId') in req_ids else 1, s.get('added') or ''))

acted = 0
season_cmds = 0
for s in cands:
    if not DRY and acted >= MAX_ACTIONS:
        break
    st = s.get('statistics') or {}
    total = st.get('episodeCount') or 0
    files = st.get('episodeFileCount') or 0
    sid = str(s.get('id'))
    entry = state.get(sid, {'attempts': 0, 'last': 0})
    if entry['attempts'] >= ESCALATE_ATTEMPTS:
        if (now - entry['last']) > ESCALATE_DAYS * 86400 and not entry.get('escalated'):
            gotify('MEDIA: backfill stalled', "%s: still %d/%d episodes after %d attempts - likely unavailable on indexers." % (s.get('title'), files, total, entry['attempts']), 5)
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
    req = ' [requested]' if s.get('tmdbId') in req_ids else ''
    log(('WOULD ACT' if DRY else 'acting') + ': %s (%d/%d files, profile %s)%s' % (s.get('title'), files, total, s.get('qualityProfileId'), req))
    if not DRY:
        if s.get('qualityProfileId') != TARGET_PROFILE:
            api('/api/v3/series/editor', 'PUT', {'seriesIds': [s.get('id')], 'qualityProfileId': TARGET_PROFILE})
        api('/api/v3/command', 'POST', {'name': 'MissingEpisodeSearch', 'seriesId': s.get('id')})
        # empty seasons -> season-pack search (capped; packs are often the only source for old catalogs)
        for se in sorted(s.get('seasons') or [], key=lambda x: x.get('seasonNumber') or 0):
            if season_cmds >= SEASON_CAP:
                break
            sst = se.get('statistics') or {}
            if (sst.get('episodeCount') or 0) > 0 and (sst.get('episodeFileCount') or 0) == 0:
                api('/api/v3/command', 'POST', {'name': 'SeasonSearch', 'seriesId': s.get('id'), 'seasonNumber': se.get('seasonNumber')})
                season_cmds += 1
        state[sid] = {'attempts': entry['attempts'] + 1, 'last': now, 'title': s.get('title')}
        acted += 1
tracked = len([k for k in state if not k.startswith('_')])
if not DRY:
    json.dump(state, open(STATE_FILE, 'w'))
log('done. acted=%d season_cmds=%d tracked=%d dry=%s' % (acted, season_cmds, tracked, DRY))
