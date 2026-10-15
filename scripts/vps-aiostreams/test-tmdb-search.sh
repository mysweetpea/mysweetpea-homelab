#!/usr/bin/env bash
# Smoke test: tmdb-search addon (run on the VPS)
B=${1:-http://127.0.0.1:3010}
F=0; N=0
t() { N=$((N+1)); if printf '%s' "$2" | grep -q "$3"; then echo "ok   $1"; else echo "FAIL $1  (pattern '$3' not found)"; F=1; fi; }
M=$(curl -s -m 5 "$B/manifest.json"); t "manifest name" "$M" '"name": "TMDB Search"'
t "catalog search extra" "$M" '"isRequired": true'
R=$(curl -s -m 8 "$B/catalog/movie/search/search=fargo.json"); t "movie search returns tmdb ids" "$R" '"id": "tmdb:'
t "known title present (Fargo)" "$R" '"name": "Fargo"'
t "posters present" "$R" 'image.tmdb.org'
R2=$(curl -s -m 8 "$B/catalog/series/search/search=tracker.json"); t "series search works" "$R2" '"type": "series"'
R3=$(curl -s -m 8 "$B/catalog/movie/search/search=fargo&skip=0.json"); t "ampersand extra form parses" "$R3" '"id": "tmdb:'
E=$(curl -s -m 5 "$B/catalog/movie/search/search=.json"); t "empty query -> empty metas" "$E" '"metas": \[\]'
echo "$((N-F))/$N passed"
exit $F
