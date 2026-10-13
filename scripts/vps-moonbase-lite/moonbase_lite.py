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

Auth: the client's Authorization header is validated against the AIOStreams
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


def resolve_profile(uid: str, scope: str) -> dict:
    out: dict = {}
    merge_flat(out, load_defaults())
    doc = load_user(uid)
    merge_flat(out, doc.get("global", {}))
    if scope in ("desktop", "mobile", "tv"):
        merge_flat(out, doc.get(scope, {}))
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
    def _send(self, code: int, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(
            obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        user, ok = whoami(self.headers.get("Authorization", ""))
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
            last_hb = time.time()
            while True:
                try:
                    frame = q.get(timeout=5)
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    if time.time() - last_hb >= 20:
                        self.wfile.write(b": hb\n\n")
                        self.wfile.flush()
                        last_hb = time.time()
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
