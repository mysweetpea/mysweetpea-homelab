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
import base64, json, os, socket, subprocess, sys, time, urllib.request, urllib.parse, urllib.error, datetime

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
SEASON_CAP = 12              # max SeasonSearch commands per run
BUDGET_PER_DAY = 800        # global engine search budget (indexer capacity ~15k+/day; real limiter is Sonarr health)
QUEUE_MAX = 40              # skip non-request lanes while Sonarr queue is this deep
STALL_MIN = 45              # commands stuck 'started' this long = executor jam (gate lanes + alert)
PARK_DAYS = 30
SAB_BASE = 'http://192.168.20.215:8282'   # decypharr SAB-compat API (LB)
SAB_ARRS = [                # category -> (arr FQDN used for auth, key secret)
    ('Sonarr', 'http://sonarr.private.svc.cluster.local:8989', 'SONARR_API_KEY'),
    ('Radarr', 'http://radarr.private.svc.cluster.local:7878', 'RADARR_API_KEY'),
]
SAB_GRACE_H = 2             # first-seen grace before purging an empty-folder history entry
SAB_CAP = 300               # max history purges per run
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

JF_ADMIN_UID = '3d75456ef9c5438480195d731242133d'

def jf_post(path, jk, body=None, timeout=90):
    req = urllib.request.Request(JF_BASE + path, data=json.dumps(body or {}).encode(),
        headers={'Authorization': 'MediaBrowser Token="%s"' % jk,
                 'Content-Type': 'application/json', 'User-Agent': UA}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None

def jf_raw(path, jk, timeout=45):
    req = urllib.request.Request(JF_BASE + path,
        headers={'Authorization': 'MediaBrowser Token="%s"' % jk, 'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

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

def warm_lane(st):
    """Post-import warm (2026-10-12): for items added in the last 48h, run the
    two one-time server-side costs so NO member pays them on first play:
      (a) PlaybackInfo -> forces+caches the metadata probe (measured 20.5s cold -> 0.2s warm);
      (b) per text-subtitle track -> SubtitleEncoder extraction cached under
          /config/data/subtitles/<xx>/<item>/<idx>.* (cold reads run 10-120s over usenet;
          the burn-in on first play otherwise waits on exactly this).
    Caches are server-side + shared by every user and platform. Newest-first,
    capped, idempotent via state; failures retry on later runs."""
    try:
        jk = secret('JELLYFIN_API_KEY')
    except Exception as e:
        log('warm lane: no JELLYFIN_API_KEY (%s)' % e)
        return
    acted = 0
    try:
        data = jf_get('/Items?SortBy=DateCreated&SortOrder=Descending&IncludeItemTypes=Movie,Episode'
                      '&Recursive=true&Limit=60&Fields=DateCreated&ImageTypeLimit=0'
                      '&userId=%s' % JF_ADMIN_UID, jk, timeout=60)
    except Exception as e:
        log('warm lane: item list failed (%s)' % e)
        return
    warm = st.setdefault('_warm', {})
    cutoff = (datetime.datetime.now(datetime.timezone.utc) -
              datetime.timedelta(hours=48)).isoformat()
    todo = [it for it in (data.get('Items') or [])
            if (it.get('DateCreated') or '') >= cutoff and it.get('Id') not in warm]
    for it in todo[:6]:
        iid = it['Id']
        try:
            info = jf_post('/Items/%s/PlaybackInfo?userId=%s' % (iid, JF_ADMIN_UID), jk,
                           {'UserId': JF_ADMIN_UID}, timeout=120)
            idxs = []
            for src_ in (info or {}).get('MediaSources', []):
                for st_ in (src_.get('MediaStreams') or []):
                    if st_.get('Type') == 'Subtitle' and st_.get('IsTextSubtitleStream') and st_.get('Index') is not None:
                        idxs.append(st_['Index'])
            for i2 in idxs[:8]:
                try:
                    jf_raw('/Videos/%s/%s/Subtitles/%d/Stream.srt' % (iid, iid, i2), jk, timeout=45)
                except Exception:
                    pass  # cold tail reads can exceed the window; first play retries naturally
            warm[iid] = now
            acted += 1
        except Exception as e:
            log('warm: %s failed (%s) - retry next run' % (str(it.get('Name'))[:40], str(e)[:60]))
    # prune old entries (7d)
    for k2 in [k2 for k2, v in warm.items() if isinstance(v, (int, float)) and v < now - 7 * 86400]:
        warm.pop(k2, None)
    if acted or todo:
        log('warm lane: acted=%d pending=%d' % (acted, max(0, len(todo) - acted)))

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

JUNK_QUEUE_CLASSES = ['No files found are eligible', 'Single episode file contains all episodes', 'release was matched to series by ID',
                      # usenet class: decypharr failed/cleaned the download but the arr never got the
                      # failure callback (arr busy/down) -> item stuck at completed/importPending while
                      # the scratch folder is gone -> import retries fail forever. Settle after grace.
                      'path does not exist or is not accessible']

def is_junk_queue_item(rec):
    """Sonarr queue item whose import can never self-resolve (junk release classes)."""
    if rec.get('status') != 'completed':
        return False
    msgs = ' | '.join(t for m in (rec.get('statusMessages') or []) for t in (m.get('messages') or []))
    return any(c in msgs for c in JUNK_QUEUE_CLASSES)

def is_foreign_file(f):
    return (f.get('customFormatScore') or 0) < 0

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
    assert is_junk_queue_item({'status': 'completed', 'statusMessages': [{'messages': ['No files found are eligible for import in /x']}]}) is True
    assert is_junk_queue_item({'status': 'completed', 'statusMessages': [{'messages': ['Single episode file contains all episodes in seasons']}]}) is True
    assert is_junk_queue_item({'status': 'completed', 'statusMessages': [{'messages': ['Import failed, path does not exist or is not accessible by Sonarr: /data/x']}]}) is True
    assert is_junk_queue_item({'status': 'downloading', 'statusMessages': [{'messages': ['No files found are eligible for import in /x']}]}) is False
    assert is_junk_queue_item({'status': 'completed', 'statusMessages': [{'messages': ['everything imported fine']}]}) is False
    assert is_foreign_file({'customFormatScore': -40}) is True
    assert is_foreign_file({'customFormatScore': 110}) is False
    assert is_foreign_file({}) is False
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

# --- post-import warm lane (JF-side; runs every tick; moves first-play costs
#     server-side so no member pays them - see warm_lane docstring) ---
try:
    warm_lane(state)
except Exception as e:
    log('warm lane error: %r' % e)

# --- import-list watchdog: surfaces Trakt token expiry (re-auth needed) instead of dying silently ---
def failing_import_lists(base, key):
    try:
        h = api('/api/v3/health', base=base, key=key, timeout=20)
        return [ (x.get('message') or '')[:90] for x in (h or [])
                 if 'import' in (x.get('message') or '').lower() or 'lists unavailable' in (x.get('message') or '').lower() ]
    except Exception:
        return []

fails = failing_import_lists(SONARR, KEY)
try:
    fails += failing_import_lists(RADARR, secret('RADARR_API_KEY'))
except Exception:
    pass
if fails and not DRY and now - alerts.get('ImportLists', 0) > 7 * 86400:
    gotify('MEDIA: import lists failing', 'Needs re-auth (Trakt tokens expire ~90d). Failing: ' + '; '.join(fails[:6]), 4)
    alerts['ImportLists'] = now
    log('alerted: import lists failing x%d' % len(fails))
elif not fails:
    alerts.pop('ImportLists', None)

# --- language audit: daily sweep for foreign-only files (negative CF score = French/German/Dutch/MULTi class) ---
# Dynamic + case-by-case: detects via scores, blocklists the old grabs, deletes foreign files and re-searches
# (max 2 series + 1 movie per run). No global language blocks — each title is handled on real data.
def language_audit():
    # rotating windows: 40 series + 40 movies per day -> full library swept ~every 12 days
    res = {'series': [], 'movies': []}
    try:
        all_series = api('/api/v3/series', timeout=180) or []
    except Exception:
        all_series = []
    ids = sorted(str(x.get('id')) for x in all_series)
    cur = int(state.get('_langaudit_cur', 0) or 0)
    window = ids[cur:cur + 40]
    if not DRY:
        state['_langaudit_cur'] = 0 if cur + 40 >= len(ids) else cur + 40
    bad = []
    for sid in window:
        try:
            efs = api('/api/v3/episodefile?seriesId=%s' % sid, timeout=60) or []
        except Exception:
            continue
        badf = [f for f in efs if is_foreign_file(f)]
        if badf:
            bad.append((sid, badf))
    if not bad:
        log('language audit: series window %d-%d clean' % (cur, min(cur + 40, len(ids))))
    for sid, files in sorted(bad, key=lambda kv: -len(kv[1]))[:2]:
        log('language audit: series %s -> %d foreign file(s), e.g. %s' % (sid, len(files), (files[0].get('sceneName') or '')[:55]))
        if DRY:
            res['series'].append((sid, 0)); continue
        try:
            hist = api('/api/v3/history/series?seriesId=%s&page=1&pageSize=500&sortKey=date&sortDirection=descending' % sid) or []
            names = [(f.get('sceneName') or '') for f in files if f.get('sceneName')]
            for h in (hist if isinstance(hist, list) else []):
                st = h.get('sourceTitle') or ''
                if h.get('eventType') == 'grabbed' and any(n[:40] in st for n in names):
                    try:
                        api('/api/v3/history/failed/%s' % h.get('id'), method='POST', body={})
                    except Exception:
                        pass
        except Exception as e:
            log('language audit: blocklist scan failed (%s)' % str(e)[:50])
        deleted = 0
        for f in files[:25]:
            try:
                api('/api/v3/episodefile/%s' % f.get('id'), method='DELETE'); deleted += 1
            except Exception:
                pass
        try:
            api('/api/v3/command', method='POST', body={'name': 'MissingEpisodeSearch', 'seriesId': sid})
        except Exception:
            pass
        res['series'].append((sid, deleted))
    # movies window
    try:
        all_movies = api('/api/v3/movie', timeout=180) or []
    except Exception:
        all_movies = []
    mids = sorted(str(x.get('id')) for x in all_movies)
    mcur = int(state.get('_langaudit_mcur', 0) or 0)
    mwindow = mids[mcur:mcur + 40]
    if not DRY:
        state['_langaudit_mcur'] = 0 if mcur + 40 >= len(mids) else mcur + 40
    for mid in mwindow:
        if res['movies']:
            break
        try:
            mfs = api('/api/v3/moviefile?movieId=%s' % mid, timeout=60) or []
        except Exception:
            continue
        for f in mfs:
            if not is_foreign_file(f):
                continue
            log('language audit: movie %s foreign (%s)' % (mid, (f.get('sceneName') or f.get('relativePath') or '')[:55]))
            if DRY:
                res['movies'].append(mid); break
            try:
                hist = api('/api/v3/history/movie?movieId=%s&page=1&pageSize=200&sortKey=date&sortDirection=descending' % mid) or []
                st0 = (f.get('sceneName') or '')
                for h in (hist if isinstance(hist, list) else []):
                    if h.get('eventType') == 'grabbed' and st0 and st0[:40] in (h.get('sourceTitle') or ''):
                        try:
                            api('/api/v3/history/failed/%s' % h.get('id'), method='POST', body={})
                        except Exception:
                            pass
            except Exception:
                pass
            try:
                api('/api/v3/moviefile/%s' % f.get('id'), method='DELETE')
                api('/api/v3/command', method='POST', body={'name': 'MoviesSearch', 'movieIds': [int(mid)]})
                res['movies'].append(mid)
            except Exception:
                pass
            break
    return res

# --- queue hygiene: settle import-blocked junk that never resolves on its own ---
# (multi-season-single-file uploads, empty/fake releases, ambiguous matches -> importPending forever).
# Removal uses removeFromClient=false (dodges decypharr's 500 on already-gone items) + blocklist=true
# (junk can't re-grab). 75-min grace so Sonarr + Cleanuparr get first shot; max 25 removals/run.
def queue_hygiene():
    try:
        q = api('/api/v3/queue?page=1&pageSize=200&includeUnknownSeriesItems=true')
    except Exception as e:
        log('queue hygiene: fetch failed (%s)' % str(e)[:60])
        return 0
    seen = state.setdefault('_qbad', {})
    removed = 0
    bad_now = set()
    for x in (q or {}).get('records') or []:
        if not is_junk_queue_item(x):
            continue
        qid = str(x.get('id'))
        bad_now.add(qid)
        first = seen.setdefault(qid, now)
        if now - first > 75 * 60 and removed < 25:
            if DRY:
                log('WOULD queue-hygiene remove: %s' % (x.get('title') or '')[:60])
                removed += 1
                continue
            try:
                api('/api/v3/queue/%s?removeFromClient=false&blocklist=true' % qid, method='DELETE', timeout=45)
                removed += 1
                log('queue hygiene: removed %s' % (x.get('title') or '')[:60])
            except Exception as e:
                if '404' in str(e):
                    seen.pop(qid, None)
                else:
                    log('queue hygiene: remove failed for %s (%s)' % (qid, str(e)[:50]))
            seen.pop(qid, None)
    for k in [k for k in seen if k not in bad_now]:
        seen.pop(k, None)
    return removed

def sab_get(mode, fqdn, key, extra=''):
    """Read/delete via decypharr's SAB-compat API. Auth = arr FQDN + that arr's own
    API key (decypharr validates the pair by calling the arr back)."""
    url = '%s/sabnzbd/api?mode=%s&output=json&ma_username=%s&ma_password=%s%s' % (
        SAB_BASE, mode, urllib.parse.quote(fqdn, safe=''), key, extra)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())

def decypharr_history_hygiene():
    """Purge decypharr SAB history tracking entries whose download folder holds no
    files left to import. This is the 'ghost' class (2026-10-11 incident): the arr
    never got the failure callback, so it re-tries these on every refresh forever.
    Entries must stay empty-folder for SAB_GRACE_H before removal; capped per run."""
    removed = 0
    seen = state.setdefault('_dhist', {})
    bad_now = set()
    for cat, fqdn, keyname in SAB_ARRS:
        try:
            slots = (sab_get('history', fqdn, secret(keyname), '&limit=100000').get('history') or {}).get('slots') or []
        except Exception as e:
            log('history hygiene: fetch failed for %s (%s)' % (cat, str(e)[:60]))
            continue
        cands = []
        for s in slots:
            if s.get('category') != cat or s.get('status') not in ('Completed', 'Failed'):
                continue
            st = (s.get('storage') or '').strip()
            nzo = s.get('nzo_id') or ''
            if st and nzo:
                cands.append((nzo, st))
        if not cands:
            continue
        payload = '\n'.join(nzo + '\t' + st for nzo, st in cands) + '\n'
        try:
            out = subprocess.run(
                ['kubectl', '-n', 'private', 'exec', '-i', 'deploy/decypharr', '--', 'sh', '-c',
                 'T=$(printf "\\t"); while IFS="$T" read -r id path; do c=$(find "$path" -maxdepth 1 -type f 2>/dev/null | wc -l); [ "$c" -eq 0 ] && echo EMPTY "$id"; done'],
                input=payload, capture_output=True, text=True, timeout=180).stdout
        except Exception as e:
            log('history hygiene: scan failed (%s)' % str(e)[:60])
            continue
        empty = set(l.split()[1] for l in out.splitlines() if l.startswith('EMPTY '))
        for nzo, st in cands:
            if nzo not in empty:
                seen.pop(nzo, None)
                continue
            bad_now.add(nzo)
            first = seen.setdefault(nzo, now)
            if now - first > SAB_GRACE_H * 3600 and removed < SAB_CAP:
                if DRY:
                    log('WOULD purge history entry (empty folder): %s' % st.split('/')[-1][:70])
                    removed += 1
                else:
                    try:
                        sab_get('history', fqdn, secret(keyname), '&name=delete&value=' + nzo)
                        removed += 1
                        log('history hygiene: purged ghost entry %s' % st.split('/')[-1][:70])
                    except Exception as e:
                        log('history hygiene: purge failed (%s)' % str(e)[:60])
                seen.pop(nzo, None)
    for k in [k for k in seen if k not in bad_now]:
        seen.pop(k, None)
    return removed

dh = decypharr_history_hygiene()
if dh:
    log('history hygiene: %d ghost tracking entry(ies) purged' % dh)

qh = queue_hygiene()
if qh:
    log('queue hygiene: %d junk item(s) settled' % qh)

# language audit at most once per 24h (dry runs always check, never write)
if DRY or now - state.get('_langaudit_ts', 0) > 86400:
    la = language_audit()
    if not DRY:
        state['_langaudit_ts'] = now
    log('language audit: series_fixed=%d movies_fixed=%d (dry=%s)' % (len(la['series']), len(la['movies']), DRY))

# --- budget (searches/day) ---
today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
budget = state.setdefault('_budget', {'date': today, 'n': 0})
if budget.get('date') != today:
    budget['date'] = today
    budget['n'] = 0
budget_left = max(0, BUDGET_PER_DAY - budget['n'])

# --- queue depth (wedge guard) ---
queue_full = False
q = None
try:
    q = api('/api/v3/queue?page=1&pageSize=1')
    queue_full = (q.get('totalRecords') or 0) > QUEUE_MAX
except Exception:
    # fail CLOSED (2026-10-11 incident): when the depth check can't be verified the
    # app is starved/sick - do not pile more searches onto it. Requests lane still
    # runs; other lanes wait one tick.
    queue_full = True

# --- executor stall (wedge guard v2, 2026-10-12 incident): 3 searches hung in
# 'started' for 2h; depth stayed under QUEUE_MAX the whole time so the guard
# above never tripped, and RssSync starved behind the jam for hours.
exec_stalled, stuck_n = False, 0
try:
    for c in api('/api/v3/command', timeout=60) or []:
        if c.get('status') != 'started':
            continue
        try:
            age = (datetime.datetime.now(datetime.timezone.utc) -
                   datetime.datetime.fromisoformat((c.get('queued') or '').replace('Z', '+00:00'))).total_seconds()
        except Exception:
            age = 0
        if age > STALL_MIN * 60:
            stuck_n += 1
    exec_stalled = stuck_n > 0
except Exception:
    exec_stalled = False  # the depth guard above already fails closed
if exec_stalled:
    log('executor STALLED: %d command(s) started >%dmin - gating non-request lanes' % (stuck_n, STALL_MIN))
    queue_full = True
    if not DRY and now - state.get('_stall_alert_ts', 0) > HEALTH_ALERT_DEDUPE_H * 3600:
        try:
            gotify('MEDIA: Sonarr executor stalled',
                   '%d search command(s) stuck >%dmin; non-request lanes paused until it clears. '
                   'If it persists, restart the sonarr pod.' % (stuck_n, STALL_MIN), 6)
            state['_stall_alert_ts'] = now
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
    {'name': 'requests', 'match': m_requests, 'cadence_min': 15,  'gap_h': 12,  'cap': 5, 'prio': 'high'},
    {'name': 'airing',   'match': m_airing,   'cadence_min': 120, 'gap_h': 24,  'cap': 6, 'prio': 'normal'},
    {'name': 'watched',  'match': m_watched,  'cadence_min': 240, 'gap_h': 48,  'cap': 4, 'prio': 'normal'},
    {'name': 'anime',    'match': m_anime,    'cadence_min': 480, 'gap_h': 48,  'cap': 5, 'prio': 'normal'},
    {'name': 'backfill', 'match': m_backfill, 'cadence_min': 240, 'gap_h': 72,  'cap': 8, 'prio': 'normal'},
    {'name': 'movies',   'match': None,       'cadence_min': 1440,'gap_h': 168, 'cap': 5, 'prio': 'normal'},
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
                    mega_cap = 6 if name == 'requests' else MEGA_SEASONS  # requested megashows fill deep, oldest seasons first
                    for se in megashow_seasons(s)[:mega_cap]:
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
stats['qhygiene'] = stats.get('qhygiene', 0) + qh
if not DRY and datetime.date.today().weekday() == 6 and now - stats.get('last_digest', 0) > 6 * 86400:
    parked = [v.get('title', '?') for k, v in state.items() if not k.startswith('_') and isinstance(v, dict) and v.get('parked_until', 0) > now]
    msg = """acted this period: %d
queue hygiene: %d junk items settled
parked (unavailable after 4 tries): %d
%s
budget used today: %d/%d""" % (
        stats.get('acted', 0), stats.get('qhygiene', 0), len(parked), ', '.join(parked[:10]) if parked else '-', budget['n'], BUDGET_PER_DAY)
    gotify('MEDIA weekly digest', msg, 3)
    stats['last_digest'] = now
    stats['acted'] = 0

tracked = len([k for k in state if not k.startswith('_')])
if not DRY:
    json.dump(state, open(STATE_FILE, 'w'))
log('done. acted_total=%d tracked=%d budget=%d/%d dry=%s lane=%s' % (acted_total, tracked, budget['n'], BUDGET_PER_DAY, DRY, ONLY_LANE or 'all'))
