#!/usr/bin/env python3
"""AIOStreams media-server pre-warmer (v3: dynamic users, per-user tokens).

Keeps the /jellyfin endpoints Moonfin + the apps actually hit hot, replaying
their EXACT query shapes (ground truth from real client traffic):
  - resolved-layout rows: synthetic playlists + taste genre rows (per user),
    plus the UID-LESS variant older builds send (owner only; token-scoped)
  - media-bar recentlyReleased windows + SortName count probes over the 7
    curated bar catalogs (moonbase-lite seed-defaults.py mediaBarLibraryIds)
  - Resume / NextUp / Upcoming + common home rows (BoxSet, DatePlayed, history)
  - genre grid page-1 shapes + genre-chip SortBy=Random previews (top genres)
  - batched per-item metadata (/Items?Ids=) for detail pages
  - NextUp/Resume detail pages (the cold ~2s open) and EVERY BoxSet's children
    in BOTH client shapes (Recursive=true&Fields=UserData + Fields=BasicSyncInfo)

v3: users are auto-discovered from the media server's /Users list, and each
user is warmed with THEIR OWN token. The server scopes UserData by token, not
by the uid in the path - warming a persona's paths with the owner's token
would bake the owner's progress into the persona's cache entries, so personas
log in with the PIN stored at provisioning (moonbase-lite data/pins.json).

nginx keys are lane- and token-normalized (conf.d/aios-api-cache.conf), so
one warm pass covers every device/platform of a user.

Root cron: 13 * * * * flock -n /tmp/aiostreams-warmer.lock python3 /root/aiostreams-warmer.py >> /var/log/aiostreams-warmer.log 2>&1
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = 'https://aiostreams.mysweetpea.cc/jellyfin'
MOUNT = ''  # per-run '/<uuid>/<encpw>' (set first in main) so edge-cache keys
            # match the apps: nginx canonicalizes mount+bare lanes to the uuid.
CREDS = '/root/.aiostreams-warmer-creds.json'
STATE = '/root/.aiostreams-warmer-state.json'
LOGFILE = '/var/log/aiostreams-warmer.log'
PINS = '/opt/moonbase-lite/data/pins.json'  # {slug: {name, pin}} written at provisioning

BUDGET_S = 600          # hard cap for the whole run
VIEW_LIMIT = 120         # never more than this many views
GENRE_PREWARM = 30        # genre grid page-1 prewarm: full clean-list coverage
META_BATCH = 40         # ids per /Items?Ids= call
MAX_META_BATCHES = 30   # hard cap on meta batches per run
MAX_USERS = 6           # users warmed per run (owner first)
CHILD_CAP = 200         # BoxSets hydrated per user (83 live at 2026-10-15)

# exact field/image strings Moonfin sends (from its source + access log)
F_LONG = ('DateCreated,Type,UserData,Overview,Genres,CommunityRating,CriticRating,'
          'OfficialRating,RunTimeTicks,ProductionYear,SeriesName,ParentIndexNumber,'
          'IndexNumber,Status,ImageTags,BackdropImageTags,ParentBackdropItemId,'
          'ParentBackdropImageTags,ParentThumbItemId,ParentThumbImageTag,SeriesId,'
          'SeriesPrimaryImageTag,ParentLogoItemId,ParentLogoImageTag,PrimaryImageTag,'
          'PrimaryImageAspectRatio')
F_BAR = ('Type,Genres,OfficialRating,CommunityRating,CriticRating,RunTimeTicks,'
         'ProductionYear,ImageTags,BackdropImageTags,Overview,ProviderIds')
IMG_LONG = 'Primary,Backdrop,Thumb,Banner'
IMG_BAR = 'Backdrop,Logo'

F_GRID = ('PrimaryImageAspectRatio,SortName,Type,IsFolder,UserData,CommunityRating,OfficialRating,'
          'RunTimeTicks,ProductionYear,ImageTags,BackdropImageTags,ParentBackdropItemId,'
          'ParentBackdropImageTags,ParentThumbItemId,ParentThumbImageTag,SeriesId,SeriesPrimaryImageTag,'
          'Album,AlbumId,AlbumArtist,Artists,Genres,Studios')

BAR_CATALOGS = [
    'a2b3d475a9842e952002bbf66ada706f',  # Trending Movies
    'a26d1a33e2d56da6265981f9d902a374',  # Trending Shows
    'a2170aa89ada673e50b5011031296a24',  # Popular Movies
    'a20e1577e1543cd8707bba16912f48a4',  # Popular Shows
    'a2b40fefb4986e8e38b519752d90a283',  # Netflix Movies
    'a281c33fb3bb152e71a5d1442b634258',  # Netflix Top 10 Movies (Global)
    'a2606deb6bee40c4b9566cf4edf03a96',  # Netflix Top 10 Shows (Global)
]

q = lambda s: urllib.parse.quote(s, safe='')


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    print(line, flush=True)
    try:
        with open(LOGFILE, 'a') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def call(path, token=None, timeout=90, method='GET', body=None):
    hdr = {'X-Aios-Warm': '1', 'Authorization': 'MediaBrowser Token="%s"' % token} if token else {'X-Aios-Warm': '1', 'Authorization': 'MediaBrowser'}
    data = json.dumps(body).encode() if body is not None else None
    if data:
        hdr['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + MOUNT + path, data=data, headers=hdr, method=method)
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


def _get_uid(token):
    d, _ = call('/Users/Me', token)
    return d.get('Id')


def discover_users(token, owner_uid):
    """Owner + every persona the media server knows (personas appear in
    /Users as soon as they are provisioned - no config to touch)."""
    users = [{'id': owner_uid, 'name': 'owner'}]
    try:
        d, _ = call('/Users', token, timeout=30)
        for u in items_of(d):
            u2 = u.get('Id')
            if u2 and u2 != owner_uid and not any(x['id'] == u2 for x in users):
                users.append({'id': u2, 'name': u.get('Name') or u2})
    except Exception as exc:
        log('  WARN: /Users discovery failed (%s)' % type(exc).__name__)
    return users[:MAX_USERS]


def login_persona(slug, pin):
    d, _ = call('/Users/AuthenticateByName', method='POST',
                body={'Username': slug, 'Pw': pin})
    return d['AccessToken']


def main():
    t_start = time.time()
    ok = bad = meta_ok = meta_bad = 0
    global MOUNT
    try:
        creds = json.load(open(CREDS))
        cuuid = creds.get('uuid') or creds.get('config_uuid') or \
                json.load(open('/opt/moonbase-lite/config.json')).get('config_uuid')
        encpw = open('/opt/aiostreams/.warm-encpw').read().strip()
        if cuuid and encpw:
            MOUNT = '/%s/%s' % (cuuid, encpw)
    except Exception as exc:
        log('  WARN: mount path unavailable (%s) - warming bare paths' % type(exc).__name__)
    log('  mount: %s' % (MOUNT[:40] + '...' if MOUNT else '(bare)'))

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

    # content rows 2026-10-16: home rows are synthetic playlists served by the
    # moonbase-lite shim (playlist engine); warm each slot per user below.
    try:
        slots = json.load(open('/opt/moonbase-lite/data/rows.json')).get('rows') or []
    except Exception:
        slots = []
    uid = _get_uid(token)
    users = discover_users(token, uid)
    try:
        pins = json.load(open(PINS))
        if not isinstance(pins, dict):
            pins = {}
    except Exception:
        pins = {}

    # owner first, then each persona logged in with the PIN stored at
    # provisioning (UserData is token-scoped; never warm a persona with
    # someone else's token).
    targets = [(uid, token, 'owner')]
    for u in users[1:]:
        slug = next((s for s, e in pins.items()
                     if isinstance(e, dict) and e.get('name') == u.get('name')), None)
        if not slug or not pins[slug].get('pin'):
            log('  skip %s (%s): no stored pin' % ((u.get('id') or '?')[:8], u.get('name')))
            continue
        try:
            targets.append((u['id'], login_persona(slug, pins[slug]['pin']), u.get('name')))
        except Exception as exc:
            log('  skip %s (%s): login failed (%s)' % ((u.get('id') or '?')[:8], u.get('name'), type(exc).__name__))
    log('warm start: %d slots, %d user(s) to warm of %d found, budget %ds'
        % (len(slots), len(targets), len(users), BUDGET_S))

    meta_ids = []

    def warm(utok, path, label):
        nonlocal ok, bad
        try:
            d, _ = call(path, utok)
            ok += 1
            for i in items_of(d):
                if i.get('Id'):
                    meta_ids.append(i.get('Id'))
        except Exception as exc:
            bad += 1
            log('  FAIL %s: %s (%s)' % (label, path.split('?')[0], type(exc).__name__))

    for uix, (uid, utok, uname) in enumerate(targets):
        if time.time() - t_start > BUDGET_S:
            log('budget hit before user %s' % uid[:8])
            break
        owner = (uix == 0)
        log('  user %s%s' % (uid[:8], ' (owner)' if owner else ' (%s)' % uname))
        t_user = time.time()

        # content rows: read the LIVE resolved layout (playlist engine + genre rows)
        # so the warmer follows exactly what clients render, per user.
        layout = []
        try:
            prof, _ = call('/Moonfin/Settings/Resolved/desktop', utok, timeout=60)
            layout = [h for h in (prof.get('homeSections') or [])
                      if isinstance(h, dict) and h.get('enabled')]
        except Exception as exc:
            log('  FAIL resolved layout: %s' % type(exc).__name__)
        if not layout:
            layout = [{'pluginSource': 'playlists', 'pluginAdditionalData': r.get('id'),
                       'pluginDisplayText': r.get('name')} for r in slots]
        for h in layout:
            if time.time() - t_start > BUDGET_S:
                break
            src = h.get('pluginSource')
            data = (h.get('pluginAdditionalData') or '').strip()
            if src == 'playlists' and data:
                warm(utok, '/Playlists/%s/Items?Limit=500' % data,
                     'row %s' % (h.get('pluginDisplayText') or data))
            elif src == 'genres' and data:
                warm(utok, '/Users/%s/Items?IncludeItemTypes=%s&ExcludeItemTypes=Episode'
                     '&SortBy=CommunityRating%%2CSortName&SortOrder=Descending&StartIndex=0&Limit=15'
                     '&Recursive=true&Fields=%s&GenreIds=%s&EnableImageTypes=%s&ImageTypeLimit=1'
                     % (uid, q('Movie,Series'), q(F_LONG), data, q('Primary,Backdrop,Thumb,Banner')),
                     'genre row %s' % (h.get('pluginDisplayText') or data))

        # genre-grid prewarm: page 1 of the top genres from the client's Genres row
        # (tapping a genre tile = exactly this paged query; 3h edge cache, refilled hourly)
        try:
            gr, _ = call('/Genres?UserId=%s&SortBy=CommunityRating%%2CSortName&SortOrder=Descending'
                         '&Limit=40&Recursive=true&Fields=ItemCounts%%2CPrimaryImageAspectRatio'
                         '&IncludeItemTypes=%s' % (uid, q('Movie,Series')), utok, timeout=60)
            gids = [i.get('Id') for i in items_of(gr) if i.get('Id')][:GENRE_PREWARM]
        except Exception as exc:
            gids = []
            log('  FAIL genre list: %s' % type(exc).__name__)
        for gid in gids:
            if time.time() - t_start > BUDGET_S:
                break
            warm(utok, '/Users/%s/Items?IncludeItemTypes=%s&ExcludeItemTypes=%s'
                 '&SortBy=SortName&SortOrder=Ascending&StartIndex=0&Limit=75&Recursive=true'
                 '&Fields=%s&GenreIds=%s&CollapseBoxSetItems=true&EnableImageTypes=%s&ImageTypeLimit=1'
                 % (uid, q('Movie,Series,BoxSet'), q('Playlist,Episode,Season,Folder'),
                    q(F_GRID), gid, q('Primary,Backdrop,Thumb,Banner')),
                 'genre grid %s' % gid[:8])
        # genre-chip thumbnails: the Genres row renders one SortBy=Random preview
        # per genre tile (exact client args; previously only warmed on visit)
        for gid in gids:
            if time.time() - t_start > BUDGET_S:
                break
            warm(utok, '/Users/%s/Items?IncludeItemTypes=%s&ExcludeItemTypes=Episode'
                 '&SortBy=Random&SortOrder=Ascending&Limit=10&Recursive=true'
                 '&Fields=%s&GenreIds=%s&EnableImageTypes=%s&ImageTypeLimit=1'
                 % (uid, q('Movie,Series'), q(F_LONG), gid, q('Primary,Backdrop,Thumb,Banner')),
                 'genre chip %s' % gid[:8])

        # media-bar pool: recentlyReleased windows + SortName count probes
        maxpremiere = q(datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
        for vid in BAR_CATALOGS:
            for itypes in ('Movie', 'Series'):
                warm(utok, '/Users/%s/Items?ParentId=%s&Recursive=true&IncludeItemTypes=%s&Limit=40&Fields=%s'
                     '&EnableImageTypes=%s&SortBy=PremiereDate&SortOrder=Descending&MaxPremiereDate=%s'
                     % (uid, vid, q(itypes), q(F_BAR), q(IMG_BAR), maxpremiere), 'bar %s %s' % (vid[:12], itypes))
            warm(utok, '/Users/%s/Items?ParentId=%s&Recursive=true&IncludeItemTypes=%s&SortBy=SortName'
                 '&SortOrder=Ascending&Limit=1&EnableTotalRecordCount=true'
                 % (uid, vid, q('Movie,Series')), 'bar-count %s' % vid[:12])

        # Resume / NextUp / Upcoming + common home rows (exact client shapes)
        cutoff = q((datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace('+00:00', 'Z'))
        extras = (
            '/UserItems/Resume?MediaTypes=Video&Limit=15&Fields=%s&EnableImageTypes=%s&ImageTypeLimit=1' % (q(F_LONG), q(IMG_LONG)),
            '/Shows/NextUp?UserId=%s&Limit=15&Fields=%s&EnableResumable=false&NextUpDateCutoff=%s&EnableImageTypes=%s&ImageTypeLimit=1' % (uid, q(F_LONG), cutoff, q(IMG_LONG)),
            '/Shows/Upcoming?userId=%s&Limit=24' % uid,
            '/Users/%s/Items?IncludeItemTypes=BoxSet&Limit=50&Recursive=true&Fields=%s' % (uid, q(F_LONG)),
            '/Users/%s/Items?IncludeItemTypes=%s&SortBy=DatePlayed&SortOrder=Descending&Limit=30&Recursive=true&Fields=%s' % (uid, q('Movie,Episode'), q(F_LONG)),
            '/Users/%s/Items?IncludeItemTypes=Episode&SortBy=DatePlayed&SortOrder=Descending&Limit=100&Recursive=true&Fields=%s&Filters=IsPlayed' % (uid, q('SeriesId,UserData')),
            '/Items/Latest?Limit=40',
            '/Users/%s/Items?IncludeItemTypes=Movie&SortBy=ProductionYear%%2CSortName&SortOrder=Descending&Limit=30&Recursive=true' % uid,
            '/Users/%s/Items?IncludeItemTypes=Series&SortBy=ProductionYear%%2CSortName&SortOrder=Descending&Limit=30&Recursive=true' % uid,
        )
        for path in extras:
            warm(utok, path, 'extra')

        # detail-page prewarm: the items the user most likely opens next
        # (NextUp + Resume) in the EXACT Moonfin detail shape = the cold ~2s call
        detail_fields = ('Chapters,MediaSources,MediaStreams,People,Overview,Genres,UserData,'
                         'RecursiveItemCount,ChildCount,ParentLogoItemId,ParentLogoImageTag,DateCreated')
        detail_ids = []
        for dp in ('/Shows/NextUp?UserId=%s&Limit=10&Fields=UserData' % uid,
                   '/UserItems/Resume?MediaTypes=Video&Limit=10'):
            try:
                dd, _ = call(dp, utok, timeout=60)
                detail_ids += [i.get('Id') for i in items_of(dd) if i.get('Id')]
            except Exception as exc:
                log('  FAIL detail-seed %s: %s' % (dp.split('?')[0], type(exc).__name__))
        seen_d = set()
        detail_ids = [i for i in detail_ids if not (i in seen_d or seen_d.add(i))][:20]
        det_ok = det_bad = 0
        for iid in detail_ids:
            if time.time() - t_start > BUDGET_S:
                break
            try:
                call('/Users/%s/Items/%s?Fields=%s' % (uid, iid, q(detail_fields)), utok, timeout=60)
                det_ok += 1
            except Exception:
                det_bad += 1
        log('  detail prewarm: ok=%d fail=%d' % (det_ok, det_bad))

        # collections-row children: EVERY BoxSet in BOTH client shapes
        # (collections row hydration + the light BasicSyncInfo re-sync pass)
        try:
            bx, _ = call('/Users/%s/Items?IncludeItemTypes=BoxSet&Limit=500&Recursive=true&Fields=%s'
                         % (uid, q(F_LONG)), utok, timeout=120)
            box_ids = [i.get('Id') for i in items_of(bx) if i.get('Id')][:CHILD_CAP]
        except Exception:
            box_ids = []
        ch_ok = ch_bad = 0
        for bid in box_ids:
            if time.time() - t_start > BUDGET_S:
                break
            for cargs in ('Recursive=true&Fields=UserData', 'Fields=BasicSyncInfo'):
                try:
                    call('/Users/%s/Items?ParentId=%s&%s' % (uid, bid, cargs), utok, timeout=60)
                    ch_ok += 1
                except Exception:
                    ch_bad += 1
        log('  children prewarm: ok=%d fail=%d (%d BoxSets x2 shapes)' % (ch_ok, ch_bad, len(box_ids)))
        log('  user %s done in %ds' % (uid[:8], time.time() - t_user))

    # dedupe, then warm item metadata in batches (owner token; /Items?Ids= is
    # uncached - this only primes the app's internal meta cache for the owner)
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
    log('warm done in %.0fs: users=%d rows ok=%d fail=%d | meta batches ok=%d fail=%d | unique ids=%d'
        % (dur, len(targets), ok, bad, meta_ok, meta_bad, len(uniq)))
    json.dump({'token': token,
               'last': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'slots': len(slots), 'users': len(targets), 'ids': len(uniq), 'dur': round(dur, 1)},
              open(STATE, 'w'))

    if ok == 0:
        log('WARN: zero rows warmed this run')
        sys.exit(1)


if __name__ == '__main__':
    main()
