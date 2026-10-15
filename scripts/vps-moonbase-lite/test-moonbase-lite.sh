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
chk register-post 200 "$BASE/Moonfin/Notifications/Register" -H "$AUTH" -H "Content-Type: application/json" -d '{"token":"t","platform":"linux","deviceId":"selftest"}'
if curl -s -m 15 -H "$AUTH" "$BASE/Moonfin/Tmdb/EpisodeRating?tmdbId=456&season=8&episode=23" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('success') and (d.get('voteAverage') or 0)>0, d" 2>/dev/null; then
  echo "PASS ep-rating-content"; pass=$((pass+1));
else echo "FAIL ep-rating-content"; fail=$((fail+1)); fi
chk companies 200 "$BASE/Moonfin/Tmdb/ProductionCompanies?tmdbId=1228834&type=movie" -H "$AUTH"
if python3 - "$BASE" "$TOKEN" <<'PYEOF'
import json,sys,urllib.request
base, tok = sys.argv[1], sys.argv[2]
def get(u, hdr=True):
    h = {"Authorization": 'MediaBrowser Token="%s"' % tok} if hdr else {}
    return urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=15).read()
d = json.loads(get(base + "/Moonfin/Tmdb/ProductionCompanies?tmdbId=1228834&type=movie"))
ids = [c["id"] for c in d.get("companies", []) if c.get("hasLogo") and c["id"] > 0][:4]
assert d.get("success") and ids, d
svg = get(base + "/Moonfin/Tmdb/StudioImage/7899?ApiKey=" + tok, hdr=False)
assert svg[:4] == b"\x89PNG", "7899 (svg-sourced logo) regressed"
for i in ids:
    try:
        if get(base + "/Moonfin/Tmdb/StudioImage/%d?ApiKey=%s" % (i, tok), hdr=False)[:4] == b"\x89PNG":
            print("studio-image ok for", i)
            break
    except Exception:
        pass
else:
    raise AssertionError("no PNG from %r" % ids)
PYEOF
then
  echo "PASS companies+studio-image-png (query token)"; pass=$((pass+1));
else echo "FAIL companies+studio-image-png"; fail=$((fail+1)); fi
chk studio-image-none 404 "$BASE/Moonfin/Tmdb/StudioImage/999999999" -H "$AUTH"
echo "== $pass passed, $fail failed =="
[ $fail -eq 0 ]
