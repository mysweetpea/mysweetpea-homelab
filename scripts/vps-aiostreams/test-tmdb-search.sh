#!/usr/bin/env bash
# Smoke suite for the tmdb-search addon (search + deep streaming catalogs).
# Run ON THE VPS:  bash test-tmdb-search.sh
set -u
BASE="http://127.0.0.1:3010"
pass=0; fail=0

chk() { # chk <name> <expected_status> <url>
  local name="$1" want="$2" url="$3"
  local got; got=$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$url")
  if [ "$got" = "$want" ]; then echo "PASS $name ($got)"; pass=$((pass+1));
  else echo "FAIL $name (got $got want $want)"; fail=$((fail+1)); fi
}
pychk() { # pychk <name> <url> <python-assert-code (stdin = response)>
  local name="$1" url="$2" code="$3"
  if curl -s -m 25 "$url" | python3 -c "$code" 2>/dev/null; then
    echo "PASS $name"; pass=$((pass+1))
  else echo "FAIL $name"; fail=$((fail+1)); fi
}

chk manifest 200 "$BASE/manifest.json"
chk root 200 "$BASE/"
chk bad-path 404 "$BASE/nope"

pychk manifest-23 "$BASE/manifest.json" 'import json,sys; m=json.load(sys.stdin); c=m["catalogs"]; assert len(c)==111, len(c)  # 2 search + 21 deep + 86 discover + 2 dynamic; assert sum(1 for x in c if x["id"]=="search")==2; assert sum(1 for x in c if x["id"].endswith(("-movies","-shows")))==21; assert "skip" in json.dumps(c)'

# search regression: both types answer with tmdb: previews
pychk search-movie "$BASE/catalog/movie/search/search=matrix.json" 'import json,sys; d=json.load(sys.stdin); ms=d["metas"]; assert ms and all(i["id"].startswith("tmdb:") for i in ms), len(ms); assert any("Matrix" in i["name"] for i in ms)'
pychk search-series "$BASE/catalog/series/search/search=breaking%20bad.json" 'import json,sys; d=json.load(sys.stdin); ms=d["metas"]; assert ms and any("Breaking Bad" in i["name"] for i in ms)'

# deep catalogs: netflix page + skip paging + specific providers
pychk deep-netflix-p1 "$BASE/catalog/movie/netflix-movies.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])==20, len(d["metas"])'
pychk deep-netflix-skip100 "$BASE/catalog/movie/netflix-movies/skip=100.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])==20, len(d["metas"])'
pychk deep-paging-differs "$BASE/catalog/movie/netflix-movies.json" 'import json,sys,urllib.request; a=json.load(sys.stdin)["metas"]; b=json.load(urllib.request.urlopen("http://127.0.0.1:3010/catalog/movie/netflix-movies/skip=100.json", timeout=20))["metas"]; assert a and b and {x["id"] for x in a} != {x["id"] for x in b}'
pychk deep-canal-fr "$BASE/catalog/movie/canal-movies.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])==20, len(d["metas"])'
pychk deep-paramount "$BASE/catalog/movie/paramount-movies.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])==20, len(d["metas"])'
pychk deep-discovery-series "$BASE/catalog/series/discovery-shows.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])==20, len(d["metas"])'
pychk deep-all-21 "$BASE/manifest.json" 'import json,sys,urllib.request; m=json.load(sys.stdin); bad=[]
for c in m["catalogs"]:
    if c["id"]=="search": continue
    u="http://127.0.0.1:3010/catalog/%s/%s.json" % (c["type"], c["id"])
    try:
        d=json.load(urllib.request.urlopen(u, timeout=20))
        if not d.get("metas"): bad.append(c["id"])
    except Exception as e: bad.append("%s:%s" % (c["id"], e))
assert not bad, bad'

# v1.5: category / sub-category / streamer names resolve to their libraries' content
pychk intent-slice-of-life "$BASE/catalog/movie/search/search=slice%20of%20life.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=5, len(d["metas"])'
pychk intent-isekai "$BASE/catalog/series/search/search=isekai.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=5, len(d["metas"])'
pychk intent-anime-romance "$BASE/catalog/movie/search/search=anime%20romance.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=5, len(d["metas"])'
pychk intent-documentaries "$BASE/catalog/movie/search/search=documentaries.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=10, len(d["metas"])'
pychk intent-scifi-fantasy "$BASE/catalog/movie/search/search=sci-fi%20%26%20fantasy.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=10, len(d["metas"])'
pychk intent-netflix-shows "$BASE/catalog/series/search/search=netflix.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=15, len(d["metas"])'
pychk intent-netflix-movies-suffix "$BASE/catalog/movie/search/search=netflix%20movies.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=15, len(d["metas"])'
pychk intent-new-releases "$BASE/catalog/movie/search/search=new%20releases.json" 'import json,sys; d=json.load(sys.stdin); assert len(d["metas"])>=5, len(d["metas"])'
pychk title-still-works "$BASE/catalog/movie/search/search=the%20matrix.json" 'import json,sys; d=json.load(sys.stdin); assert any("Matrix" in i["name"] for i in d["metas"])'

echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
