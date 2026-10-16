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
v1.4.0 adds a dynamic New Releases pair (rolling 8-week window, refreshed daily).
v1.6.0 adds 22 genre/theme extensions (Animation, Family & Kids, Crime, Mystery,
History, War & Politics, Western, Musical, Reality TV, Sitcoms, Sports, Based on
a True Story, Heists & Cons, Spies & Espionage, Time Travel, Space, Zombie, Coming
of Age, Supernatural, Medical, Detective, Stand-Up Comedy) plus 3 anime sub-genres
(Drama, Mystery, Supernatural). Keyword ids verified live against TMDB.

Deep catalogs (2026-10-15): per-service browse catalogs backed by TMDB discover
(watch-provider filtered; US unless noted) with Stremio `skip` paging.
Provider ids verified live: Paramount+ = 2303 (531 is legacy junk), Canal+
needs watch_region FR (US returns 0 results). Keyword ids verified live:
anime=210024, isekai=237451, mecha=10046, slice-of-life=9914, sport=6075,
horror=315058, true crime=33722, cult=6158.

v1.7.0 adds Studio Ghibli / Chinese Dramas / Chinese Movies catalogs, 16:9
service tiles as `poster` on the deep streamer catalogs (AIOStreams turns
catalog.poster into the view's ImageTags -> Moonfin renders them on the My
Media row), and a misspell rescue in the search path: fuzzy category match
plus a word-pool title fallback for zero-result queries ("sprited away",
"intersteller", "chines drama").

Endpoints (Stremio catalog protocol):
  GET /manifest.json
  GET /catalog/{movie|series}/search/search=<term>.json
  GET /catalog/{movie|series}/<cid>.json[/skip=N]

ponytail: single-process ThreadingHTTPServer is plenty (<1 req/s from aios); no config knobs.
"""
import difflib
import json
import re
import sys
import time
from datetime import date, timedelta
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
# 16:9 brand tiles served by the VPS nginx (/var/www/aiostreams/logos/). aios
# maps catalog.poster -> view ImageTags.Primary, which is what puts the logo
# on the My Media row.
LOGO_BASE = "https://aiostreams.mysweetpea.cc/logos/"
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
            "poster": f"{LOGO_BASE}{_k}.png",
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
     _d(with_keywords=f"{_AKW},9840")),  # TV has no Romance genre; keyword 9840
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
    ("anime-drama", "Anime: Drama",
     _d(with_genres="18", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="18", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-mystery", "Anime: Mystery",
     _d(with_genres="9648", with_keywords=_AKW, **{"vote_count.gte": "50"}),
     _d(with_genres="9648", with_keywords=_AKW, **{"vote_count.gte": "50"})),
    ("anime-supernatural", "Anime: Supernatural",
     _d(with_keywords=f"{_AKW},6152"),
     _d(with_keywords=f"{_AKW},6152")),
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
    # v1.7.0 language/studio families (volumes probed live: zh dramas 64,
    # zh movies 100+, Ghibli company 10342 = 82)
    ("cdrama", "Chinese Dramas", None,
     _d(with_original_language="zh", with_genres="18|10749", **{"vote_count.gte": "50"})),
    ("chinese", "Chinese Movies",
     _d(with_original_language="zh", with_genres="18|53", **{"vote_count.gte": "100"}), None),
    ("ghibli", "Studio Ghibli",
     _d(with_companies="10342"), None),
    # --- mainstream top-level genres (2026-10-16) ---
    ("action", "Action & Adventure",
     _d(with_genres="28|12", **{"vote_count.gte": "500"}),
     _d(with_genres="10759", **{"vote_count.gte": "100"})),
    ("comedy", "Comedy",
     _d(with_genres="35", **{"vote_count.gte": "500"}),
     _d(with_genres="35", **{"vote_count.gte": "100"})),
    ("drama", "Drama",
     _d(with_genres="18", **{"vote_count.gte": "500"}),
     _d(with_genres="18", **{"vote_count.gte": "100"})),
    ("scifi-fantasy", "Sci-Fi & Fantasy",
     _d(with_genres="878|14", **{"vote_count.gte": "500"}),
     _d(with_genres="10765", **{"vote_count.gte": "100"})),
    ("horror", "Horror",
     _d(with_genres="27", **{"vote_count.gte": "300"}),
     _d(with_keywords="315058", **{"vote_count.gte": "30"})),
    ("thriller", "Thriller",
     _d(with_genres="53", **{"vote_count.gte": "500"}),
     _d(with_genres="80|9648", **{"vote_count.gte": "100"})),
    ("romance", "Romance",
     _d(with_genres="10749", **{"vote_count.gte": "300"}),
     _d(with_genres="10749", **{"vote_count.gte": "50"})),
    ("documentaries", "Documentaries",
     _d(with_genres="99", **{"vote_count.gte": "100"}),
     _d(with_genres="99", **{"vote_count.gte": "30"})),
    # --- genre + theme extensions (v1.6.0, 2026-10-16; keyword ids verified live) ---
    ("animation", "Animation",
     _d(with_genres="16", **{"vote_count.gte": "500"}),
     _d(with_genres="16", **{"vote_count.gte": "100"})),
    ("family-kids", "Family & Kids",
     _d(with_genres="10751", **{"vote_count.gte": "500"}),
     _d(with_genres="10751|10762", **{"vote_count.gte": "100"})),
    ("crime", "Crime",
     _d(with_genres="80", **{"vote_count.gte": "500"}),
     _d(with_genres="80", **{"vote_count.gte": "100"})),
    ("mystery", "Mystery",
     _d(with_genres="9648", **{"vote_count.gte": "500"}),
     _d(with_genres="9648", **{"vote_count.gte": "100"})),
    ("history", "History",
     _d(with_genres="36", **{"vote_count.gte": "300"}),
     _d(with_keywords="282633", **{"vote_count.gte": "10"})),
    ("war", "War & Politics",
     _d(with_genres="10752", **{"vote_count.gte": "300"}),
     _d(with_genres="10768", **{"vote_count.gte": "100"})),
    ("western", "Western",
     _d(with_genres="37", **{"vote_count.gte": "300"}),
     _d(with_genres="37", **{"vote_count.gte": "100"})),
    ("musical", "Musical",
     _d(with_keywords="4344", **{"vote_count.gte": "50"}),
     _d(with_keywords="4344", **{"vote_count.gte": "30"})),
    ("reality", "Reality TV", None,
     _d(with_genres="10764")),
    ("sitcoms", "Sitcoms", None,
     _d(with_keywords="193171", **{"vote_count.gte": "30"})),
    ("sports", "Sports",
     _d(with_keywords="6075", **{"vote_count.gte": "100"}),
     _d(with_keywords="6075", **{"vote_count.gte": "30"})),
    ("true-story", "Based on a True Story",
     _d(with_keywords="9672", **{"vote_count.gte": "50"}),
     _d(with_keywords="9672", **{"vote_count.gte": "30"})),
    ("heist", "Heists & Cons",
     _d(with_keywords="10051", **{"vote_count.gte": "50"}),
     _d(with_keywords="10051", **{"vote_count.gte": "10"})),
    ("spy", "Spies & Espionage",
     _d(with_keywords="470", **{"vote_count.gte": "50"}),
     _d(with_keywords="470", **{"vote_count.gte": "30"})),
    ("time-travel", "Time Travel",
     _d(with_keywords="4379", **{"vote_count.gte": "50"}),
     _d(with_keywords="4379", **{"vote_count.gte": "30"})),
    ("space", "Space",
     _d(with_keywords="9882", **{"vote_count.gte": "50"}),
     _d(with_keywords="9882", **{"vote_count.gte": "30"})),
    ("zombie", "Zombie",
     _d(with_keywords="12377", **{"vote_count.gte": "50"}),
     _d(with_keywords="12377", **{"vote_count.gte": "30"})),
    ("coming-of-age", "Coming of Age",
     _d(with_keywords="10683", **{"vote_count.gte": "50"}),
     _d(with_keywords="10683", **{"vote_count.gte": "30"})),
    ("supernatural", "Supernatural",
     _d(with_keywords="6152", **{"vote_count.gte": "50"}),
     _d(with_keywords="6152", **{"vote_count.gte": "30"})),
    ("medical", "Medical",
     _d(with_keywords="208788", **{"vote_count.gte": "10"}),
     _d(with_keywords="208788", **{"vote_count.gte": "10"})),
    ("detective", "Detective",
     _d(with_keywords="703", **{"vote_count.gte": "50"}),
     _d(with_keywords="703", **{"vote_count.gte": "30"})),
    ("standup", "Stand-Up Comedy",
     _d(with_keywords="9716", **{"vote_count.gte": "50"}), None),
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

# --- dynamic catalogs (params computed per request) ---

def _today():
    return date.today().isoformat()


def _new_releases_params(kind, today):
    """Rolling 8-week release window, popularity-sorted."""
    since = (date.fromisoformat(today) - timedelta(days=56)).isoformat()
    if kind == "movie":
        return _d(**{"primary_release_date.gte": since, "primary_release_date.lte": today,
                     "vote_count.gte": "25"})
    return _d(**{"first_air_date.gte": since, "first_air_date.lte": today,
                 "vote_count.gte": "10"})


DYNAMIC_DEFS = {
    "new-releases-movies": lambda t: _new_releases_params("movie", t),
    "new-releases-shows": lambda t: _new_releases_params("series", t),
}
DYNAMIC_CATALOGS = [
    {"type": "movie", "id": "new-releases-movies", "name": "New Releases Movies",
     "extra": [{"name": "skip"}]},
    {"type": "series", "id": "new-releases-shows", "name": "New Releases Shows",
     "extra": [{"name": "skip"}]},
]

MANIFEST = {
    "id": "org.mysweetpea.tmdb-search",
    "version": "1.7.0",
    "name": "TMDB Search",
    "description": "Fast TMDB search and deep streaming catalogs for AIOStreams",
    "resources": ["catalog"],
    "types": ["movie", "series"],
    "catalogs": [
        {"type": "movie", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
        {"type": "series", "id": "search", "name": "Search",
         "extra": [{"name": "search", "isRequired": True}]},
    ] + DEEP_CATALOGS + DISCOVER_CATALOGS + DYNAMIC_CATALOGS,
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
INTENTS["chinese"] = (INTENTS["chinese"][0], INTENTS["cdrama"][1])
# history's genre-map entry has no series side; restore the themed TV params
INTENTS["history"] = (_d(with_genres="36", **{"vote_count.gte": "300"}),
                      _d(with_keywords="282633", **{"vote_count.gte": "10"}))

# streamer LIBRARIES searchable by name (same params as their deep catalogs)
for _k, _pid, _name, _region, _mv, _sh in SERVICES:
    _p = _d(with_watch_providers=str(_pid), watch_region=_region)
    INTENTS[_k] = (_p if _mv else None, _p if _sh else None)

# engine-row intents: "new releases" computes its rolling window per request
INTENTS["new-releases"] = ("__DYNAMIC__", "__DYNAMIC__")
INTENTS["trending"] = (_d(**{"vote_count.gte": "300"}),
                       _d(**{"vote_count.gte": "100"}))

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

PHRASE_MAP.update({
    # phrase forms of the mainstream pairs
    "action & adventure": "action", "action and adventure": "action",
    "sci-fi & fantasy": "scifi-fantasy", "sci fi & fantasy": "scifi-fantasy",
    "sci-fi fantasy": "scifi-fantasy", "sci fi fantasy": "scifi-fantasy",
    "scifi fantasy": "scifi-fantasy", "science fiction fantasy": "scifi-fantasy",
    # genre plurals
    "comedies": "comedy", "dramas": "drama", "thrillers": "thriller",
    "romances": "romance", "documentaries": "documentaries",
    "musicals": "music", "westerns": "western", "mysteries": "mystery",
    "animes": "anime",
    # engine rows
    "new releases": "new-releases", "new release": "new-releases",
    "trending": "trending", "trending now": "trending",
    # streamer libraries (deep catalogs)
    "netflix": "netflix", "hulu": "hulu",
    "disney": "disney", "disney+": "disney", "disney plus": "disney",
    "prime": "prime", "prime video": "prime", "amazon": "prime", "amazon prime": "prime",
    "hbo": "hbo", "hbo max": "hbo",
    "apple": "apple", "apple tv": "apple", "apple tv+": "apple", "apple tv plus": "apple",
    "paramount": "paramount", "paramount+": "paramount", "paramount plus": "paramount",
    "peacock": "peacock", "crunchyroll": "crunchyroll",
    "discovery": "discovery", "discovery+": "discovery", "discovery plus": "discovery",
    "canal": "canal", "canal+": "canal", "canal plus": "canal",
})

# v1.6.0 genre/theme extensions (keyword ids verified live 2026-10-16)
PHRASE_MAP.update({
    "sitcom": "sitcoms", "sitcoms": "sitcoms",
    "reality": "reality", "reality tv": "reality",
    "stand-up": "standup", "stand up": "standup", "standup": "standup",
    "stand up comedy": "standup", "stand-up comedy": "standup",
    "musical": "musical", "musicals": "musical",
    "kids": "family-kids", "family & kids": "family-kids",
    "family and kids": "family-kids",
    "based on a true story": "true-story", "true story": "true-story",
    "true stories": "true-story",
    "heist": "heist", "heists": "heist",
    "spy": "spy", "spies": "spy", "espionage": "spy",
    "time travel": "time-travel",
    "space": "space",
    "zombie": "zombie", "zombies": "zombie",
    "coming of age": "coming-of-age",
    "supernatural": "supernatural",
    "medical": "medical", "medical drama": "medical", "medical dramas": "medical",
    "detective": "detective", "detectives": "detective",
    "sports": "sports", "sport": "sports",
})

# colloquial forms (live search audit 2026-10-16: "cartoons" fell through to
# a literal title search returning obscure shorts; map what users actually type)
PHRASE_MAP.update({
    "cartoon": "animation", "cartoons": "animation",
    "scary": "horror", "funny": "comedy",
    # v1.7.0 studio + language families
    "ghibli": "ghibli", "studio ghibli": "ghibli",
    "cdrama": "cdrama", "cdramas": "cdrama",
    "chinese drama": "cdrama", "chinese dramas": "cdrama",
    "chinese": "chinese", "chinese movies": "chinese", "chinese films": "chinese",
    "chinese shows": "chinese", "chinese series": "chinese", "chinese tv": "chinese",
})

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
    "drama": "anime-drama",
    "mystery": "anime-mystery",
    "supernatural": "anime-supernatural",
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
            # recurse, so "slice of life shows", "netflix movies" and
            # "anime horror shows" all resolve through the same tables
            return match_intent(t[:-len(sfx)].strip())
    if t.endswith(" anime"):
        sub = t[:-6].strip()
        return ANIME_SUBS.get(sub, "anime")
    if t.startswith("anime "):
        sub = t[6:].strip()
        return ANIME_SUBS.get(sub, "anime")
    return None


# Fuzzy fallbacks (2026-10-16): TMDB's own search is strict - one misspelled
# token returns nothing. Categories get a close-match pass, and zero-result
# title searches get a word-pool rescue.
_FUZZY_INTENT = 0.82
_FUZZY_MIN = 0.5


def fuzzy_intent(term):
    """Closest category for a misspelled one ('chines drama' -> cdrama)."""
    t = " ".join(term.lower().split())
    if not t:
        return None
    hit = difflib.get_close_matches(t, PHRASE_MAP, n=1, cutoff=_FUZZY_INTENT)
    return PHRASE_MAP[hit[0]] if hit else None

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


def _fuzzy_title(kind, term):
    """Zero-result rescue: pool TMDB hits for each query word (plus the word's
    2-char-short prefix - tail typos like 'intersteller' only match as
    'interstell'), then rank by similarity to the full query ('sprited away'
    -> the 'away' pool contains Spirited Away)."""
    ck = (kind, "fuzzy:" + term.lower(), 0)
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    words = [w for w in re.findall(r"[a-z0-9]+", term.lower()) if len(w) >= 4]
    if not words:
        return []
    probes = []
    for w in sorted(words, key=len, reverse=True)[:3]:
        probes.append(w)
        if len(w) >= 6:
            probes.append(w[:-2])
    pool = {}
    for p in probes[:5]:  # ponytail: 5 TMDB calls cap; only runs on zero-result quirks
        try:
            q = urllib.parse.urlencode({"query": p, "api_key": _key(),
                                        "include_adult": "false", "page": 1})
            with urllib.request.urlopen(f"{TMDB}/search/{kind}?{q}",
                                        timeout=TMDB_TIMEOUT) as r:
                for m in json.loads(r.read().decode()).get("results", []):
                    if m.get("id"):
                        pool[m["id"]] = m
        except Exception:
            continue
    if not pool:
        return []
    query = " ".join(term.lower().split())

    def score(m):
        name = (m.get("title") or m.get("name") or "").lower()
        return difflib.SequenceMatcher(None, query, name).ratio()

    ranked = sorted(pool.values(),
                    key=lambda m: (score(m), m.get("popularity", 0)), reverse=True)
    hits = [m for m in ranked if score(m) >= _FUZZY_MIN]
    out = _metas(hits, "movie" if kind == "movie" else "series")
    _cache[ck] = (time.time(), out)
    print(f"tmdb-search: fuzzy {kind} {term!r} -> {len(out)}", flush=True)
    return out


def metas_for(kind, term, skip=0):
    """kind: 'movie'|'tv' -> list of Stremio MetaPreview dicts."""
    intent = match_intent(term)
    if intent:
        params = INTENTS.get(intent, (None, None))[0 if kind == "movie" else 1]
        if params is None:
            return []  # intent recognised, nothing on this side (e.g. "thriller" series)
        if params == "__DYNAMIC__":
            params = _new_releases_params(kind, _today())
            out = discover_metas(kind, params, f"intent:{intent}:{_today()}", skip)
        else:
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
    if not out and skip == 0:
        # misspell rescue, cheapest first (live audit 2026-10-16: TMDB turns
        # away "sprited away", "intersteller", "chines drama" with zero hits).
        # NOTE: the empty strict result must NOT be cached before this ran -
        # a cached [] short-circuits the rescue for the next CACHE_TTL.
        fi = fuzzy_intent(term)
        if fi:
            params = INTENTS.get(fi, (None, None))[0 if kind == "movie" else 1]
            if params and params != "__DYNAMIC__":
                fout = discover_metas(kind, params, f"intent:{fi}", 0)
                if fout:
                    print(f"tmdb-search: fuzzy-intent {term!r} -> {fi} -> {len(fout)}", flush=True)
                    out = fout
        if not out:
            out = _fuzzy_title(kind, term)
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


def _search_term(parts, u):
    """Search term from the path segments / query. Segments arrive percent-
    decoded, so a term containing a literal '&' ("law & order") must be taken
    from its OWN segment whole - splitting it on '&' would truncate it."""
    term = (urllib.parse.parse_qs(u.query).get("search") or [""])[0]
    for seg in parts:
        if seg.startswith("search="):
            term = seg[len("search="):]
            break
        for piece in seg.split("&"):
            if piece.startswith("search="):
                term = piece[len("search="):]
    if term.endswith(".json"):
        term = term[:-5]
    return term.strip()


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
                    term = _search_term(parts[3:], u)
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
                if cid in DYNAMIC_DEFS:
                    today = _today()
                    return self._send({"metas": discover_metas(kind, DYNAMIC_DEFS[cid](today),
                                       f"dyn:{cid}:{today}", skip), "cacheMaxAge": 3600})
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
    assert DYNAMIC_DEFS["new-releases-movies"]("2026-10-16")["primary_release_date.gte"] == "2026-08-21"
    assert match_intent("psychological thriller") == "psych-thriller"
    assert match_intent("dark comedy") == "dark-comedy"
    assert match_intent("superhero movies") == "superhero"
    assert match_intent("80s movies") == "80s"
    assert match_intent("crime drama") == "crime-drama"
    assert match_intent("slice of life") == "anime-sol"
    assert match_intent("slice of life shows") == "anime-sol"
    assert match_intent("anime horror shows") == "anime-horror"
    assert match_intent("documentaries") == "documentaries"
    assert match_intent("sci-fi & fantasy") == "scifi-fantasy"
    assert match_intent("action & adventure") == "action"
    assert match_intent("netflix") == "netflix"
    assert match_intent("netflix shows") == "netflix"
    assert match_intent("new releases") == "new-releases"
    assert match_intent("trending now") == "trending"
    assert match_intent("max") is None  # bare "max" stays a title search
    assert match_intent("disney+") == "disney"
    _u = urllib.parse.urlparse("/catalog/movie/search/search=law%20%26%20order.json")
    _p = [urllib.parse.unquote(x) for x in _u.path.strip("/").split("/")]
    assert _search_term(_p[3:], _u) == "law & order", _search_term(_p[3:], _u)
    _u2 = urllib.parse.urlparse("/catalog/movie/search/search=anime.json")
    _p2 = [urllib.parse.unquote(x) for x in _u2.path.strip("/").split("/")]
    assert _search_term(_p2[3:], _u2) == "anime"
    assert INTENTS["netflix"][0] and INTENTS["netflix"][1]
    assert INTENTS["romantic-comedy"][0] and INTENTS["romantic-comedy"][1]
    # intent sides resolve for both kinds where promised
    for key in ("anime", "korean", "true-crime", "feel-good"):
        mv, ser = INTENTS[key]
        assert mv and ser, key
    assert match_intent("sitcoms") == "sitcoms"
    assert match_intent("based on a true story") == "true-story"
    assert match_intent("time travel movies") == "time-travel"
    assert match_intent("anime mystery") == "anime-mystery"
    assert match_intent("zombies") == "zombie"
    assert match_intent("reality tv shows") == "reality"
    assert match_intent("stand up comedy") == "standup"
    assert match_intent("detective shows") == "detective"
    assert match_intent("kids shows") == "family-kids"
    assert match_intent("anime drama") == "anime-drama"
    assert INTENTS["sitcoms"][1] and INTENTS["reality"][1]
    assert INTENTS["standup"][0] and INTENTS["korean"][1]
    assert match_intent("cartoons") == "animation"
    assert match_intent("cartoons shows") == "animation"
    assert match_intent("scary movies") == "horror"
    assert match_intent("funny") == "comedy"
    assert match_intent("studio ghibli") == "ghibli"
    assert match_intent("ghibli movies") == "ghibli"
    assert match_intent("chinese dramas") == "cdrama"
    assert match_intent("cdrama") == "cdrama"
    assert match_intent("chinese") == "chinese"
    assert match_intent("chinese shows") == "chinese"
    assert INTENTS["chinese"][0] and INTENTS["chinese"][1]
    assert fuzzy_intent("chines drama") == "cdrama"
    assert fuzzy_intent("gibli") == "ghibli"
    assert fuzzy_intent("cartons") == "animation"
    assert len(DISCOVER_CATALOGS) == 136, len(DISCOVER_CATALOGS)
    assert len(MANIFEST["catalogs"]) == 2 + len(DEEP_CATALOGS) + 136 + len(DYNAMIC_CATALOGS)
    ids = [(c["type"], c["id"]) for c in MANIFEST["catalogs"]]
    assert len(ids) == len(set(ids)), "duplicate (type, id) catalog entries"
    print("selftest ok: %d catalogs (%d discover)" % (len(MANIFEST["catalogs"]), len(DISCOVER_CATALOGS)))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
