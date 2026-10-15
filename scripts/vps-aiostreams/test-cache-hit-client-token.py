import json, re, time, urllib.request, urllib.parse, datetime
from collections import Counter
cfg = json.load(open('/opt/moonbase-lite/config.json'))
UUID = cfg['config_uuid']; UID = UUID.replace('-', '')
# verify with the CLIENT's real mount token (most frequent in the access log),
# NOT the warmer's - that mismatch once masked a phantom cache key.
log = open('/var/log/nginx/aiostreams.access.log', 'r', errors='replace').read()
toks = re.findall(r'/jellyfin/%s/([A-Za-z0-9_-]+)/Users' % re.escape(UUID), log)
ENC = Counter(toks).most_common(1)[0][0]
req = urllib.request.Request('http://127.0.0.1:3000/jellyfin/Users/AuthenticateByName',
    data=json.dumps({'Username': UUID, 'Pw': cfg['config_password']}).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
TOK = json.load(urllib.request.urlopen(req, timeout=15))['AccessToken']
BASE = 'https://aiostreams.mysweetpea.cc/jellyfin/%s/%s' % (UUID, ENC)
NOW = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
F = 'DateCreated%2CType%2CUserData%2COverview%2CGenres%2CCommunityRating%2CCriticRating%2COfficialRating%2CRunTimeTicks%2CProductionYear%2CSeriesName%2CParentIndexNumber%2CIndexNumber%2CStatus%2CImageTags%2CBackdropImageTags%2CParentBackdropItemId%2CParentBackdropImageTags%2CParentThumbItemId%2CParentThumbImageTag%2CSeriesId%2CSeriesPrimaryImageTag%2CParentLogoItemId%2CParentLogoImageTag%2CPrimaryImageTag%2CPrimaryImageAspectRatio'
IMG = 'Primary%2CBackdrop%2CThumb%2CBanner'
FB = 'Type%2CGenres%2COfficialRating%2CCommunityRating%2CCriticRating%2CRunTimeTicks%2CProductionYear%2CImageTags%2CBackdropImageTags%2COverview%2CProviderIds'
IB = 'Backdrop%2CLogo'
samples = [
 ('row L15 trend-mov', '/Users/%s/Items/Latest?ParentId=a2b3d475a9842e952002bbf66ada706f&Limit=15&Fields=%s&EnableImageTypes=%s&ImageTypeLimit=1' % (UID, F, IMG)),
 ('row L60 pop-shows', '/Users/%s/Items/Latest?ParentId=a20e1577e1543cd8707bba16912f48a4&Limit=60&Fields=%s&EnableImageTypes=%s&ImageTypeLimit=1' % (UID, F, IMG)),
 ('bar Movie@40', '/Users/%s/Items?ParentId=a2b3d475a9842e952002bbf66ada706f&Recursive=true&IncludeItemTypes=Movie&Limit=40&Fields=%s&EnableImageTypes=%s&SortBy=PremiereDate&SortOrder=Descending&MaxPremiereDate=%s' % (UID, FB, IB, urllib.parse.quote(NOW, safe=''))),
 ('bar Series@40', '/Users/%s/Items?ParentId=a26d1a33e2d56da6265981f9d902a374&Recursive=true&IncludeItemTypes=Series&Limit=40&Fields=%s&EnableImageTypes=%s&SortBy=PremiereDate&SortOrder=Descending&MaxPremiereDate=%s' % (UID, FB, IB, urllib.parse.quote(NOW, safe=''))),
 ('child b2de9695', '/Users/%s/Items?ParentId=b2de969555d319009aaacce5f4fc5d4c&Recursive=true&Fields=UserData' % UID),
 ('image (Primary)', '/Items/b2772b23b2034c1c61ec4e56d68a577d/Images/Primary'),
 ('resume live', '/UserItems/Resume?MediaTypes=Video&Limit=15'),
]
for name, path in samples:
    r = urllib.request.Request(BASE + path, headers={'Authorization': 'MediaBrowser Token="%s"' % TOK})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            body = resp.read(); ms = (time.time() - t0) * 1000
            print('%-18s %s %7.0fms %-7s %dB' % (name, resp.status, ms, resp.headers.get('X-Aios-Cache', '-'), len(body)))
    except Exception as e:
        print('%-18s ERR %s' % (name, e))
    time.sleep(0.1)
