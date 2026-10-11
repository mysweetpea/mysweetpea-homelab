#!/usr/bin/env python3
# seerr-notify — request lifecycle popups for Moonfin (in-app messages, delivery=popup).
# Polls Seerr every run:
#   new request        -> "Request received" popup to the requester (A2/A4)
#   became available   -> "Ready to watch" popup (A3 fallback — native push relay is 403 for self-built clients)
#   >48h not available -> one "still on it" nudge (A5)
# First run seeds state (no messages for pre-existing requests). Log: /var/log/seerr-notify.log
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error, datetime

SEERR = 'http://10.43.186.155:5055'
JF = 'http://10.43.244.254:8096'
STATE = '/root/.seerr-notify-state.json'
LOG = '/var/log/seerr-notify.log'
DRY = '--dry-run' in sys.argv
TEST = '--test' in sys.argv
MAX_MSGS = 10

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

SEERR_KEY = secret('SEERR_API_KEY')
JF_KEY = secret('JELLYFIN_API_KEY')

def call(base, path, headers, method='GET', body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:150].decode(errors='replace')
    except Exception as e:
        return 0, str(e)

SH = {'X-Api-Key': SEERR_KEY, 'User-Agent': 'Mozilla/5.0'}
JH = {'Authorization': 'MediaBrowser Token="%s"' % JF_KEY, 'Content-Type': 'application/json'}

def jf_message(title, body, user_ids, color='green'):
    msg = {'Title': title, 'Body': body, 'Color': color, 'Delivery': 'popup',
           'Audience': 'users', 'TargetUserIds': list(user_ids)}
    if DRY:
        log('WOULD message %s -> %s' % (user_ids, title))
        return True
    st, resp = call(JF, '/Moonfin/Admin/Messages', JH, 'POST', msg)
    log('message [%s] -> %s' % (title[:50], st))
    return st in (200, 204)

def media_title(x):
    m = x.get('media') or {}
    mt, tid = m.get('mediaType'), m.get('tmdbId')
    if not mt or not tid:
        return 'your request'
    st, d = call(SEERR, '/api/v1/%s/%s' % (mt, tid), SH)
    if st == 200 and isinstance(d, dict):
        return d.get('title') or d.get('name') or 'your request'
    return 'your request'

if TEST:
    uids = sys.argv[sys.argv.index('--user') + 1].split(',') if '--user' in sys.argv else ['3d75456ef9c5438480195d731242133d']
    jf_message('Test popup ✓', 'The request-notification system is live. You will see messages like this when you request something and when it becomes ready.', uids)
    sys.exit(0)

state = json.load(open(STATE)) if os.path.exists(STATE) else {}
seeded = state.get('seeded', False)
reqs = state.setdefault('reqs', {})

st, r = call(SEERR, '/api/v1/request?take=200&sort=added&sortDirection=desc', SH)
if st != 200 or not isinstance(r, dict):
    log('cannot list requests (%s) - exiting' % st)
    sys.exit(0)

sent = 0
now = datetime.datetime.now(datetime.timezone.utc)
for x in (r.get('results') or []):
    rid = str(x.get('id'))
    s = reqs.setdefault(rid, {})
    m = x.get('media') or {}
    mstatus = m.get('status')  # 1 unknown 2 pending 3 processing 4 partial 5 available
    rb = x.get('requestedBy') or {}
    jfuid = rb.get('jellyfinUserId')
    created = x.get('createdAt') or ''
    if not seeded:
        s['ack'] = True
        s['avail'] = mstatus == 5
        continue
    if not jfuid:
        s['ack'] = True
        continue
    # A2/A4: ack every new request
    if not s.get('ack'):
        title = media_title(x)
        if sent < MAX_MSGS and jf_message('Request received ✅',
           '**%s** is approved and searching now. It usually lands within hours — you will get a ping the moment it is ready. 🍿' % title,
           [jfuid]):
            sent += 1
        s['ack'] = True
    # A3: ready ping
    if mstatus == 5 and not s.get('avail'):
        title = media_title(x)
        if sent < MAX_MSGS and jf_message('🎉 Ready to watch',
           '**%s** just landed in your library — enjoy!' % title, [jfuid]):
            sent += 1
        s['avail'] = True
    # A5: 48h still-searching nudge (once)
    if not s.get('avail') and not s.get('nudge') and mstatus in (3, 4) and created:
        try:
            age = now - datetime.datetime.fromisoformat(created.replace('Z', '+00:00'))
            if age.total_seconds() > 48 * 3600:
                title = media_title(x)
                if sent < MAX_MSGS and jf_message('Still on it ⏳',
                   'We are still working on **%s** — it is taking longer than usual, but it is not forgotten. No action needed.' % title,
                   [jfuid], color='yellow'):
                    sent += 1
                s['nudge'] = True
        except Exception:
            pass

state['seeded'] = True
if not DRY:
    json.dump(state, open(STATE, 'w'))
log('done. requests=%d sent=%d seeded=%s dry=%s' % (len(r.get('results') or []), sent, seeded, DRY))
