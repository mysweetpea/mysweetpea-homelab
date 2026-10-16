#!/usr/bin/env python3
"""TMDB-direct search + deep streaming catalogs addon for AIOStreams.

Search (2026-10-15): the enrichment-heavy TMDB addon takes 0.7-12s+ per cold
search (30s stalls seen); TMDB's own API answers the same query in ~0.05-0.15s.
Serves page-1 results in the exact `tmdb:<id>` preview format.

Search intents (2026-10-16): category queries ("anime", "adventure anime",
"k-drama", "true crime", "feel good", "hidden gems", ...) resolve to TMDB
discover instead of literal title matches. Plain genre words ("comedy",
"horror", "romantic comedy", "science fiction") and "<genre> movies/shows"
suffixes are recognised too. Unmatched terms fall through to the normal
TMDB title search.

Discovery families (2026-10-16): 44 sub-genre / mood / curated catalogs
(anime sub-genres, moods, critically-acclaimed/hidden-gems/best-of/cult,
true crime, K-Dramas, Korean movies). All popularity-sorted unless noted.
v1.3.0 adds cross-genre combos (Romantic Comedy, Action Comedy, Crime Drama, Sci-Fi /
Psychological / Horror Thriller, Fantasy / Family Adventure, Dark Comedy, Superhero)
and decades (80s / 90s / 2000s).

Deep catalogs (2026-10-15): per-service browse catalogs backed by TMDB discover
(watch-provider filtered; US unless noted) with Stremio `skip` paging.
Provider ids verified live: Paramount+ = 2303 (531 is legacy junk), Canal+
needs watch_region FR (US returns 0 results). Keyword ids verified live:
anime=210024, isekai=237451, mecha=10046, slice-of-life=9914, sport=6075,
horror=315058, true crime=33722, cult=6158.

Endpoints (Stremio catalog protocol):
  GET /manifest.json
  GET /catalog/{movie|series}/search/search=<term>.json
  GET /catalog/{movie|series}/<cid>.json[/skip=N]

ponytail: single-process ThreadingHTTPServer is plenty (<1 req/s from aios); no config knobs.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 3010
KEY_FILE = "/opt/moonbase-lite/tmdb.key"
TMDB = "https://api.themoviedb.org/3"
TMDB_TIMEOUT = 4          # hard ceiling: on expiry return empty rather than hang aios search
CACHE_TTL = 600           # per (kind, term); aios layers its own 6h cache above
DEEP_TTL = 21600          # discover pages change slowly; 6h matches aios catalog cache
LIMIT = 20

# (catalog key, tmdb provider id, display name, watch region, has_movies, has_shows)
SERVICES = [
    ("netflix", 8, "Netflix", "US", True, True),
    ("prime", 9, "Prime Video", "US", True, True),
    ("disney", 337, "Disney+", "US", True, True),
    ("hbo", 1899, "HBO Max", "US", True, True),
    ("hulu", 15, "Hulu", "US", True, True),
    ("paramount", 2303, "Paramount+", "US", True, True),
    ("peacock", 386, "Peacock", "US", True, True),
    ("apple", 350, "Apple TV+", "US", True, True),
    ("crunchyroll", 283, "Crunchyroll", "US", True, True),
    ("discovery", 520, "Discovery+", "US", False, True),
    ("canal", 381, "Canal+", "FR", True, True),
]

CATALOG_DEFS = {}   # (stremio_type, catalog_id) -> (provider_id, region)
DEEP_CATALOGS = []
for _k, _pid, _name, _region, _mv, _sh in SERVICES:
    for _kind, _suffix, _has in (("movie", "movies", _mv), ("series", "shows", _sh)):
        if not _has:
            continue
        _cid = f"{_k}-{_suffix}"
        CATALOG_DEFS[(_kind, _cid)] = (_pid, _region)
        DEEP_CATALOGS.append({
            "type": _kind, "id": _cid,
            "name": f"{_name} {'Movies' if _kind == 'movie' else 'Shows'}",
            "extra": [{"name": "skip"}],
        })

# ---------------------------------------------------------------------------
# Discovery families (2026-10-16) - sub-genre / mood / curated rows
# ---------------------------------------------------------------------------

def _d(**kw):
    """Discover params with house defaults."""
    kw.setdefault("sort_by", "popularity.desc")
    kw.setdefault("include_adult", "false")
    return kw

_AKW = "210024"  # TMDB keyword: anime

# (id base, display name, movie params | None, series params | None)
DISCOVER_TABLE = [
    # --- anime hub + sub-genres (anime keyword verified live 2026-10-16) ---
    ("anime", "Anime",
     _d(with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-action", "Anime: Action & Adventure",
     _d(with_genres="28|12", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="10759", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-romance", "Anime: Romance",
     _d(with_genres="10749", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="10749", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-comedy", "Anime: Comedy",
     _d(with_genres="35", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="35", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-fantasy", "Anime: Fantasy",
     _d(with_genres="14", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="10765", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-scifi", "Anime: Sci-Fi",
     _d(with_genres="878", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="10765", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-isekai", "Anime: Isekai",
     _d(with_keywords=f"{_AKW},237451"),
     _d(with_keywords=f"{_AKW},237451")),
    ("anime-sol", "Anime: Slice of Life",
     _d(with_keywords=f"{_AKW},9914"),
     _d(with_keywords=f"{_AKW},9914")),
    ("anime-sports", "Anime: Sports",
     _d(with_keywords=f"{_AKW},6075"),
     _d(with_keywords=f"{_AKW},6075")),
    ("anime-horror", "Anime: Horror",
     _d(with_genres="27", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_keywords=f"{_AKW},315058")),
    ("anime-mecha", "Anime: Mecha",
     _d(with_keywords=f"{_AKW},10046"),
     _d(with_genres="16", with_keywords="10046")),
    # --- moods ---
    ("feel-good", "Feel-Good",
     _d(with_genres="35|10751", **{"vote_average.gte": "6.5", "vote_count.gte": "500"}),
     _d(with_genres="35|10751", **{"vote_average.gte": "6.5", "vote_count.gte": "100"})),
    ("dark-gritty", "Dark & Gritty",
     _d(with_genres="80|53", **{"vote_count.gte": "300"}),
     _d(with_genres="80|9648", **{"vote_count.gte": "100"})),
    ("mind-bending", "Mind-Bending",
     _d(with_genres="878|9648", sort_by="vote_average.desc",
        **{"vote_average.gte": "7.5", "vote_count.gte": "1000"}),
     _d(with_genres="9648|10765", sort_by="vote_average.desc",
        **{"vote_average.gte": "7.5", "vote_count.gte": "300"})),
    ("cozy", "Cozy",
     _d(with_genres="35|10751", **{"vote_average.gte": "7.0", "vote_count.gte": "300"}),
     _d(with_genres="35|10751", **{"vote_average.gte": "7.0", "vote_count.gte": "100"})),
    ("epic", "Epic",
     _d(with_genres="12|14", **{"with_runtime.gte": "120", "vote_count.gte": "500"}),
     _d(with_genres="10759|10765", **{"vote_average.gte": "7.0", "vote_count.gte": "300"})),
    # --- curated ---
    ("acclaimed", "Critically Acclaimed",
     _d(sort_by="vote_average.desc", **{"vote_average.gte": "7.0", "vote_count.gte": "5000"}),
     _d(sort_by="vote_average.desc", **{"vote_average.gte": "7.5", "vote_count.gte": "1000"})),
    ("hidden-gems", "Hidden Gems",
     _d(**{"vote_average.gte": "7.5", "vote_count.gte": "50", "vote_count.lte": "1500",
           "primary_release_date.gte": "2010-01-01"}),
     _d(**{"vote_average.gte": "7.5", "vote_count.gte": "40", "vote_count.lte": "1000",
           "first_air_date.gte": "2010-01-01"})),
    ("best", "Best of All Time",
     _d(sort_by="vote_average.desc", **{"vote_count.gte": "10000"}),
     _d(sort_by="vote_average.desc", **{"vote_count.gte": "2500"})),
    ("cult", "Cult Classics",
     _d(with_keywords="6158", **{"vote_count.gte": "200"}),
     _d(with_keywords="6158", **{"vote_count.gte": "50"})),
    # --- specials ---
    ("true-crime", "True Crime",
     _d(with_genres="99|80", with_keywords="33722"),
     _d(with_genres="99|80", with_keywords="33722")),
    ("kdrama", "K-Dramas", None,
     _d(with_original_language="ko", with_genres="18|10749", **{"vote_count.gte": "50"})),
    ("korean", "Korean Movies",
     _d(with_original_language="ko", with_genres="18|53", **{"vote_count.gte": "100"}), None),
    # --- cross-genre combos (Netflix-style multi-genre shelves) ---
    ("romantic-comedy", "Romantic Comedy",
     _d(with_genres="35,10749", **{"vote_count.gte": "200"}),
     _d(with_genres="35", with_keywords="9840", **{"vote_count.gte": "30"})),
    ("action-comedy", "Action Comedy",
     _d(with_genres="28,35", **{"vote_count.gte": "200"}),
     _d(with_genres="10759,35", **{"vote_count.gte": "100"})),
    ("crime-drama", "Crime Drama",
     _d(with_genres="80,18", **{"vote_count.gte": "300"}),
     _d(with_genres="80,18", **{"vote_count.gte": "100"})),
    ("scifi-thriller", "Sci-Fi Thriller",
     _d(with_genres="878,53", **{"vote_count.gte": "300"}),
     _d(with_genres="10765", with_keywords="316362", **{"vote_count.gte": "100"})),
    ("psych-thriller", "Psychological Thriller",
     _d(with_keywords="12565", **{"vote_count.gte": "200"}),
     _d(with_keywords="12565", **{"vote_count.gte": "50"})),
    ("fantasy-adventure", "Fantasy Adventure",
     _d(with_genres="14,12", **{"vote_count.gte": "200"}),
     _d(with_genres="10765,10759", **{"vote_count.gte": "100"})),
    ("family-adventure", "Family Adventure",
     _d(with_genres="10751,12", **{"vote_count.gte": "200"}),
     _d(with_genres="10751,10759", **{"vote_count.gte": "50"})),
    ("horror-thriller", "Horror Thriller",
     _d(with_genres="27,53", **{"vote_count.gte": "200"}),
     _d(with_genres="9648", with_keywords="315058", **{"vote_count.gte": "30"})),
    ("dark-comedy", "Dark Comedy",
     _d(with_keywords="10123", **{"vote_count.gte": "100"}),
     _d(with_keywords="10123", **{"vote_count.gte": "50"})),
    ("superhero", "Superhero",
     _d(with_keywords="9715", **{"vote_count.gte": "100"}),
     _d(with_keywords="9715", **{"vote_count.gte": "50"})),
    # --- decades ---
    ("80s", "80s",
     _d(**{"primary_release_date.gte": "1980-01-01", "primary_release_date.lte": "1989-12-31",
           "vote_count.gte": "500"}),
     _d(**{"first_air_date.gte": "1980-01-01", "first_air_date.lte": "1989-12-31",
           "vote_count.gte": "100"})),
    ("90s", "90s",
     _d(**{"primary_release_date.gte": "1990-01-01", "primary_release_date.lte": "1999-12-31",
           "vote_count.gte": "500"}),
     _d(**{"first_air_date.gte": "1990-01-01", "first_air_date.lte": "1999-12-31",
           "vote_count.gte": "100"})),
    ("2000s", "2000s",
     _d(**{"primary_release_date.gte": "2000-01-01", "primary_release_date.lte": "2009-12-31",
           "vote_count.gte": "500"}),
     _d(**{"first_air_date.gte": "2000-01-01", "first_air_date.lte": "2009-12-31",
           "vote_count.gte": "100"})),
]

DISCOVER_DEFS = {}      # (stremio_type, catalog_id) -> params
DISCOVER_CATALOGS = []
for _base, _name, _mv, _ser in DISCOVER_TABLE:
    _both = _mv is not None and _ser is not None
    for _kind, _params, _suffix in (("movie", _mv, " Movies"), ("series", _ser, " Shows")):
        if _params is None:
            continue
        _cid = f"{_base}-movies" if _kind == "movie" else f"{_base}-shows"
        _full = _name if (not _both or _name.endswith("Movies") or _name.endswith("Shows")) else _name + _suffix
        DISCOVER_DEFS[(_kind, _cid)] = _params
        DISCOVER_CATALOGS.append({
            "type": _kind, "id": _cid, "name": _full,
            "extra": [{"name": "skip"}],
        })

MANIFEST = {
    "id": "org.mysweetpea.tmdb-search",
    "version": "1.3.0",
    "name": "TMDB Search",
    "description": "Fast TMDB search and deep streaming catalogs for AIOStreams",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "catalogs": [
        {"type": "movie", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
        {"type": "series", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
    ] + DEEP_CATALOGS + DISCOVER_CATALOGS,
}

# ---------------------------------------------------------------------------
# Search intents (2026-10-16)
# ---------------------------------------------------------------------------

INTENTS = {}            # key -> (movie params | None, series params | None)
for _base, _name, _mv, _ser in DISCOVER_TABLE:
    INTENTS[_base] = (_mv, _ser)

_GENRE_MAP = {          # word -> (movie genre, series genre | None)
    "action": ("28", "10759"),
    "adventure": ("12", "10759"),
    "animation": ("16", "16"),
    "animated": ("16", "16"),
    "comedy": ("35", "35"),
    "crime": ("80", "80"),
    "documentary": ("99", "99"),
    "drama": ("18", "18"),
    "family": ("10751", "10751"),
    "fantasy": ("14", "10765"),
    "history": ("36", None),
    "mystery": ("9648", "9648"),
    "romance": ("10749", "10749"),
    "scifi": ("878", "10765"),
    "thriller": ("53", None),
    "war": ("10752", "10768"),
    "western": ("37", "37"),
    "music": ("10402", None),
}
for _g, (_m, _s) in _GENRE_MAP.items():
    INTENTS[_g] = (_d(with_genres=_m, **{"vote_count.gte": "200"}) if _m else None,
                   _d(with_genres=_s, **{"vote_count.gte": "100"}) if _s else None)
# series horror has no TMDB genre; keyword it
INTENTS["horror"] = (_d(with_genres="27", **{"vote_count.gte": "200"}),
                     _d(with_keywords="315058", **{"vote_count.gte": "100"}))
INTENTS["romcom"] = (_d(with_genres="35,10749", **{"vote_count.gte": "200"}),
                     _d(with_genres="35", with_keywords="9840", **{"vote_count.gte": "30"}))
INTENTS["korean"] = (INTENTS["korean"][0], INTENTS["kdrama"][1])

PHRASE_MAP = {
    "anime": "anime",
    "isekai": "anime-isekai",
    "mecha": "anime-mecha",
    "slice of life": "anime-sol",
    "k-drama": "korean",
    "k-dramas": "korean",
    "kdrama": "korean",
    "korean": "korean",
    "korean drama": "korean",
    "korean dramas": "korean",
    "korean films": "korean",
    "true crime": "true-crime",
    "rom com": "romcom",
    "romcom": "romcom",
    "romantic comedy": "romcom",
    "psychological thriller": "psych-thriller",
    "dark comedy": "dark-comedy",
    "black comedy": "dark-comedy",
    "superhero": "superhero",
    "crime drama": "crime-drama",
    "action comedy": "action-comedy",
    "sci-fi thriller": "scifi-thriller",
    "80s": "80s",
    "90s": "90s",
    "2000s": "2000s",
    "feel good": "feel-good",
    "feel-good": "feel-good",
    "feelgood": "feel-good",
    "mind bending": "mind-bending",
    "mind-bending": "mind-bending",
    "cozy": "cozy",
    "cosy": "cozy",
    "epic": "epic",
    "hidden gems": "hidden-gems",
    "hidden gem": "hidden-gems",
    "critically acclaimed": "acclaimed",
    "cult classics": "cult",
    "cult classic": "cult",
    "best of all time": "best",
    "science fiction": "scifi",
    "sci-fi": "scifi",
    "sci fi": "scifi",
    "gritty": "dark-gritty",
    "dark and gritty": "dark-gritty",
    "dark & gritty": "dark-gritty",
}
for _g in _GENRE_MAP:
    PHRASE_MAP.setdefault(_g, _g)
PHRASE_MAP["horror"] = "horror"

ANIME_SUBS = {
    "action": "anime-action",
    "adventure": "anime-action",
    "action & adventure": "anime-action",
    "romance": "anime-romance",
    "comedy": "anime-comedy",
    "fantasy": "anime-fantasy",
    "sci-fi": "anime-scifi",
    "scifi": "anime-scifi",
    "sci fi": "anime-scifi",
    "science fiction": "anime-scifi",
    "isekai": "anime-isekai",
    "mecha": "anime-mecha",
    "slice of life": "anime-sol",
    "sports": "anime-sports",
    "sport": "anime-sports",
    "horror": "anime-horror",
}

_SUFFIXES = (" movies", " movie", " shows", " show", " series")


def match_intent(term):
    """Normalised category query -> intent key, or None for a plain title search."""
    t = " ".join(term.lower().split())
    if not t:
        return None
    if t in PHRASE_MAP:
        return PHRASE_MAP[t]
    for sfx in _SUFFIXES:
        if t.endswith(sfx):
            base = t[:-len(sfx)].strip()
            if base in PHRASE_MAP:
                return PHRASE_MAP[base]
            if base in ANIME_SUBS:
                return ANIME_SUBS[base]
            return None
    if t.endswith(" anime"):
        sub = t[:-6].strip()
        return ANIME_SUBS.get(sub, "anime")
    if t.startswith("anime "):
        sub = t[6:].strip()
        return ANIME_SUBS.get(sub, "anime")
    return None

# ---------------------------------------------------------------------------
# TMDB fetch layer
# ---------------------------------------------------------------------------

_cache = {}   # ponytail: unbounded, but keys are human-typed search terms
_dcache = {}  # (kind, cache_key, page) -> (ts, metas)


def _key():
    with open(KEY_FILE) as f:
        return f.read().strip()


def _metas(results, stype):
    """TMDB result list -> Stremio MetaPreview list (shared by search + discover)."""
    out = []
    for m in (results or [])[:LIMIT]:
        name = m.get("title") or m.get("name")
        if not m.get("id") or not name:
            continue
        item = {"id": f"tmdb:{m['id']}", "type": stype, "name": name}
        if m.get("poster_path"):
            item["poster"] = "https://image.tmdb.org/t/p/w500" + m["poster_path"]
        if m.get("backdrop_path"):
            item["background"] = "https://image.tmdb.org/t/p/w780" + m["backdrop_path"]
        rel = (m.get("release_date") or m.get("first_air_date") or "")[:4]
        if rel:
            item["releaseInfo"] = rel
        out.append(item)
    return out


def metas_for(kind, term, skip=0):
    """kind: 'movie'|'tv' -> list of Stremio MetaPreview dicts."""
    intent = match_intent(term)
    if intent:
        params = INTENTS.get(intent, (None, None))[0 if kind == "movie" else 1]
        if params is None:
            return []  # intent recognised, nothing on this side (e.g. "thriller" series)
        out = discover_metas(kind, params, f"intent:{intent}", skip)
        print(f"tmdb-search: intent {term!r} -> {intent} -> {len(out)}", flush=True)
        return out
    ck = (kind, term.lower(), skip)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    try:
        q = urllib.parse.urlencode(
            {"query": term, "api_key": _key(), "include_adult": "false",
             "page": skip // LIMIT + 1})
        with urllib.request.urlopen(f"{TMDB}/search/{kind}?{q}",
                                    timeout=TMDB_TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except Exception as e:  # fail-soft: search must never hang on us
        print(f"tmdb-search: {kind} {term!r} failed: {e}", flush=True)
        return []  # ponytail: failures not cached, so recovery is immediate
    out = _metas(data.get("results"), "movie" if kind == "movie" else "series")
    _cache[ck] = (time.time(), out)
    print(f"tmdb-search: {kind} {term!r} -> {len(out)}", flush=True)
    return out


def _discover_page(kind, params, cache_key, page):
    k = (kind, cache_key, page)
    hit = _dcache.get(k)
    if hit and time.time() - hit[0] < DEEP_TTL:
        return hit[1]
    try:
        q = urllib.parse.urlencode({**params, "page": page, "api_key": _key()})
        with urllib.request.urlopen(f"{TMDB}/discover/{kind}?{q}",
                                    timeout=TMDB_TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"tmdb-search: discover {cache_key} p{page} failed: {e}", flush=True)
        return None  # not cached; recovery is immediate
    metas = _metas(data.get("results"), "movie" if kind == "movie" else "series")
    _dcache[k] = (time.time(), metas)
    return metas


def discover_metas(kind, params, cache_key, skip):
    """Page of a discover-backed catalog at Stremio offset `skip` (TMDB pages are 20)."""
    page = skip // LIMIT + 1
    off = skip % LIMIT
    if skip < 0 or page > 500:  # TMDB hard cap
        return []
    first = _discover_page(kind, params, cache_key, page)
    if first is None:
        return []
    out = first[off:off + LIMIT]
    if off and len(out) < LIMIT:
        nxt = _discover_page(kind, params, cache_key, page + 1)
        if nxt:
            out = out + nxt[:LIMIT - len(out)]
    return out


def _parsed_skip(parts, u):
    skip = 0
    for seg in parts[3:]:
        for piece in seg.split("&"):
            if piece.startswith("skip="):
                digits = "".join(c for c in piece[5:] if c.isdigit())
                skip = int(digits) if digits else 0
    qskip = (urllib.parse.parse_qs(u.query).get("skip") or [""])[0]
    if qskip.lstrip("-").isdigit():
        skip = int(qskip)
    return skip


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/")]
        if parts == ["manifest.json"]:
            return self._send(MANIFEST)
        if len(parts) >= 3 and parts[0] == "catalog":
            stype = parts[1]
            kind = {"movie": "movie", "series": "tv"}.get(stype)
            if kind:
                cid = parts[2]
                if cid.endswith(".json"):  # Stremio single-segment form: /catalog/movie/<id>.json
                    cid = cid[:-5]
                if cid == "search":
                    term = (urllib.parse.parse_qs(u.query).get("search") or [""])[0]
                    for seg in parts[3:]:
                        for piece in seg.split("&"):
                            if piece.startswith("search="):
                                term = piece[len("search="):]
                    term = term[:-5] if term.endswith(".json") else term
                    term = term.strip()
                    if not term:
                        return self._send({"metas": []})
                    return self._send({"metas": metas_for(kind, term, _parsed_skip(parts, u)),
                                       "cacheMaxAge": 3600})
                skip = _parsed_skip(parts, u)
                if (stype, cid) in CATALOG_DEFS:
                    provider_id, region = CATALOG_DEFS[(stype, cid)]
                    params = {"with_watch_providers": str(provider_id), "watch_region": region,
                              "sort_by": "popularity.desc"}
                    return self._send({"metas": discover_metas(kind, params, f"svc:{provider_id}:{region}", skip),
                                       "cacheMaxAge": 3600})
                if (stype, cid) in DISCOVER_DEFS:
                    return self._send({"metas": discover_metas(kind, DISCOVER_DEFS[(stype, cid)], f"cid:{cid}", skip),
                                       "cacheMaxAge": 3600})
            return self._send({"metas": []})  # unknown catalog type/id
        if not parts or parts == [""]:
            return self._send({"ok": True, "service": "tmdb-search"})
        self._send({"err": "not found"}, code=404)

    def log_message(self, fmt, *args):  # quiet default logging; we print our own
        pass


def _selftest():
    assert match_intent("anime") == "anime"
    assert match_intent("Adventure Anime") == "anime-action"
    assert match_intent("anime romance") == "anime-romance"
    assert match_intent("isekai") == "anime-isekai"
    assert match_intent("k-drama") == "korean"
    assert match_intent("Korean Dramas") == "korean"
    assert match_intent("true crime") == "true-crime"
    assert match_intent("true crime shows") == "true-crime"
    assert match_intent("feel good") == "feel-good"
    assert match_intent("romantic comedy") == "romcom"
    assert match_intent("science fiction") == "scifi"
    assert match_intent("horror movies") == "horror"
    assert match_intent("the matrix") is None
    assert match_intent("demon anime") == "anime"
    assert match_intent("spider man") is None
    assert match_intent("psychological thriller") == "psych-thriller"
    assert match_intent("dark comedy") == "dark-comedy"
    assert match_intent("superhero movies") == "superhero"
    assert match_intent("80s movies") == "80s"
    assert match_intent("crime drama") == "crime-drama"
    assert INTENTS["romantic-comedy"][0] and INTENTS["romantic-comedy"][1]
    # intent sides resolve for both kinds where promised
    for key in ("anime", "korean", "true-crime", "feel-good"):
        mv, ser = INTENTS[key]
        assert mv and ser, key
    assert len(DISCOVER_CATALOGS) == 70, len(DISCOVER_CATALOGS)
    assert len(MANIFEST["catalogs"]) == 2 + len(DEEP_CATALOGS) + 70
    ids = [(c["type"], c["id"]) for c in MANIFEST["catalogs"]]
    assert len(ids) == len(set(ids)), "duplicate (type, id) catalog entries"
    print("selftest ok: %d catalogs (%d discover)" % (len(MANIFEST["catalogs"]), len(DISCOVER_CATALOGS)))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
