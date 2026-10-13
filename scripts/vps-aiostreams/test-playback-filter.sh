#!/usr/bin/env bash
# AIOStreams PlaybackInfo browser-filter check (pi-filter sidecar, VPS 127.0.0.1:3001).
# Usage: AIOS_UUID=<config-uuid> AIOS_PW=<config-pw> bash test-playback-filter.sh [item-id]
# PASS = browser UA is served H.264-only sources, native UA gets the full list.
# (An item with no H.264 release at all legitimately falls back to the full list.)
set -u
B=https://aiostreams.mysweetpea.cc/jellyfin
UUID="${AIOS_UUID:?set AIOS_UUID (config uuid)}"
PW="${AIOS_PW:?set AIOS_PW (config password)}"
ITEM="${1:-a14102000000d3b92600020002000000}"
UA_C='Mozilla/5.0 (X11; Linux x86_64) Chrome/154.0.0.0 Safari/537.36'
UA_M='Mozilla/5.0 (compatible; Moonfin/2.5.1)'
TOK=$(curl -s -m 15 -X POST "$B/Users/AuthenticateByName" -H 'Content-Type: application/json' \
  -d "{\"Username\":\"$UUID\",\"Pw\":\"$PW\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("AccessToken",""))')
[ -n "$TOK" ] || { echo "FAIL: no token (check AIOS_UUID/AIOS_PW)"; exit 1; }
U="$B/Items/$ITEM/PlaybackInfo?userId=$(echo "$UUID" | tr -d '-')"
curl -s -m 30 -X POST "$U" -H "Authorization: MediaBrowser Token=\"$TOK\"" -H "User-Agent: $UA_C" -H 'Content-Type: application/json' -d '{}' -o /tmp/tpf-b.json -w 'browser: %{http_code} %{size_download}B\n'
curl -s -m 30 -X POST "$U" -H "Authorization: MediaBrowser Token=\"$TOK\"" -H "User-Agent: $UA_M" -H 'Content-Type: application/json' -d '{}' -o /tmp/tpf-m.json -w 'moonfin: %{http_code} %{size_download}B\n'
python3 << 'EOF'
import json
b = json.load(open('/tmp/tpf-b.json')).get('MediaSources', [])
m = json.load(open('/tmp/tpf-m.json')).get('MediaSources', [])
bv = [st.get('Codec') for x in b for st in x.get('MediaStreams', []) if st.get('Type') == 'Video']
print('browser: %d sources, all h264: %s' % (len(b), all(c in ('h264', 'avc1') for c in bv)))
print('moonfin: %d sources' % len(m))
print('PASS' if b and all(c in ('h264', 'avc1') for c in bv) else 'CHECK (browser list empty or contains non-h264 - filter off, or no h264 exists for this item)')
EOF
