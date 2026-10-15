#!/usr/bin/env python3
"""AIOStreams media-server pre-warmer.

Keeps the /jellyfin endpoints Moonfin + the apps actually hit hot, replaying
their EXACT query shapes (ground truth from real client traffic):
  - per-view Items/Latest rows (Limit 15 + 60, full Fields list, user-scoped)
  - media-bar recentlyReleased windows + SortName count probes over the 7
    curated bar catalogs (moonbase-lite seed-defaults.py mediaBarLibraryIds)
  - Resume / NextUp / Upcoming + common home rows (BoxSet, DatePlayed, history)
  - batched per-item metadata (/Items?Ids=) for detail pages
Catalog pages live 6h; this runs every 2h so nothing goes cold on a client.

Root cron: 13 */2 * * * flock -n /tmp/aiostreams-warmer.lock python3 /root/aiostreams-warmer.py >> /var/log/aiostreams-warmer.log 2>&1
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = 'https://aiostreams.mysweetpea.cc/jellyfin'
CREDS = '/root/.aiostreams-warmer-creds.json'
STATE = '/root/.aiostreams-warmer-state.json'
LOGFILE = '/var/log/aiostreams-warmer.log'

BUDGET_S = 600          # hard cap for the whole run
VIEW_LIMIT = 60         # never more than this many views
META_BATCH = 40         # ids per /Items?Ids= call
MAX_META_BATCHES = 30   # hard cap on meta batches per run

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


def _get_uid(token):
    d, _ = call('/Users/Me', token)
    return d.get('Id')


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
    uid = _get_uid(token)

    def warm(path, label):
        nonlocal ok, bad
        try:
            d, _ = call(path, token)
            ok += 1
            for i in items_of(d):
                if i.get('Id'):
                    meta_ids.append(i.get('Id'))
        except Exception as exc:
            bad += 1
            log('  FAIL %s: %s (%s)' % (label, path.split('?')[0], type(exc).__name__))

    meta_ids = []
    for v in items:
        if time.time() - t_start > BUDGET_S:
            log('budget hit during rows')
            break
        vid, name = v.get('Id'), v.get('Name', '?')
        for lim in (15, 60):
            warm('/Users/%s/Items/Latest?ParentId=%s&Limit=%d&Fields=%s&EnableImageTypes=%s&ImageTypeLimit=1'
                 % (uid, vid, lim, q(F_LONG), q(IMG_LONG)), name)

    # media-bar pool: recentlyReleased windows + SortName count probes
    maxpremiere = q(datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    for vid in BAR_CATALOGS:
        for itypes in ('Movie', 'Series'):
            warm('/Users/%s/Items?ParentId=%s&Recursive=true&IncludeItemTypes=%s&Limit=40&Fields=%s'
                 '&EnableImageTypes=%s&SortBy=PremiereDate&SortOrder=Descending&MaxPremiereDate=%s'
                 % (uid, vid, q(itypes), q(F_BAR), q(IMG_BAR), maxpremiere), 'bar %s %s' % (vid[:12], itypes))
        warm('/Users/%s/Items?ParentId=%s&Recursive=true&IncludeItemTypes=%s&SortBy=SortName'
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
        warm(path, 'extra')

        # detail-page prewarm: the items the user most likely opens next
    # (NextUp + Resume) in the EXACT Moonfin detail shape = the cold 2s call
    detail_fields = ('Chapters,MediaSources,MediaStreams,People,Overview,Genres,UserData,'
                     'RecursiveItemCount,ChildCount,ParentLogoItemId,ParentLogoImageTag,DateCreated')
    detail_ids = []
    for dp in ('/Shows/NextUp?UserId=%s&Limit=10&Fields=UserData' % uid,
               '/UserItems/Resume?MediaTypes=Video&Limit=10'):
        try:
            dd, _ = call(dp, token, timeout=60)
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
            call('/Users/%s/Items/%s?Fields=%s' % (uid, iid, q(detail_fields)), token, timeout=60)
            det_ok += 1
        except Exception:
            det_bad += 1
    log('  detail prewarm: ok=%d fail=%d' % (det_ok, det_bad))

    # collections-row children prewarm: exact client shape per BoxSet
    try:
        bx, _ = call('/Users/%s/Items?IncludeItemTypes=BoxSet&Limit=50&Recursive=true&Fields=%s'
                     % (uid, q(F_LONG)), token, timeout=120)
        box_ids = [i.get('Id') for i in items_of(bx) if i.get('Id')][:60]
    except Exception:
        box_ids = []
    ch_ok = ch_bad = 0
    for bid in box_ids:
        if time.time() - t_start > BUDGET_S:
            break
        try:
            call('/Users/%s/Items?ParentId=%s&Recursive=true&Fields=UserData' % (uid, bid), token, timeout=60)
            ch_ok += 1
        except Exception:
            ch_bad += 1
    log('  children prewarm: ok=%d fail=%d' % (ch_ok, ch_bad))

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
