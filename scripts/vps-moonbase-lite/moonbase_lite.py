#!/usr/bin/env python3
"""moonbase-lite: Moonfin /Moonfin/* shim for AIOStreams media servers.

Serves the subset of the Moonbase plugin surface that Moonfin clients call,
for servers that run AIOStreams instead of Jellyfin+Moonbase. Stdlib only.

Endpoints (all under /Moonfin/):
  GET  Ping                        -> feature flags + fleet default profile
  GET  Defaults                    -> fleet default profile (flat)
  GET  Settings                    -> caller's scoped settings object
  GET  Settings/Resolved/{scope}   -> flat merged profile (defaults<-global<-scope)
  POST Settings/Profile/{scope}    -> merge a flat profile into a scope
  POST Settings                    -> merge a full scoped object (legacy client path)
  DELETE Settings                  -> reset caller's scopes
  DELETE Settings/Profile/{scope}  -> reset one scope
  POST Admin/PushDefaults          -> merge fleet defaults into all user files
  POST Admin/Themes               -> save a theme JSON (admin)
  GET  Themes                      -> array of full theme JSONs
  GET  Themes/{id}                 -> one theme JSON
  GET  Messages                    -> {items:[...]} filtered for the caller
  POST Notifications/Prefs        -> store per-user notification prefs
  GET  Settings/Stream            -> SSE: settingsUpdated / themesChanged + heartbeats
  GET  Web/loader.js, Web/config.json -> web-client stubs
  GET  Tmdb/NextEpisode           -> TMDB next_episode_to_air (needs tmdb.key)
  GET  Tmdb/EpisodeRating         -> TMDB episode vote_average (needs tmdb.key)
  POST/DELETE Notifications/Register -> accepted no-op (no push delivery here)
  GET  Tmdb/ProductionCompanies   -> companies+networks [{id,name,hasLogo}] (needs tmdb.key)
  GET  Tmdb/StudioImage/{id}      -> PNG logo bytes (negative id = TV network)

Auth: the client's Authorization header (or ?ApiKey= query token, for image
widgets that cannot set headers) is validated against the AIOStreams
server's /Users/Me (loopback). Results cached 10 min. Network failure to
AIOStreams = fail open (single-user homelab; nginx fronts this anyway).

State lives in DATA_DIR (default /opt/moonbase-lite/data):
  defaults.json          flat fleet profile
  users/<key>.json       {schemaVersion, global:{}, desktop:{}, mobile:{}, tv:{}}
  messages.json          [ {id,title,body,color,delivery,audience,targetUserIds,
                            actionLabel,actionUrl,startUtc,endUtc,createdUtc} ]
  notifications/<key>.json
  themes/<id>.json       full theme JSONs served to clients
  mdblist-cache.json     upstream cache

# ponytail: single-user/household scale, in-memory SSE registry, file-per-user
# JSON; swap for SQLite if any of that ever grows teeth.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
DEFAULT_CFG = {
    "port": 3002,
    "aios_url": "http://127.0.0.1:3000",
    "mdblist_key": "",
    "server_url": "https://aiostreams.mysweetpea.cc/jellyfin",
    "version": "moonbase-lite-1.0",
    "avatar_dir": "/var/www/aiostreams/avatars",
    "avatar_base": "https://aiostreams.mysweetpea.cc/avatars",
}
CFG = dict(DEFAULT_CFG)
if os.path.exists(CFG_PATH):
    try:
        CFG.update(json.load(open(CFG_PATH)))
    except Exception as exc:  # keep booting on a broken config
        print(f"config.json unreadable ({exc}); using defaults", flush=True)

DATA_DIR = CFG.get("data_dir") or os.path.join(BASE, "data")
for sub in ("users", "notifications", "themes"):
    os.makedirs(os.path.join(DATA_DIR, sub), exist_ok=True)

SCOPES = ("global", "desktop", "mobile", "tv")
SCHEMA_VERSION = 1
_io_lock = threading.Lock()
_subscribers: "list[queue.Queue]" = []
_auth_cache: "dict[str, tuple[float, dict | None]]" = {}


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------
def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _user_key(uid: str) -> str:
    return hashlib.sha1(uid.encode("utf-8")).hexdigest()[:16]


def user_path(uid: str) -> str:
    return os.path.join(DATA_DIR, "users", _user_key(uid) + ".json")


def load_user(uid: str) -> dict:
    doc = _read_json(user_path(uid), None)
    if not isinstance(doc, dict):
        doc = {"schemaVersion": SCHEMA_VERSION}
    for scope in SCOPES:
        if not isinstance(doc.get(scope), dict):
            doc[scope] = {}
    return doc


def save_user(uid: str, doc: dict):
    doc["schemaVersion"] = SCHEMA_VERSION
    doc["lastUpdated"] = int(time.time() * 1000)
    with _io_lock:
        _write_json(user_path(uid), doc)


def merge_flat(dst: dict, src: dict) -> dict:
    # null means "inherit", so nulls never overwrite (matches Moonbase merge).
    for k, v in (src or {}).items():
        if v is not None:
            dst[k] = v
    return dst


def load_defaults() -> dict:
    d = _read_json(os.path.join(DATA_DIR, "defaults.json"), {})
    return d if isinstance(d, dict) else {}


# Fleet-wide policy pins: merged LAST, on top of defaults + user scopes, so a
# device profile push can never revert them for anyone (owner directive
# 2026-10-14: episode rows show the episode's own still/frame, not series art;
# recommendations come from the server). Mirror of these values lives in
# seed-defaults.py — keep both in sync.
PINS = {
    # Episodes show their own Primary image (a frame from that episode).
    "homeImageUseSeriesImage": False,
    # CW row must be thumb-type or the client substitutes series art for episodes.
    "homeImageTypeContinueWatching": "thumb",
    # "Since you watched" recs from the server's /Items/{id}/Similar (verified on aios).
    "sinceYouWatchedSource": "server",
    "sinceYouWatchedNumRows": 5,
    "sinceYouWatched4Enabled": True,
    "sinceYouWatched5Enabled": True,
    # Fleet row list (push-proof): devices re-push their own homeSections which would
    # otherwise shadow defaults.json and disable rows 4/5 (seen 2026-10-15). Order: all personalized rows first
    # (resume..playlists), then browse rows (genres / latestmedia / collections).
    # Owner request 2026-10-15.
    "homeSections": [{'type': 'resume', 'enabled': True, 'order': 1}, {'type': 'nextup', 'enabled': True, 'order': 2}, {'type': 'sinceyouwatched1', 'enabled': True, 'order': 3}, {'type': 'sinceyouwatched2', 'enabled': True, 'order': 4}, {'type': 'sinceyouwatched3', 'enabled': True, 'order': 5}, {'type': 'sinceyouwatched4', 'enabled': True, 'order': 6}, {'type': 'sinceyouwatched5', 'enabled': True, 'order': 7}, {'type': 'rewatch', 'enabled': True, 'order': 8}, {'type': 'genres', 'enabled': True, 'order': 12}, {'type': 'latestmedia', 'enabled': True, 'order': 13}, {'type': 'collections', 'enabled': True, 'order': 14}, {'type': 'favoritemovies', 'enabled': True, 'order': 9}, {'type': 'favoriteepisodes', 'enabled': True, 'order': 10}, {'type': 'playlists', 'enabled': True, 'order': 11}, {'type': 'mediabar', 'enabled': False, 'order': 15}, {'type': 'recentlyreleased', 'enabled': False, 'order': 16}, {'type': 'smalllibrarytiles', 'enabled': False, 'order': 17}, {'type': 'librarybuttons', 'enabled': False, 'order': 18}, {'type': 'resumeaudio', 'enabled': False, 'order': 19}, {'type': 'resumebook', 'enabled': False, 'order': 20}, {'type': 'activerecordings', 'enabled': False, 'order': 21}, {'type': 'audioartists', 'enabled': False, 'order': 22}, {'type': 'audioalbums', 'enabled': False, 'order': 23}, {'type': 'audioplaylists', 'enabled': False, 'order': 24}, {'type': 'favoriteseries', 'enabled': False, 'order': 25}, {'type': 'favoritepeople', 'enabled': False, 'order': 26}, {'type': 'favoriteartists', 'enabled': False, 'order': 27}, {'type': 'favoritemusicvideos', 'enabled': False, 'order': 28}, {'type': 'favoritealbums', 'enabled': False, 'order': 29}, {'type': 'favoritesongs', 'enabled': False, 'order': 30}, {'type': 'studios', 'enabled': False, 'order': 31}, {'type': 'livetv', 'enabled': False, 'order': 32}, {'type': 'seerr_shortcuts', 'enabled': False, 'order': 33}, {'type': 'seerr_recent_requests', 'enabled': False, 'order': 34}, {'type': 'seerr_watchlist', 'enabled': False, 'order': 35}, {'type': 'seerr_recently_added', 'enabled': False, 'order': 36}, {'type': 'seerr_popular_movies', 'enabled': False, 'order': 37}, {'type': 'seerr_upcoming_movies', 'enabled': False, 'order': 38}, {'type': 'seerr_popular_series', 'enabled': False, 'order': 39}, {'type': 'seerr_upcoming_series', 'enabled': False, 'order': 40}, {'type': 'seerr_trending', 'enabled': False, 'order': 41}, {'type': 'seerr_movie_genres', 'enabled': False, 'order': 42}, {'type': 'seerr_studios', 'enabled': False, 'order': 43}, {'type': 'seerr_series_genres', 'enabled': False, 'order': 44}, {'type': 'seerr_networks', 'enabled': False, 'order': 45}, {'type': 'imdb_top_250_movies', 'enabled': False, 'order': 46}, {'type': 'imdb_top_250_tv_shows', 'enabled': False, 'order': 47}, {'type': 'imdb_most_popular_movies', 'enabled': False, 'order': 48}, {'type': 'imdb_most_popular_tv_shows', 'enabled': False, 'order': 49}, {'type': 'imdb_lowest_rated_movies', 'enabled': False, 'order': 50}, {'type': 'imdb_top_english_movies', 'enabled': False, 'order': 51}, {'type': 'tmdb_popular_movies', 'enabled': False, 'order': 52}, {'type': 'tmdb_top_rated_movies', 'enabled': False, 'order': 53}, {'type': 'tmdb_now_playing_movies', 'enabled': False, 'order': 54}, {'type': 'tmdb_upcoming_movies', 'enabled': False, 'order': 55}, {'type': 'tmdb_popular_tv', 'enabled': False, 'order': 56}, {'type': 'tmdb_top_rated_tv', 'enabled': False, 'order': 57}, {'type': 'tmdb_airing_today_tv', 'enabled': False, 'order': 58}, {'type': 'tmdb_on_the_air_tv', 'enabled': False, 'order': 59}, {'type': 'tmdb_trending_movie_daily', 'enabled': False, 'order': 60}, {'type': 'tmdb_trending_movie_weekly', 'enabled': False, 'order': 61}, {'type': 'tmdb_trending_tv_daily', 'enabled': False, 'order': 62}, {'type': 'tmdb_trending_tv_weekly', 'enabled': False, 'order': 63}, {'type': 'tmdb_trending_all_weekly', 'enabled': False, 'order': 64}, {'type': 'radarr_calendar', 'enabled': False, 'order': 65}, {'type': 'sonarr_calendar', 'enabled': False, 'order': 66}, {'type': 'seasonal', 'enabled': False, 'order': 67}],
    # Classic Moonfin media bar style instead of the Aya style (owner directive).
    "mediaBarMode": "moonfin",
}


def resolve_profile(uid: str, scope: str) -> dict:
    out: dict = {}
    merge_flat(out, load_defaults())
    doc = load_user(uid)
    merge_flat(out, doc.get("global", {}))
    if scope in ("desktop", "mobile", "tv"):
        merge_flat(out, doc.get(scope, {}))
    merge_flat(out, PINS)
    return out


def load_messages() -> list:
    m = _read_json(os.path.join(DATA_DIR, "messages.json"), [])
    return m if isinstance(m, list) else []


# --------------------------------------------------------------------------
# auth: validate caller token against AIOStreams /Users/Me
# --------------------------------------------------------------------------
def _http_get_json(url: str, headers: dict, timeout: int = 8):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def whoami(auth_header: str):
    """-> (user_dict|None, ok). ok=False only on definitive rejection."""
    if not auth_header:
        return None, False
    now = time.time()
    hit = _auth_cache.get(auth_header)
    if hit and now - hit[0] < 600:
        return hit[1], hit[1] is not None
    url = CFG["aios_url"].rstrip("/") + "/jellyfin/Users/Me"
    try:
        data = _http_get_json(url, {"Authorization": auth_header})
        user = {"id": str(data.get("Id") or data.get("id") or ""),
                "name": data.get("Name") or data.get("name") or "",
                "persona": bool(data.get("Persona") or data.get("persona"))}
        _auth_cache[auth_header] = (now, user)
        return user, True
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            _auth_cache[auth_header] = (now, None)
            return None, False
        print(f"whoami upstream {exc.code}; failing open", flush=True)
        return None, True
    except ValueError:
        # HTML/undecodable body = misrouted request, not an outage: fail closed.
        _auth_cache[auth_header] = (now, None)
        return None, False
    except Exception as exc:
        print(f"whoami unreachable ({exc}); failing open", flush=True)
        return None, True


# --------------------------------------------------------------------------
# SSE broadcast
# --------------------------------------------------------------------------
def broadcast(event: dict):
    payload = "data: " + json.dumps(event) + "\n\n"
    with _io_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except Exception:
            pass


# --------------------------------------------------------------------------
# mdblist
# --------------------------------------------------------------------------
def tmdb_key():
    """Server-side TMDB v3 key: /opt/moonbase-lite/tmdb.key (read per request so
    it can be dropped in without a restart), else config.json tmdb_key."""
    try:
        with open(os.path.join(BASE, "tmdb.key"), "r") as fh:
            return fh.read().strip()
    except OSError:
        return CFG.get("tmdb_key") or ""


_TMDB_NEXT_CACHE = {}
_TMDB_NEXT_TTL = 6 * 3600  # matches the Moonbase plugin's NextEpisode cache


def tmdb_next_episode(tmdb_id: str, key: str) -> dict:
    hit = _TMDB_NEXT_CACHE.get(tmdb_id)
    now = time.time()
    if hit and now - hit[0] < _TMDB_NEXT_TTL:
        return hit[1]
    url = ("https://api.themoviedb.org/3/tv/%s?api_key=%s"
           % (urllib.parse.quote(tmdb_id, safe=""), urllib.parse.quote(key, safe="")))
    try:
        data = _http_get_json(url, {"User-Agent": "Moonfin/1.0"}, timeout=8)
        nxt = data.get("next_episode_to_air") or None
        if nxt:
            result = {"success": True, "airDate": nxt.get("air_date"),
                      "seasonNumber": nxt.get("season_number"),
                      "episodeNumber": nxt.get("episode_number"),
                      "name": nxt.get("name")}
        else:
            result = {"success": False, "error": "No upcoming episode"}
    except Exception as exc:
        result = {"success": False, "error": f"TMDB fetch failed: {exc}"}
    _TMDB_NEXT_CACHE[tmdb_id] = (now, result)
    return result


_TMDB_EP_CACHE = {}
_TMDB_EP_TTL = 6 * 3600  # same freshness class as NextEpisode


def tmdb_episode_rating(tmdb_id: str, season: int, episode: int, key: str) -> dict:
    ck = "%s:%d:%d" % (tmdb_id, season, episode)
    hit = _TMDB_EP_CACHE.get(ck)
    now = time.time()
    if hit and now - hit[0] < _TMDB_EP_TTL:
        return hit[1]
    url = ("https://api.themoviedb.org/3/tv/%s/season/%d/episode/%d?api_key=%s"
           % (urllib.parse.quote(tmdb_id, safe=""), season, episode,
              urllib.parse.quote(key, safe="")))
    try:
        data = _http_get_json(url, {"User-Agent": "Moonfin/1.0"}, timeout=8)
        result = {"success": True,
                  "voteAverage": data.get("vote_average"),
                  "voteCount": data.get("vote_count"),
                  "name": data.get("name"),
                  "airDate": data.get("air_date"),
                  "seasonNumber": data.get("season_number"),
                  "episodeNumber": data.get("episode_number"),
                  "stillPath": data.get("still_path")}
    except Exception as exc:
        result = {"success": False, "error": f"TMDB fetch failed: {exc}"}
    _TMDB_EP_CACHE[ck] = (now, result)
    return result


_TMDB_CO_CACHE = {}
_TMDB_CO_TTL = 6 * 3600
_TMDB_LOGO_CACHE = {}  # companyId -> (ts, bytes|None); logos are tiny
# ponytail: in-memory logo cache; a restart just re-fetches once per company.


def tmdb_production_companies(tmdb_id: str, mtype: str, key: str) -> dict:
    ck = "%s:%s" % (mtype, tmdb_id)
    hit = _TMDB_CO_CACHE.get(ck)
    now = time.time()
    if hit and now - hit[0] < _TMDB_CO_TTL:
        return hit[1]
    url = ("https://api.themoviedb.org/3/%s/%s?api_key=%s"
           % (mtype, urllib.parse.quote(tmdb_id, safe=""),
              urllib.parse.quote(key, safe="")))
    try:
        data = _http_get_json(url, {"User-Agent": "Moonfin/1.0"}, timeout=8)
        companies = []
        for c in (data.get("production_companies") or []):
            if c.get("id") and (c.get("name") or "").strip():
                companies.append({"id": c["id"], "name": c["name"],
                                  "hasLogo": bool(c.get("logo_path"))})
        for n in (data.get("networks") or []):  # networks ride along (C# uses -id)
            if n.get("id") and (n.get("name") or "").strip():
                companies.append({"id": -n["id"], "name": n["name"],
                                  "hasLogo": bool(n.get("logo_path"))})
        result = {"success": True, "companies": companies}
    except Exception as exc:
        result = {"success": False, "error": f"TMDB fetch failed: {exc}"}
    _TMDB_CO_CACHE[ck] = (now, result)
    return result


def tmdb_studio_image(company_id: int, key: str):
    """-> PNG bytes or None. Resolves the logo via TMDB /images (stateless; the
    C# plugin instead carries logo_path from the title's detail response)."""
    now = time.time()
    hit = _TMDB_LOGO_CACHE.get(company_id)
    if hit and now - hit[0] < (12 * 3600 if hit[1] else 3600):
        return hit[1]
    real = -company_id if company_id < 0 else company_id
    kind = "network" if company_id < 0 else "company"
    payload = None
    try:
        data = _http_get_json(
            "https://api.themoviedb.org/3/%s/%d/images?api_key=%s"
            % (kind, real, urllib.parse.quote(key, safe="")),
            {"User-Agent": "Moonfin/1.0"}, timeout=8)
        logos = [l for l in (data.get("logos") or [])
                 if str(l.get("file_path") or "").lower().endswith(".png")]
        logos.sort(key=lambda l: l.get("iso_639_1") is not None)  # neutral first
        if logos:
            req = urllib.request.Request(
                "https://image.tmdb.org/t/p/w500" + logos[0]["file_path"],
                headers={"User-Agent": "Moonfin/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
            if body:
                payload = body
    except Exception as exc:
        print(f"studio image fetch failed for {company_id}: {exc}", flush=True)
    _TMDB_LOGO_CACHE[company_id] = (now, payload)
    return payload


def mdblist_ratings(mtype: str, tmdb_id: str, api_key: str):
    cache_path = os.path.join(DATA_DIR, "mdblist-cache.json")
    cache = _read_json(cache_path, {})
    ck = f"{mtype}:{tmdb_id}"
    hit = cache.get(ck)
    if hit and time.time() - hit.get("ts", 0) < (21600 if hit.get("ok") else 1800):
        return hit.get("ratings", []), None
    url = ("https://api.mdblist.com/tmdb/%s/%s/?apikey=%s"
           % (urllib.parse.quote(mtype), urllib.parse.quote(tmdb_id),
              urllib.parse.quote(api_key)))
    try:
        data = _http_get_json(url, {"User-Agent": "Moonfin/1.0"}, timeout=15)
        ratings = data.get("ratings") or []
        cache[ck] = {"ts": time.time(), "ok": True, "ratings": ratings}
        _write_json(cache_path, cache)
        return ratings, None
    except Exception as exc:
        cache[ck] = {"ts": time.time(), "ok": False, "ratings": []}
        try:
            _write_json(cache_path, cache)
        except Exception:
            pass
        return [], f"mdblist fetch failed: {exc}"


# --------------------------------------------------------------------------
# member avatars: deterministic identicon PNGs (stdlib only, no fonts)
# --------------------------------------------------------------------------
AVATAR_PALETTE = [
    (139, 126, 246), (69, 214, 200), (122, 160, 215), (168, 140, 235),
    (92, 190, 160), (215, 170, 110), (170, 150, 240), (100, 205, 190),
]


def _png_rgba(w: int, h: int, px: bytearray) -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes(px[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def make_avatar_png(slug: str, display: str) -> str | None:
    """Deterministic 5x5 identicon per member -> public URL, or None. Never raises."""
    try:
        d = str(CFG.get("avatar_dir") or "")
        base = str(CFG.get("avatar_base") or "").rstrip("/")
        if not d or not base:
            return None
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, slug + ".png")
        if not os.path.exists(path):
            h = hashlib.sha256(("msp:" + (slug or display or "x")).encode()).digest()
            color = bytes(AVATAR_PALETTE[h[0] % len(AVATAR_PALETTE)]) + b"\xff"
            bg = bytes((12, 15, 20, 255))
            size, slot, gap, pad = 256, 44, 3, 18
            img = bytearray(size * size * 4)
            for i in range(0, len(img), 4):
                img[i:i + 4] = bg
            bits = int.from_bytes(h[1:4], "big")
            for gy in range(5):
                for gx in range(5):
                    col = gx if gx < 3 else 4 - gx
                    if (bits >> (gy * 3 + col)) & 1:
                        x0 = pad + gx * slot + gap
                        y0 = pad + gy * slot + gap
                        for yy in range(y0, y0 + slot - 2 * gap):
                            rowbase = (yy * size + x0) * 4
                            for xx in range(slot - 2 * gap):
                                j = rowbase + xx * 4
                                img[j:j + 4] = color
            data = _png_rgba(size, size, img)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        return base + "/" + slug + ".png"
    except Exception as exc:  # cosmetic; provisioning must never break on it
        print(f"avatar generation failed for {slug}: {exc}", flush=True)
        return None


def remove_avatar(slug: str) -> None:
    try:
        d = str(CFG.get("avatar_dir") or "")
        if d:
            os.remove(os.path.join(d, slug + ".png"))
    except OSError:
        pass


# --------------------------------------------------------------------------
# AIOStreams config API: personas
# --------------------------------------------------------------------------
def _aios_config_auth():
    import base64 as _b64
    uuid = CFG.get("config_uuid")
    pw = CFG.get("config_password")
    if not uuid or not pw:
        return None
    return "Basic " + _b64.b64encode(f"{uuid}:{pw}".encode()).decode()


def aios_persona_op(pid: str, name: str, remove: bool, _valid_pin: str = None):
    """Add or remove a persona in the AIOStreams user config. -> (ok, dict)"""
    auth = _aios_config_auth()
    if not auth:
        return False, "config credentials not configured"
    base = CFG["aios_url"].rstrip("/")
    hdrs = {"Authorization": auth, "User-Agent": "moonbase-lite/1.0"}
    try:
        req = urllib.request.Request(base + "/api/v1/user", headers=hdrs)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        root = data.get("data") if isinstance(data, dict) else None
        user_data = None
        if isinstance(root, dict):
            user_data = root.get("userData") or root.get("config")
        if user_data is None and isinstance(data, dict):
            user_data = data.get("userData") or data.get("config")
        if not isinstance(user_data, dict):
            return False, "unexpected config response shape"
        jf = user_data.setdefault("jellyfin", {})
        personas = jf.setdefault("personas", [])
        if not isinstance(personas, list):
            return False, "personas not a list"
        existing = next((p for p in personas if isinstance(p, dict) and p.get("id") == pid), None)
        if remove:
            if existing is None:
                return False, "persona not found"
            personas.remove(existing)
            remove_avatar(pid)
            pin = None
        else:
            if existing is not None:
                if not existing.get("avatar"):
                    avatar_url = make_avatar_png(pid, name or pid)
                    if not avatar_url:
                        return True, {"success": True, "exists": True, "persona": pid, "pin": None}
                    existing["avatar"] = avatar_url
                else:
                    return True, {"success": True, "exists": True, "persona": pid, "pin": None}
            pin = _valid_pin or "".join(secrets.choice("0123456789") for _ in range(6))
            entry = {"id": pid, "name": (name or pid)[:32], "history": "own", "lock": pin}
            avatar_url = make_avatar_png(pid, name or pid)
            if avatar_url:
                entry["avatar"] = avatar_url
            personas.append(entry)
        payload = json.dumps({"config": user_data}).encode("utf-8")
        last_exc = None
        for attempt in (1, 2):  # AIOStreams API bursts to 429; one retry covers household signup storms
            req = urllib.request.Request(base + "/api/v1/user", data=payload, method="PUT",
                                         headers={**hdrs, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    resp.read()
                last_exc = None
                break
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code == 429:
                    time.sleep(5)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        return True, {"success": True, "persona": pid, "pin": pin}
    except Exception as exc:
        return False, f"config api failed: {exc}"


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "moonbase-lite"

    def log_message(self, fmt, *args):  # quieter logs, still useful
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- plumbing ----------------------------------------------------------
    def _send(self, code: int, obj, ctype="application/json", cache="no-store"):
        body = obj if isinstance(obj, bytes) else json.dumps(
            obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 2_000_000:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _caller(self):
        auth = self.headers.get("Authorization", "")
        if not auth:
            # image widgets cannot set headers; clients append the token in the query
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            tok = (q.get("ApiKey") or q.get("api_key") or [""])[0].strip()
            if tok:
                auth = 'MediaBrowser Token="%s"' % tok
        user, ok = whoami(auth)
        if not ok:
            self._send(401, {"error": "A valid user token is required"})
            return None, False
        uid = (user or {}).get("id") or "anonymous"
        is_admin = not (user or {}).get("persona")
        return {"id": uid, "admin": is_admin, "user": user}, True

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        if not path.startswith("/Moonfin"):
            self._send(404, {"error": "not found"})
            return
        rel = path[len("/Moonfin"):] or "/"

        # ---- unauthenticated-ish web stubs ---------------------------------
        if rel == "/Web/loader.js":
            self._send(200, b"/* moonbase-lite: no web runtime customization */\n",
                       ctype="application/javascript")
            return
        if rel == "/Web/config.json":
            self._send(200, {"defaultServerUrl": CFG["server_url"],
                             "forcedServerUrl": None})
            return

        # token-authenticated provisioning (called by n8n during account creation)
        if rel == "/Admin/Provision":
            self._provision(method)
            return

        caller, ok = self._caller()
        if not ok:
            return
        uid = caller["id"]

        try:
            self._dispatch(method, rel, query, caller)
        except Exception as exc:  # never 500-with-nothing; clients degrade anyway
            import traceback
            traceback.print_exc()
            self._send(500, {"error": f"shim error: {exc}"})

    def _dispatch(self, method, rel, query, caller):
        uid = caller["id"]

        # ---- ping / defaults ----------------------------------------------
        if method == "GET" and rel == "/Ping":
            defaults = load_defaults()
            resp = {
                "installed": True,
                "version": CFG["version"],
                "settingsSyncEnabled": True,
                "serverName": "MySweetPea",
                "seerrEnabled": False,
                "seerrUrl": None,
                "jellyseerrEnabled": False,
                "jellyseerrUrl": None,
                "mdblistAvailable": bool(CFG.get("mdblist_key")),
                "tmdbAvailable": False,
                "tmdbThumbsSupported": False,
                "messagesSupported": True,
                "recommendationsSupported": False,
                "defaultSettings": defaults or None,
            }
            self._send(200, resp)
            return

        if method == "GET" and rel == "/Defaults":
            self._send(200, load_defaults())
            return

        # ---- graceful stubs: no AIOStreams-side backing --------------------
        # Same "feature off" shapes the Moonbase plugin returns when a feature
        # is disabled; stops client error logs and probe loops on this server.
        if method == "GET" and rel == "/Games/Libraries":
            self._send(200, [])
            return
        if method == "GET" and rel == "/Seerr/Status":
            self._send(200, {"enabled": False, "authenticated": False, "url": None})
            return
        if method == "GET" and rel == "/Seerr/Config":
            self._send(200, {"enabled": False, "url": None, "displayName": "Seerr",
                             "variant": "seerr", "userEnabled": True})
            return
        if method == "GET" and rel == "/AnimeMarkers/Items":
            self._send(200, {"enabled": False, "items": {}})
            return
        if method == "GET" and rel == "/AnimeMarkers/Series":
            self._send(200, {"enabled": False, "matched": False,
                             "episodes": {}, "seasons": {}})
            return

        # ---- tmdb: next episode to air (needs tmdb.key or config tmdb_key) ---
        if method == "GET" and rel == "/Tmdb/NextEpisode":
            tmdb_id = (query.get("tmdbId") or [""])[0].strip()
            if not tmdb_id:
                self._send(400, {"error": "tmdbId required"})
                return
            resolved = resolve_profile(uid, "mobile")
            key = (resolved.get("tmdbApiKey") or tmdb_key() or "").strip()
            if not key:
                self._send(200, {"success": False,
                                 "error": "No TMDB API key configured on the server."})
                return
            self._send(200, tmdb_next_episode(tmdb_id, key))
            return

        # ---- tmdb: per-episode rating (mirrors Moonbase Tmdb/EpisodeRating) --
        if method == "GET" and rel == "/Tmdb/EpisodeRating":
            tmdb_id = (query.get("tmdbId") or [""])[0].strip()
            season = (query.get("season") or [""])[0].strip()
            episode = (query.get("episode") or [""])[0].strip()
            if not (tmdb_id and season.isdigit() and episode.isdigit()):
                self._send(400, {"error": "tmdbId, season and episode required"})
                return
            resolved = resolve_profile(uid, "mobile")
            key = (resolved.get("tmdbApiKey") or tmdb_key() or "").strip()
            if not key:
                self._send(200, {"success": False,
                                 "error": "No TMDB API key configured on the server."})
                return
            self._send(200, tmdb_episode_rating(tmdb_id, int(season), int(episode), key))
            return

        # ---- tmdb: production companies + studio logo images ---------------
        if method == "GET" and rel == "/Tmdb/ProductionCompanies":
            tmdb_id = (query.get("tmdbId") or [""])[0].strip()
            mtype = ("tv" if (query.get("type") or [""])[0].strip().lower() == "tv"
                     else "movie")
            if not tmdb_id:
                self._send(400, {"error": "tmdbId required"})
                return
            resolved = resolve_profile(uid, "mobile")
            key = (resolved.get("tmdbApiKey") or tmdb_key() or "").strip()
            if not key:
                self._send(200, {"success": False,
                                 "error": "No TMDB API key configured on the server."})
                return
            self._send(200, tmdb_production_companies(tmdb_id, mtype, key))
            return

        if method == "GET" and rel.startswith("/Tmdb/StudioImage/"):
            try:
                company_id = int(rel.split("/", 3)[-1])
            except ValueError:
                self._send(400, {"error": "companyId required"})
                return
            resolved = resolve_profile(uid, "mobile")
            key = (resolved.get("tmdbApiKey") or tmdb_key() or "").strip()
            logo = tmdb_studio_image(company_id, key) if key else None
            if logo:
                self._send(200, logo, ctype="image/png",
                           cache="public, max-age=31536000, immutable")
            else:
                self._send(404, {"error": "no logo"})
            return

        # ---- settings -------------------------------------------------------
        if rel == "/Settings/Stream" and method == "GET":
            self._sse()
            return

        if rel == "/Settings" and method == "GET":
            self._send(200, load_user(uid))
            return

        if rel in ("/Settings", "/settings") and method == "POST":
            body = self._read_body()
            payload = body.get("settings") or body.get("Settings") or body
            doc = load_user(uid)
            touched = []
            for key in list(payload.keys()):
                low = key.lower()
                if low in SCOPES and isinstance(payload[key], dict):
                    merge_flat(doc[low], payload[key])
                    touched.append(low)
                elif low in ("hiddencontentbaselines", "hiddenContentBaselines".lower(),
                             "syncenabled", "syncEnabled".lower()):
                    doc[key if low in SCOPES else low] = payload[key]
            save_user(uid, doc)
            broadcast({"type": "settingsUpdated", "userId": uid,
                       "scopes": touched or ["global"]})
            self._send(200, {"success": True})
            return

        if rel in ("/Settings", "/settings") and method == "DELETE":
            doc = {"schemaVersion": SCHEMA_VERSION}
            for scope in SCOPES:
                doc[scope] = {}
            save_user(uid, doc)
            broadcast({"type": "settingsUpdated", "userId": uid, "scopes": list(SCOPES)})
            self._send(200, {"success": True})
            return

        if rel.startswith("/Settings/Resolved/") and method == "GET":
            scope = rel.rsplit("/", 1)[-1].lower()
            if scope not in ("global", "desktop", "mobile", "tv"):
                self._send(404, {"error": "unknown scope"})
                return
            resolved = resolve_profile(uid, scope)
            if not resolved and not load_defaults():
                self._send(404, {"error": "No settings found"})
                return
            self._send(200, resolved)
            return

        if rel.startswith("/Settings/Profile/") and method == "POST":
            scope = rel.rsplit("/", 1)[-1].lower()
            if scope not in ("global", "desktop", "mobile", "tv"):
                self._send(404, {"error": "unknown scope"})
                return
            body = self._read_body()
            flat = body.get("profile") or body.get("Profile") or {}
            if not isinstance(flat, dict):
                self._send(400, {"error": "profile must be an object"})
                return
            doc = load_user(uid)
            merge_flat(doc[scope], flat)
            save_user(uid, doc)
            broadcast({"type": "settingsUpdated", "userId": uid, "scopes": [scope]})
            self._send(200, {"success": True, "profile": scope})
            return

        if rel.startswith("/Settings/Profile/") and method == "DELETE":
            scope = rel.rsplit("/", 1)[-1].lower()
            if scope not in ("global", "desktop", "mobile", "tv"):
                self._send(404, {"error": "unknown scope"})
                return
            doc = load_user(uid)
            doc[scope] = {}
            save_user(uid, doc)
            broadcast({"type": "settingsUpdated", "userId": uid, "scopes": [scope]})
            self._send(200, {"success": True})
            return

        # admin: push defaults into every user file
        if rel == "/Admin/PushDefaults" and method == "POST":
            if not caller["admin"]:
                self._send(403, {"error": "admin required"})
                return
            body = self._read_body()
            overwrite = bool(body.get("overwrite"))
            defaults = load_defaults()
            users_dir = os.path.join(DATA_DIR, "users")
            count = 0
            for name in os.listdir(users_dir):
                if not name.endswith(".json"):
                    continue
                fp = os.path.join(users_dir, name)
                doc = _read_json(fp, None)
                if not isinstance(doc, dict):
                    continue
                for scope in SCOPES:
                    if overwrite or not doc.get(scope):
                        doc[scope] = dict(defaults) if overwrite else merge_flat(
                            doc.get(scope) or {}, defaults)
                    else:
                        merge_flat(doc[scope], defaults)
                _write_json(fp, doc)
                count += 1
            broadcast({"type": "settingsUpdated", "scopes": list(SCOPES)})
            self._send(200, {"success": True, "users": count})
            return

        # ---- themes ----------------------------------------------------------
        if rel == "/Themes" and method == "GET":
            themes = []
            tf = os.path.join(DATA_DIR, "themes")
            for name in sorted(os.listdir(tf)):
                if name.endswith(".json"):
                    t = _read_json(os.path.join(tf, name), None)
                    if isinstance(t, dict):
                        themes.append(t)
            self._send(200, themes)
            return

        if rel.startswith("/Themes/") and method == "GET":
            tid = rel.split("/", 2)[-1]
            t = _read_json(os.path.join(DATA_DIR, "themes", tid + ".json"), None)
            if t is None:
                self._send(404, {"error": "theme not found"})
                return
            self._send(200, t)
            return

        if rel == "/Admin/Themes" and method == "POST":
            if not caller["admin"]:
                self._send(403, {"error": "admin required"})
                return
            theme = self._read_body()
            tid = str(theme.get("id") or "").strip()
            if not tid:
                self._send(400, {"error": "theme id required"})
                return
            _write_json(os.path.join(DATA_DIR, "themes", tid + ".json"), theme)
            broadcast({"type": "themesChanged"})
            self._send(200, {"success": True, "id": tid})
            return

        # ---- messages --------------------------------------------------------
        if rel == "/Messages" and method == "GET":
            now_ms = time.time() * 1000
            is_admin = caller["admin"]
            items = []
            for m in load_messages():
                start = m.get("startUtc")
                end = m.get("endUtc")
                if start and _parse_ms(start) > now_ms:
                    continue
                if end and _parse_ms(end) < now_ms:
                    continue
                aud = (m.get("audience") or "all").lower()
                if aud == "admins" and not is_admin:
                    continue
                if aud == "users":
                    targets = m.get("targetUserIds") or []
                    if uid not in targets and not is_admin:
                        continue
                items.append({k: v for k, v in m.items() if k != "targetUserIds"})
            self._send(200, {"items": items})
            return

        # ---- notification prefs ----------------------------------------------
        if rel == "/Notifications/Prefs":
            prefs_path = os.path.join(DATA_DIR, "notifications", _user_key(uid) + ".json")
            if method == "POST":
                prefs = self._read_body()
                with _io_lock:
                    _write_json(prefs_path, prefs)
                self._send(200, {"success": True})
                return
            if method == "GET":
                self._send(200, _read_json(prefs_path, {}))
                return

        # ---- push registration: accepted no-op (no delivery path here) ------
        if rel == "/Notifications/Register" and method in ("POST", "DELETE"):
            _ = self._read_body()  # drain so keep-alive stays in sync
            self._send(200, {"success": True})
            return

        # ---- mdblist -----------------------------------------------------------
        if rel == "/MdbList/Ratings" and method == "GET":
            mtype = (query.get("type") or ["movie"])[0]
            tmdb_id = (query.get("tmdbId") or [""])[0]
            profile = (query.get("profile") or ["mobile"])[0].lower()
            if not tmdb_id:
                self._send(400, {"error": "tmdbId required"})
                return
            resolved = resolve_profile(uid, profile if profile in SCOPES else "mobile")
            key = resolved.get("mdblistApiKey") or CFG.get("mdblist_key") or ""
            if not key:
                self._send(200, {"success": False, "error": "MDBList not configured"})
                return
            ratings, err = mdblist_ratings(mtype, tmdb_id, key)
            want = resolved.get("mdblistRatingSources")
            if isinstance(want, list) and want:
                wanted = {str(s).lower() for s in want}
                ratings = [r for r in ratings
                           if str(r.get("source", "")).lower() in wanted]
            if err:
                self._send(200, {"success": False, "error": err})
                return
            self._send(200, {"success": True, "ratings": ratings})
            return

        self._send(404, {"error": "not found"})

    # -- provisioning ----------------------------------------------------------
    def _provision(self, method):
        if method != "POST":
            self._send(405, {"error": "POST only"})
            return
        want = str(CFG.get("admin_token") or "")
        got = self.headers.get("X-Admin-Token", "")
        if not want or not secrets.compare_digest(got, want):
            self._send(403, {"error": "admin token required"})
            return
        body = self._read_body()
        username = str(body.get("username") or "").strip().lower()
        name = str(body.get("name") or username).strip()
        remove = bool(body.get("remove"))
        pid = re.sub(r"[^a-z0-9_-]", "-", username).strip("-")[:32]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", pid or ""):
            self._send(400, {"error": "invalid username"})
            return
        pin = str(body.get("pin") or "").strip()
        if pin and not re.fullmatch(r"[0-9]{4,12}", pin):
            self._send(400, {"error": "pin must be 4-12 digits"})
            return
        tier = str(body.get("tier") or "").strip().lower()
        allowed = [str(t).strip().lower() for t in CFG.get("provision_tiers", ["sweetpea"])]
        if not remove and tier and tier not in allowed:
            self._send(200, {"success": True, "skipped": "tier", "tier": tier})
            return
        ok, result = aios_persona_op(pid, name, remove, pin or None)
        self._send(200 if ok else 502, result if isinstance(result, dict) else {"error": result})

    # -- SSE -----------------------------------------------------------------
    def _sse(self):
        q: queue.Queue = queue.Queue()
        with _io_lock:
            _subscribers.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b'data: {"type":"connected"}\n\n')
            self.wfile.flush()
            # Bounded hold (~22s), then a clean close; clients reconnect on
            # finish (same cadence the Moonbase plugin produces on Jellyfin).
            # Client stream transports that cap a connection below their 30s
            # receive timeout get a clean end-of-stream instead of timeout
            # errors when the server closes first.
            deadline = time.time() + 22
            while time.time() < deadline:
                try:
                    frame = q.get(timeout=max(0.1, min(5.0, deadline - time.time())))
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _io_lock:
                if q in _subscribers:
                    _subscribers.remove(q)


def _parse_ms(value) -> float:
    """Accept epoch-ms numbers or ISO-8601 strings."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp() * 1000
    except Exception:
        return 0.0


def main():
    addr = ("127.0.0.1", int(CFG["port"]))
    srv = ThreadingHTTPServer(addr, Handler)
    print(f"moonbase-lite listening on {addr[0]}:{addr[1]} (data={DATA_DIR})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
