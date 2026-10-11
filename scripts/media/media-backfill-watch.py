#!/usr/bin/env python3
# media-backfill-watch v3 — lane-based fill engine (Sonarr + Radarr).
#
# One engine, six policy lanes, shared state/health/logging. Runs every 15 min
# from master cron (flock-guarded); each lane self-schedules via state timestamps:
#   requests : open Seerr requests not fully available  -> search HIGH priority
#   airing   : episode aired <=14d missing (RSS misses) -> search
#   watched  : shows people actually play (Jellyfin)    -> search
#   anime    : TVDB-anime series, batch/season-pack first
#   backfill : catalog fill; megashows (500+ eps) scoped to latest 2 aired seasons
#   movies   : Radarr monitored-no-file >14d, no grabs 7d
# Per-series backoff: gap doubles each fruitless attempt (cap 7d); any file-count
# progress resets it. 4 fruitless attempts -> parked 30d + weekly digest ping.
# Health checks (6 components) + Seerr availability poke stay as in v2.
# Usage: python3 media-backfill-watch.py [--dry-run] [--lane NAME] [--selftest]
import base64, json, os, socket, subprocess, sys, time, urllib.request, urllib.error, datetime

SONARR = 'http://192.168.20.219:8989'
RADARR = 'http://192.168.20.218:7878'
JF_BASE = 'http://10.43.244.254:8096'          # ponytail: ClusterIP hardcode; refresh if svc recreated
SEERR_BASES = ['http://10.43.186.155:5055', 'https://request.mysweetpea.cc']
JELLYFIN_HEALTH = JF_BASE + '/health'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
STATE_FILE = '/root/.media-backfill-state.json'
TOKEN_FILE = '/root/.gotify-backup-token'
GOTIFY = 'http://gotify.private.svc.cluster.local/message'
LOG = '/var/log/media-backfill-watch.log'
TARGET_PROFILE = 1          # 'Any'
MIN_AGE_H = 36
STALE_DAYS = 2              # a grab within this window means the pipeline is already working
LOW_FRAC = 0.30
MEGA_EPS = 500              # megashow threshold: scope to recent seasons only
MEGA_SEASONS = 2
SEASON_CAP = 8              # max SeasonSearch commands per run
BUDGET_PER_DAY = 400        # global engine search budget
QUEUE_MAX = 40              # skip non-request lanes while Sonarr queue is this deep
PARK_DAYS = 30
HEALTH_ALERT_DEDUPE_H = 6
DRY = '--dry-run' in sys.argv
ONLY_LANE = None
for i, a in enumerate(sys.argv):
    if a == '--lane' and i + 1 < len(sys.argv):
        ONLY_LANE = sys.argv[i + 1]

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

def jf_get(path, jk, timeout=25):
    req = urllib.request.Request(JF_BASE + path, headers={'Authorization': 'MediaBrowser Token="%s"' % jk, 'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def http_alive(url, key=None, timeout=6):
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

# ---------- pure helpers (selftested) ----------
def gap_hours(attempts, base_h):
    """Per-series re-search gap: doubles per fruitless attempt (12h->24h->48h),
    then the full 7-day cap once the ladder stalls."""
    if attempts >= 3:
        return 168
    return min(base_h * (2 ** attempts), 168)

def missing_count(s):
    st = s.get('statistics') or {}
    return max(0, (st.get('episodeCount') or 0) - (st.get('episodeFileCount') or 0))

def is_anime(s):
    return any('anime' in (g or '').lower() for g in (s.get('genres') or []))

def megashow_seasons(s):
    """Aired seasons, newest first."""
    se = [x for x in (s.get('seasons') or []) if (x.get('statistics') or {}).get('episodeCount', 0) > 0 and (x.get('seasonNumber') or 0) > 0]
    return sorted(se, key=lambda x: x.get('seasonNumber') or 0, reverse=True)

def megashow_recent_missing(s):
    """Missing aired eps within the latest MEGA_SEASONS aired seasons only."""
    n = 0
    for se in megashow_seasons(s)[:MEGA_SEASONS]:
        st = se.get('statistics') or {}
        n += max(0, (st.get('episodeCount') or 0) - (st.get('episodeFileCount') or 0))
    return n

def selftest():
    assert gap_hours(0, 12) == 12 and gap_hours(1, 12) == 24 and gap_hours(2, 12) == 48
    assert gap_hours(3, 12) == 168 and gap_hours(9, 12) == 168 and gap_hours(0, 168) == 168
    assert missing_count({'statistics': {'episodeCount': 10, 'episodeFileCount': 4}}) == 6
    assert missing_count({}) == 0
    assert is_anime({'genres': ['Animation', 'Anime']}) is True
    assert is_anime({'genres': ['Drama']}) is False
    mg = {'statistics': {'episodeCount': 900, 'episodeFileCount': 100}, 'seasons': [
        {'seasonNumber': 1, 'statistics': {'episodeCount': 22, 'episodeFileCount': 0}},
        {'seasonNumber': 2, 'statistics': {'episodeCount': 0, 'episodeFileCount': 0}},
        {'seasonNumber': 35, 'statistics': {'episodeCount': 20, 'episodeFileCount': 18}},
        {'seasonNumber': 34, 'statistics': {'episodeCount': 22, 'episodeFileCount': 4}},
    ]}
    assert megashow_recent_missing(mg) == (20 - 18) + (22 - 4) == 20
    print('SELFTEST OK')
    sys.exit(0)

if '--selftest' in sys.argv:
    selftest()

def seerr_open_tv_tmdbids():
    """tmdbIds of TV requests that are NOT fully available (media status 2/3/4)."""
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
                if m.get('tmdbId') and m.get('mediaType') == 'tv' and m.get('status') in (2, 3, 4):
                    out.add(m['tmdbId'])
            return out
        except Exception as e:
            log('seerr base %s failed: %s' % (base, e))
    return set()

def poke_availability_sync(se):
    try:
        api('/api/v1/settings/jobs/availability-sync/run', 'POST', base=SEERR_BASES[0], key=se, timeout=15)
    except Exception as e:
        log('availability-sync poke failed: %s' % e)

def jf_watched_plays():
    """{tvdbId: total plays} across recently-active Jellyfin users (watched lane)."""
    try:
        jk = secret('JELLYFIN_API_KEY')
        users = jf_get('/Users', jk)
    except Exception as e:
        log('watched lane: JF unreachable: %s' % e)
        return {}
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=90)).strftime('%Y-%m-%d')
    active = [u for u in users if (u.get('LastActivityDate') or '')[:10] >= cutoff][:8]
    plays = {}
    for u in active:
        try:
            d = jf_get('/Items?userId=%s&SortBy=PlayCount&SortOrder=Descending&Recursive=true&Limit=50&IncludeItemTypes=Series&fields=ProviderIds' % u.get('Id'), jk)
            for it in (d.get('Items') or []):
                ud = it.get('UserData') or {}
                tvdb = (it.get('ProviderIds') or {}).get('Tvdb')
                if tvdb and (ud.get('PlayCount') or 0) > 0:
                    plays[int(tvdb)] = plays.get(int(tvdb), 0) + ud['PlayCount']
        except Exception as e:
            log('watched lane: user %s fetch failed: %s' % (u.get('Name'), e))
    return plays

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

try:
    se_key = secret('SEERR_API_KEY')
except Exception:
    se_key = None
if se_key:
    poke_availability_sync(se_key)

# --- budget (searches/day) ---
today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
budget = state.setdefault('_budget', {'date': today, 'n': 0})
if budget.get('date') != today:
    budget['date'] = today
    budget['n'] = 0
budget_left = max(0, BUDGET_PER_DAY - budget['n'])

# --- queue depth (wedge guard) ---
queue_full = False
try:
    q = api('/api/v3/queue?page=1&pageSize=1')
    queue_full = (q.get('totalRecords') or 0) > QUEUE_MAX
except Exception:
    pass

series = api('/api/v3/series')
by_tvdb = {}
for s in series:
    if s.get('tvdbId'):
        by_tvdb[int(s['tvdbId'])] = s
req_ids = seerr_open_tv_tmdbids()
watched = {}  # lazy-loaded per lane below

# --- lane definitions (priority order) ---
def m_requests(s, ctx):
    return s.get('tvdbId') in ctx['req_ids']

def m_airing(s, ctx):
    la = (s.get('lastAired') or '')[:10]
    return bool(la) and la >= ctx['d14'] and missing_count(s) > 0

def m_watched(s, ctx):
    return ctx['watched'].get(s.get('tvdbId')) is not None and missing_count(s) > 0

def m_anime(s, ctx):
    return is_anime(s) and missing_count(s) > 0

def m_backfill(s, ctx):
    st = s.get('statistics') or {}
    tot, fil = st.get('episodeCount') or 0, st.get('episodeFileCount') or 0
    if tot == 0 or fil > max(1, tot * LOW_FRAC):
        return False
    if tot >= MEGA_EPS:
        return megashow_recent_missing(s) > 0
    return missing_count(s) > 0

lanes = [
    {'name': 'requests', 'match': m_requests, 'cadence_min': 15,  'gap_h': 12,  'cap': 3, 'prio': 'high'},
    {'name': 'airing',   'match': m_airing,   'cadence_min': 120, 'gap_h': 24,  'cap': 4, 'prio': 'normal'},
    {'name': 'watched',  'match': m_watched,  'cadence_min': 240, 'gap_h': 48,  'cap': 3, 'prio': 'normal'},
    {'name': 'anime',    'match': m_anime,    'cadence_min': 480, 'gap_h': 48,  'cap': 4, 'prio': 'normal'},
    {'name': 'backfill', 'match': m_backfill, 'cadence_min': 240, 'gap_h': 72,  'cap': 5, 'prio': 'normal'},
    {'name': 'movies',   'match': None,       'cadence_min': 1440,'gap_h': 168, 'cap': 3, 'prio': 'normal'},
]
lane_state = state.setdefault('_lanes', {})
acted_total = 0

for lane in lanes:
    name = lane['name']
    if ONLY_LANE and name != ONLY_LANE:
        continue
    if now - lane_state.get(name, 0) < lane['cadence_min'] * 60 and not DRY:
        continue
    if budget_left <= 0 and name != 'requests':
        log('lane %s: daily budget exhausted, skipping' % name)
        continue
    if queue_full and name != 'requests':
        log('lane %s: sonarr queue deep (%d), skipping' % (name, q.get('totalRecords') if q else -1))
        continue
    lane_state[name] = now

    # ---- movies lane ----
    if name == 'movies':
        try:
            ra_key = secret('RADARR_API_KEY')
            movies = api('/api/v3/movie', base=RADARR, key=ra_key)
        except Exception as e:
            log('movies lane: radarr unreachable: %s' % e)
            continue
        cut14 = now - 14 * 86400
        cands = []
        for m in movies:
            if not m.get('monitored') or m.get('hasFile') or not m.get('isAvailable'):
                continue
            try:
                added = time.mktime(time.strptime((m.get('added') or '')[:19], '%Y-%m-%dT%H:%M:%S'))
            except Exception:
                continue
            if added > cut14:
                continue
            cands.append(m)
        acted = 0
        for m in cands:
            if acted >= lane['cap'] or budget_left <= 0:
                break
            sid = 'mv:%s' % m.get('id')
            entry = state.get(sid, {'attempts': 0, 'last': 0})
            if entry.get('parked_until', 0) > now:
                continue
            if now - entry['last'] < gap_hours(entry['attempts'], lane['gap_h']) * 3600:
                continue
            try:
                hist = api('/api/v3/history/movie?movieId=%s' % m.get('id'), base=RADARR, key=ra_key) or []
            except Exception:
                hist = []
            grab_cut = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=STALE_DAYS)).strftime('%Y-%m-%dT')
            if any((ev.get('eventType') == 'grabbed' and (ev.get('date') or '') >= grab_cut) for ev in hist[-200:]):
                state.pop(sid, None)
                continue
            log(('WOULD ACT' if DRY else 'acting') + ': [movies] %s' % m.get('title'))
            if not DRY:
                try:
                    api('/api/v3/command', 'POST', {'name': 'MoviesSearch', 'movieIds': [m.get('id')]}, base=RADARR, key=ra_key)
                except Exception as e:
                    log('movies cmd failed for %s: %s' % (m.get('title'), e))
                    continue
                state[sid] = {'attempts': entry['attempts'] + 1, 'last': now, 'title': m.get('title')}
                budget['n'] += 1
                budget_left -= 1
                acted += 1
        continue

    # ---- TV lanes ----
    ctx = {'req_ids': req_ids, 'd14': (datetime.date.today() - datetime.timedelta(days=14)).isoformat(), 'watched': {}}
    if name == 'watched' and not watched:
        watched = jf_watched_plays()
    ctx['watched'] = watched
    cands = [s for s in series if lane['match'](s, ctx)]
    # ordering: requests/backfill oldest first; airing newest first; watched most-played; anime newest season
    if name == 'watched':
        cands.sort(key=lambda s: -watched.get(s.get('tvdbId'), 0))
    elif name == 'airing':
        cands.sort(key=lambda s: s.get('lastAired') or '', reverse=True)
    elif name == 'anime':
        cands.sort(key=lambda s: (megashow_seasons(s)[0].get('seasonNumber', 0) if megashow_seasons(s) else 0), reverse=True)
    else:
        cands.sort(key=lambda s: s.get('added') or '')
    acted, season_cmds = 0, 0
    for s in cands:
        if acted >= lane['cap'] or budget_left <= 0:
            break
        sid = str(s.get('id'))
        entry = state.get(sid, {'attempts': 0, 'last': 0, 'files_at_last': -1})
        # progress detection resets backoff
        files = (s.get('statistics') or {}).get('episodeFileCount') or 0
        if entry.get('files_at_last', -1) >= 0 and files > entry['files_at_last']:
            entry['attempts'] = 0
            entry.pop('parked_until', None)
        if entry.get('parked_until', 0) > now:
            continue
        if entry.get('parked_until') and entry['parked_until'] <= now:
            entry['attempts'] = 0   # unpark after 30d, fresh ladder
            entry.pop('parked_until', None)
        if now - entry['last'] < gap_hours(entry['attempts'], lane['gap_h']) * 3600:
            continue
        try:
            hist = api('/api/v3/history/series?seriesId=%s' % s.get('id')) or []
        except Exception:
            continue
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
        log(('WOULD ACT' if DRY else 'acting') + ': [%s] %s (%d missing, profile %s)' % (name, s.get('title'), missing_count(s), s.get('qualityProfileId')))
        if not DRY:
            if s.get('qualityProfileId') != TARGET_PROFILE:
                try:
                    api('/api/v3/series/editor', 'PUT', {'seriesIds': [s.get('id')], 'qualityProfileId': TARGET_PROFILE})
                except Exception:
                    pass
            cmd_ok = True
            try:
                if name == 'anime':
                    # batch-first: season packs for up to 2 recent seasons with missing
                    se_missing = [x for x in megashow_seasons(s) if (x.get('statistics') or {}).get('episodeCount', 0) > (x.get('statistics') or {}).get('episodeFileCount', 0)][:2]
                    for se in se_missing:
                        if season_cmds >= SEASON_CAP:
                            break
                        api('/api/v3/command', 'POST', {'name': 'SeasonSearch', 'seriesId': s.get('id'), 'seasonNumber': se.get('seasonNumber')})
                        season_cmds += 1
                    if (s.get('statistics') or {}).get('episodeCount', 0) < MEGA_EPS:
                        api('/api/v3/command', 'POST', {'name': 'MissingEpisodeSearch', 'seriesId': s.get('id')})
                elif (s.get('statistics') or {}).get('episodeCount', 0) >= MEGA_EPS:
                    for se in megashow_seasons(s)[:MEGA_SEASONS]:
                        st = se.get('statistics') or {}
                        if (st.get('episodeCount') or 0) > (st.get('episodeFileCount') or 0) and season_cmds < SEASON_CAP:
                            api('/api/v3/command', 'POST', {'name': 'SeasonSearch', 'seriesId': s.get('id'), 'seasonNumber': se.get('seasonNumber')})
                            season_cmds += 1
                else:
                    body = {'name': 'MissingEpisodeSearch', 'seriesId': s.get('id')}
                    if lane['prio'] != 'normal':
                        body['priority'] = lane['prio']
                    api('/api/v3/command', 'POST', body)
                    for se in megashow_seasons(s):
                        if season_cmds >= SEASON_CAP:
                            break
                        st = se.get('statistics') or {}
                        if (st.get('episodeCount') or 0) > 0 and (st.get('episodeFileCount') or 0) == 0:
                            api('/api/v3/command', 'POST', {'name': 'SeasonSearch', 'seriesId': s.get('id'), 'seasonNumber': se.get('seasonNumber')})
                            season_cmds += 1
            except Exception as e:
                cmd_ok = False
                log('cmd failed for %s: %s' % (s.get('title'), e))
            if cmd_ok:
                entry.update({'attempts': entry['attempts'] + 1, 'last': now, 'files_at_last': files, 'lane': name, 'title': s.get('title')})
                if entry['attempts'] >= 4:
                    entry['parked_until'] = now + PARK_DAYS * 86400
                    log('parked (4 fruitless attempts): ' + str(s.get('title')))
                state[sid] = entry
                budget['n'] += 1
                budget_left -= 1
                acted += 1
    if acted:
        log('lane %s: acted=%d season_cmds=%d' % (name, acted, season_cmds))
    acted_total += acted

# --- weekly digest (Sundays) ---
stats = state.setdefault('_stats', {'last_digest': 0, 'acted': 0})
stats['acted'] = stats.get('acted', 0) + acted_total
if not DRY and datetime.date.today().weekday() == 6 and now - stats.get('last_digest', 0) > 6 * 86400:
    parked = [v.get('title', '?') for k, v in state.items() if not k.startswith('_') and isinstance(v, dict) and v.get('parked_until', 0) > now]
    msg = 'acted this period: %d\nparked (unavailable after 4 tries): %d\n%s\nbudget used today: %d/%d' % (
        stats.get('acted', 0), len(parked), ', '.join(parked[:10]) if parked else '-', budget['n'], BUDGET_PER_DAY)
    gotify('MEDIA weekly digest', msg, 3)
    stats['last_digest'] = now
    stats['acted'] = 0

tracked = len([k for k in state if not k.startswith('_')])
if not DRY:
    json.dump(state, open(STATE_FILE, 'w'))
log('done. acted_total=%d tracked=%d budget=%d/%d dry=%s lane=%s' % (acted_total, tracked, budget['n'], BUDGET_PER_DAY, DRY, ONLY_LANE or 'all'))
