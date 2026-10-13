#!/usr/bin/env bash
# Smoke tests for moonbase-lite. TOKEN = a valid AIOStreams media-server token.
set -u
BASE="${1:-http://127.0.0.1:3002}"
AUTH="Authorization: MediaBrowser Token=\"${TOKEN:?export TOKEN first}\""
pass=0; fail=0
chk() { # name expected_code url [extra curl args...]
  local name="$1" want="$2" url="$3"; shift 3
  local got=$(curl -s -o /tmp/mbl.out -w '%{http_code}' -m 10 "$@" "$url")
  if [ "$got" = "$want" ]; then echo "PASS $name ($got)"; pass=$((pass+1));
  else echo "FAIL $name (got $got want $want)"; cat /tmp/mbl.out | head -3; fail=$((fail+1)); fi
}
chk web-config 200 "$BASE/Moonfin/Web/config.json"
chk themes-list 200 "$BASE/Moonfin/Themes" -H "$AUTH"
if curl -s -m 10 -H "$AUTH" "$BASE/Moonfin/Themes" | python3 -c "import json,sys; t=json.load(sys.stdin); assert len(t)>0, t" 2>/dev/null; then
  echo "PASS themes-content"; pass=$((pass+1));
else echo "FAIL themes-content (empty list)"; fail=$((fail+1)); fi
chk ping 200 "$BASE/Moonfin/Ping" -H "$AUTH"
chk resolved 200 "$BASE/Moonfin/Settings/Resolved/mobile" -H "$AUTH"
chk messages 200 "$BASE/Moonfin/Messages" -H "$AUTH"
chk sse-head 200 "$BASE/Moonfin/Settings/Stream" -H "$AUTH" --max-time 2
chk noauth 401 "$BASE/Moonfin/Ping" -H "Authorization: MediaBrowser Token=\"bogus\""
chk unknown 404 "$BASE/Moonfin/Nope" -H "$AUTH"
echo "== $pass passed, $fail failed =="
[ $fail -eq 0 ]
