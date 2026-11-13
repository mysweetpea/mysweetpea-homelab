#!/usr/bin/env python3
"""Gardener Admin -- Telegram bot for member management inside the garden.

Commands:
  /help                      this message
  /whoami                    your chat id
  /family list               groups + folders
  /family new <slug> <Name>  create family group + restricted sub-folder
  /family add <slug> <user>  give a member access
  /family remove <slug> <user>
"""
import html
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
AK_TOKEN = os.environ.get("AUTHENTIK_TOKEN", "")
AK_URL = os.environ.get("AUTHENTIK_URL", "https://auth.mysweetpea.cc/api/v3")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
ALLOWED = {x.strip() for x in os.environ.get("ALLOWED_CHATS", "").split(",") if x.strip()}
FOLDER_ID = os.environ.get("FOLDER_ID", "1")
BASE_SLUG = os.environ.get("BASE_SLUG", "sweetpea")
NS = os.environ.get("NAMESPACE", "private")
PORT = int(os.environ.get("PORT", "8080"))
# Immich "pure viewer" plumbing: every member gets a library over their own
# Nextcloud folder, and a 0-byte quota that makes uploads impossible.
IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich-lb.private.svc.cluster.local:2283")
IMMICH_KEY = os.environ.get("IMMICH_API_KEY", "")
MEDIA_FOLDER = "Immich Media"
MEMBER_QUOTA = 0  # 0 bytes = every upload rejected ("Quota has been exceeded");
                  # external-library assets do not count, so the viewer still works
RECONCILE_EVERY = 3600
_RECONCILE_LOCK = __import__("threading").Lock()
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"
# Dashboard + mail: username-change requests land in the dashboard's KV (the
# member-facing form posts there); this bot approves them and mails the member.
DASH_URL = os.environ.get("DASHBOARD_URL", "https://dashboard.mysweetpea.cc")
DASH_TOKEN = os.environ.get("DASHBOARD_BOT_TOKEN", "").strip()
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "MySweetPea <noreply@mysweetpea.cc>")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
FILES_DIR = "/var/www/html/data/__groupfolders/%s/files" % FOLDER_ID


# ---------------------------------------------------------------- transport

def http(method, url, body=None, headers=None, timeout=60):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def tg(chat_id, text):
    st, _r = http("POST", "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN,
                {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                 "disable_web_page_preview": True})
    if st != 200:
        sys.stderr.write("[gardener] telegram send failed %s: %s\n" % (st, str(_r)[:200]))
    return st


def ak(method, path, body=None):
    return http(method, AK_URL + path, body, {"Authorization": "Bearer " + AK_TOKEN})


def kubectl(*args, timeout=120):
    p = subprocess.run(["kubectl", "-n", NS] + list(args),
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[:200])
    return p.stdout


def nc_pod():
    out = kubectl("get", "pods", "-l", "app.kubernetes.io/name=nextcloud",
                  "-o", 'jsonpath={range .items[*]}{.metadata.name} {.status.containerStatuses[0].ready}{"\\n"}{end}')
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "true":
            return parts[0]
    raise RuntimeError("nextcloud pod not ready")


def occ(*args):
    return kubectl("exec", nc_pod(), "-c", "nextcloud", "--", "php", "occ", *args)


def nc_sh(cmd):
    return kubectl("exec", nc_pod(), "-c", "nextcloud", "--", "sh", "-c", cmd)


def dash(method, path, body=None):
    """Call the dashboard worker's bot lane (BOT_ADMIN_TOKEN)."""
    return http(method, DASH_URL + path, body, {"x-msp-internal": DASH_TOKEN})


def vault_message(r):
    name = html.escape(str(r.get("name") or r.get("username") or "Member"))
    email = str(r.get("email") or "")
    who = "<b>%s</b>%s" % (name, (" · <code>%s</code>" % html.escape(email)) if email else "")
    return ("🔐 <b>Vaultwarden setup request</b>\n"
            + who + "\n"
            "Open https://vault.mysweetpea.cc → MySweetPea → Members → Invite.\n"
            "After they accept, click Confirm. No owner credential is stored in this bot.")


def vault_once():
    """One poll cycle. Returns the number of requests handed to the owner."""
    st, d = dash("GET", "/api/bot/vault-requests")
    if st != 200 or not isinstance(d, dict):
        return 0
    sent_count = 0
    for r in (d.get("requests") or []):
        sent = False
        for chat in ALLOWED:
            if tg(chat, vault_message(r)) == 200:
                sent = True
        if sent:
            sent_count += 1
            st2, _ = dash("POST", "/api/bot/vault-requests/notified", {"id": r.get("id")})
            if st2 != 200:
                sys.stderr.write("[gardener] vault request notify mark failed %s\n" % st2)
    return sent_count


def vault_loop():
    """Poll the dashboard's small request inbox; notify the owner once each."""
    if not DASH_TOKEN or not ALLOWED:
        return
    while True:
        try:
            vault_once()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[gardener] vault request poll failed: %s\n" % str(e)[:200])
        time.sleep(120)


def send_mail(to, subject, body_html):
    """Resend REST; returns the HTTP status (200 = accepted)."""
    if not RESEND_KEY or not to:
        return 0
    st, _ = http("POST", "https://api.resend.com/emails",
                 {"from": MAIL_FROM, "to": [to], "subject": subject, "html": body_html},
                 {"Authorization": "Bearer " + RESEND_KEY})
    return st


# Model picker management: members only see models registered in Open WebUI's
# own DB with a public read grant (unregistered connection models are admin-
# only). These helpers run inside the open-webui pod, which is where both the
# admin JWT secret and the sqlite file live.
OWUI_POD_LABEL = "app.kubernetes.io/name=open-webui"
OWUI_ADMIN_ID = "866ed497-8ddf-4fc7-9f5f-4cbdab242f06"
OWUI_PY = r'''
import base64, json, sqlite3, sys, urllib.request, urllib.error

ADMIN = "866ed497-8ddf-4fc7-9f5f-4cbdab242f06"
DB = "/app/backend/data/webui.db"
act = json.loads(base64.b64decode(sys.argv[1]))


def api(path, body=None):
    import jwt
    secret = open("/app/backend/.webui_secret_key").read().strip()
    tok = jwt.encode({"id": ADMIN}, secret, algorithm="HS256")
    tok = tok.decode() if isinstance(tok, bytes) else tok
    req = urllib.request.Request(
        "http://127.0.0.1:8080" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


op = act["op"]
if op == "list":
    db = sqlite3.connect(DB)
    for mid, name in db.execute("select id, name from model order by name"):
        print("%s\t%s" % (mid, name))
elif op == "add":
    meta = {"description": act.get("desc") or ""}
    grant = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]
    st, _ = api("/api/v1/models/create", {
        "id": act["id"], "name": act["name"], "base_model_id": None,
        "meta": meta, "params": {}, "is_active": True, "access_grants": grant})
    st2, _ = api("/api/v1/models/model/access/update",
                 {"id": act["id"], "name": act["name"], "access_grants": grant})
    known = False
    st3, b3 = api("/api/models")
    try:
        known = any(m["id"] == act["id"] for m in json.loads(b3)["data"])
    except Exception:
        pass
    print("create=%s grant=%s in_connection=%s" % (st, st2, known))
elif op == "del":
    st, b = api("/api/v1/models/model/delete", {"id": act["id"]})
    print("delete=%s %s" % (st, b[:80].decode("utf-8", "replace")))
'''


def owui(payload):
    pod = kubectl("get", "pods", "-l", OWUI_POD_LABEL,
                  "-o", "jsonpath={.items[0].metadata.name}").strip()
    if not pod:
        raise RuntimeError("open-webui pod not found")
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    return kubectl("exec", pod, "--", "python3", "-c", OWUI_PY, b64, timeout=180)


def gid(slug):
    import hashlib
    return hashlib.sha256(("1_0_" + slug).encode()).hexdigest()


# ---------------------------------------------------------------- commands

def ak_group(slug):
    st, d = ak("GET", "/core/groups/?search=" + urllib.parse.quote(slug))
    if st == 200:
        for g in d.get("results", []):
            if g["name"] == slug:
                return g
    return None


def ak_user(username):
    st, d = ak("GET", "/core/users/?username=" + urllib.parse.quote(username))
    if st == 200 and d.get("results"):
        return d["results"][0]
    return None


def folder_list():
    out = nc_sh("ls -1 %s 2>/dev/null" % FILES_DIR)
    return [x for x in out.splitlines() if x.strip()]


# ------------------------------------------------------- immich reconcile
# Immich is a pure viewer over Nextcloud: every member's library points at
# their own "Immich Media" folder, uploads blocked by quota. Members get an
# Immich account the first time they sign in with SSO; this loop attaches the
# library to accounts that don't have one yet.

def media_path(uid):
    return "/nextcloud/data/%s/files/%s" % (uid, MEDIA_FOLDER)


def ensure_member_defaults(uid):
    # Repair real storage before Immich validates the import path (400 on ENOENT).
    # Existing files are never overwritten; only a newly copied file is scanned.
    home = "data/%s/files" % uid
    nc_sh("install -d -o www-data -g www-data -- " + shlex.quote(home) + " "
          + shlex.quote(home + "/" + MEDIA_FOLDER))
    for rel in ("README.md", MEDIA_FOLDER + "/README.txt"):
        src = "/var/www/html/config/skeleton-empty/" + rel
        dst = home + "/" + rel
        copied = nc_sh("if [ ! -f %s ]; then install -o www-data -g www-data -m 0644 %s %s; printf yes; fi"
                       % (shlex.quote(dst), shlex.quote(src), shlex.quote(dst)))
        if copied.strip() == "yes":
            occ("files:scan", "--path=" + uid + "/files/" + rel)


def im(method, path, body=None):
    return http(method, IMMICH_URL + path, body, {"x-api-key": IMMICH_KEY})


def nc_members():
    """uid -> email for every real member (sweetpea tier), local admin excluded."""
    raw = occ("user:list", "--info", "--output=json")
    data = json.loads(raw)
    sweet = gid(BASE_SLUG)
    out = {}
    for uid, info in data.items():
        if uid == "admin" or not info.get("email"):
            continue
        if sweet not in (info.get("groups") or []):
            continue
        out[uid] = info["email"].strip().lower()
    return out


def reconcile():
    """Attach a library (and the upload-blocking quota) to every member who has
    signed into Immich. Idempotent; returns a list of human-readable changes."""
    if not IMMICH_KEY:
        return ["skipped: IMMICH_API_KEY not set"]
    # ponytail: one global lock — the hourly thread and /sync can overlap and
    # would otherwise both see "missing" and create duplicate libraries.
    with _RECONCILE_LOCK:
        return _reconcile_locked()


def _reconcile_locked():
    st, users = im("GET", "/api/admin/users")
    if st != 200 or not isinstance(users, list):
        raise RuntimeError("immich users %s: %s" % (st, str(users)[:160]))
    st, libs = im("GET", "/api/libraries")
    if st != 200 or not isinstance(libs, list):
        raise RuntimeError("immich libraries %s: %s" % (st, str(libs)[:160]))
    by_email = {u["email"].strip().lower(): u for u in users if u.get("email")}
    have = {(l["ownerId"], l["name"]): l for l in libs}
    members = nc_members()
    changes = []
    for uid, email in members.items():
        ensure_member_defaults(uid)
        u = by_email.get(email)
        if not u:
            continue  # hasn't signed into Immich yet — nothing to attach
        if u.get("quotaSizeInBytes") != MEMBER_QUOTA:
            st, r = im("PUT", "/api/admin/users/" + u["id"], {"quotaSizeInBytes": MEMBER_QUOTA})
            if st != 200:
                raise RuntimeError("quota %s: %s %s" % (email, st, str(r)[:160]))
            changes.append("quota %s" % email)
        want = media_path(uid)
        lib = have.get((u["id"], MEDIA_FOLDER))
        if lib is None:
            st, r = im("POST", "/api/libraries", {"ownerId": u["id"], "name": MEDIA_FOLDER,
                                                  "importPaths": [want],
                                                  "exclusionPatterns": ["**/README.txt"]})
            if st not in (200, 201):
                raise RuntimeError("create library for %s: %s %s" % (email, st, str(r)[:160]))
            changes.append("library %s" % email)
        elif sorted(lib.get("importPaths") or []) != [want]:
            # Exact ownership boundary: drop unexpected paths and repair missing ones.
            st, r = im("PUT", "/api/libraries/" + lib["id"],
                       {"name": MEDIA_FOLDER, "importPaths": [want],
                        "exclusionPatterns": ["**/README.txt"]})
            if st != 200:
                raise RuntimeError("repair library for %s: %s %s" % (email, st, str(r)[:160]))
            changes.append("repaired path %s" % email)
    # An Immich account whose email matches no Nextcloud member can only happen
    # when the two identities drifted (e.g. someone changed their authentik
    # email) — it would otherwise sit there silently with no library.
    for email in by_email:
        if email not in set(members.values()):
            sys.stderr.write("[gardener] immich user %s matches no Nextcloud account\n" % email)
    return changes


def reconcile_loop():
    import time
    time.sleep(90)  # let the pod finish starting before the first sweep
    while True:
        try:
            ch = reconcile()
            if ch:
                sys.stderr.write("[gardener] reconcile: %s\n" % ", ".join(ch))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write("[gardener] reconcile failed: %s\n" % str(e)[:200])
        time.sleep(RECONCILE_EVERY)


def cmd_sync():
    ch = reconcile()
    if not ch:
        return "✅ <b>Immich sync</b>\nNothing to do — every member who has signed in already has their library."
    return "✅ <b>Immich sync</b>\n" + "\n".join("• " + html.escape(c) for c in ch)


def cmd_models():
    out = owui({"op": "list"}).strip()
    rows = [l.split("\t", 1) for l in out.splitlines() if "\t" in l]
    if not rows:
        return ("🤖 <b>Model picker</b>\nNo models are visible to members right now.\n\n"
                "<i>Add one: /model add &lt;id&gt; &lt;Display Name&gt;</i>")
    body = "\n".join("• <code>%s</code> — %s" % (html.escape(m), html.escape(n)) for m, n in rows)
    return ("🤖 <b>Models members can pick</b>\n━━━━━━━━━━━━━━━━━━━━\n%s\n\n"
            "<i>Add: /model add &lt;id&gt; &lt;Display Name&gt;\nRemove: /model del &lt;id&gt;\n"
            "Admins always see every model the proxy serves.</i>") % body


def cmd_model_add(args):
    if len(args) < 2:
        return ("Usage: <code>/model add &lt;id&gt; &lt;Display Name&gt;</code>\n"
                "Example: <code>/model add glm-5.3-flash GLM 5.3 Flash</code>")
    mid, name = args[0], " ".join(args[1:])
    r = owui({"op": "add", "id": mid, "name": name}).strip()
    if "create=200" not in r:
        return "⚠️ Could not add <code>%s</code>:\n<code>%s</code>" % (html.escape(mid), html.escape(r))
    warn = "" if "in_connection=True" in r else (
        "\n⚠️ That id is not in the LiteLLM connection — check the exact name, "
        "otherwise the model will appear but fail on the first message.")
    return "✅ <b>%s</b> is now in the members' picker.%s" % (html.escape(name), warn)


def cmd_model_del(arg):
    r = owui({"op": "del", "id": arg}).strip()
    if "delete=200" not in r:
        return "⚠️ Could not remove <code>%s</code>:\n<code>%s</code>" % (html.escape(arg), html.escape(r))
    return ("🗑 Removed <code>%s</code> from the members' picker. "
            "Admins still see it." % html.escape(arg))


def cmd_renames():
    st, d = dash("GET", "/api/bot/username-requests")
    if st != 200 or not isinstance(d, dict):
        return "⚠️ Could not read requests from the dashboard (%s)." % st
    reqs = d.get("requests") or []
    if not reqs:
        return "✅ <b>No pending username requests.</b>"
    out = ["👤 <b>Username change requests</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for r in reqs:
        out.append("• <code>%s</code> → <code>%s</code>%s" % (
            html.escape(str(r.get("username", ""))),
            html.escape(str(r.get("desired", ""))),
            ("  <i>%s</i>" % html.escape(str(r.get("email", "")))) if r.get("email") else ""))
    out += ["", "<i>Approve: /rename &lt;current&gt;\nDecline: /deny &lt;current&gt;</i>"]
    return "\n".join(out)


def cmd_rename(args):
    if len(args) not in (1, 2):
        return "Usage: <code>/rename &lt;current&gt;</code>  (or <code>/rename &lt;current&gt; &lt;new&gt;</code>)"
    old, new = args[0].lower(), (args[1].lower() if len(args) == 2 else "")
    if not new:
        # One word is enough: take the wanted name from the member's request.
        st, d = dash("GET", "/api/bot/username-requests")
        if st != 200 or not isinstance(d, dict):
            return "⚠️ Could not read requests from the dashboard (%s)." % st
        rec = next((r for r in (d.get("requests") or [])
                    if str(r.get("username", "")).lower() == old), None)
        if not rec:
            return ("No pending request from <code>%s</code>. See <code>/renames</code>, "
                    "or give the new name: <code>/rename %s newname</code>" % (html.escape(old), html.escape(old)))
        new = str(rec.get("desired", "")).lower()
    if not re.match(r"^[a-z0-9][a-z0-9._-]{2,29}$", new):
        return ("⚠️ Usernames are 3-30 characters: lowercase letters, digits, "
                "dot, dash or underscore.")
    u = ak_user(old)
    if not u:
        return "⚠️ No one has the username <code>%s</code>." % html.escape(old)
    st, d = ak("PATCH", "/core/users/%s/" % u["pk"], {"username": new})
    if st != 200:
        return "⚠️ authentik refused the change (%s):\n<code>%s</code>" % (
            st, html.escape(str(d)[:200]))
    st2, _ = dash("POST", "/api/bot/username-requests/resolve",
                  {"username": old, "status": "done", "newUsername": new})
    body = (
        "<p>Hi %s,</p>"
        "<p>Your MySweetPea username is now <b>%s</b>. Use it the next time you "
        "sign in — your password stays the same.</p>"
        "<p>Two things that do <b>not</b> change: your Moonfin (movies &amp; TV) "
        "sign-in, which is still your name plus your PIN, and your files, photos "
        "and emails.</p>"
        "<p>If you use Jellyfin for Live TV, sign in once with the new username.</p>"
        "<p>— MySweetPea</p>"
    ) % (html.escape(str(u.get("name") or old)), html.escape(new))
    mail = send_mail(u.get("email"), "Your MySweetPea username has changed", body)
    notes = []
    notes.append("📧 emailed %s" % html.escape(str(u.get("email") or "(no email on file)"))
                 if mail == 200 else "⚠️ email failed (%s)" % mail)
    notes.append("request closed" if st2 == 200 else "request not closed (%s)" % st2)
    return "✅ <b>%s</b> is now <b>%s</b>.\n%s" % (
        html.escape(old), html.escape(new), " · ".join(notes))


def cmd_deny(arg):
    st, _ = dash("POST", "/api/bot/username-requests/resolve",
                 {"username": arg, "status": "denied"})
    if st != 200:
        return "⚠️ Could not close the request (%s)." % st
    return "🚫 Declined the username change for <code>%s</code>. They keep their current name." % html.escape(arg)


def cmd_help(chat):
    return (
        "🌿 <b>Gardener Admin</b>\n"
        "<i>Member management for MySweetPea</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>AI models and Open WebUI</b>\n\n"
        "<b>/models</b>\n"
        "Shows the models members can pick in Open WebUI right now.\n\n"
        "<b>/model add</b> <code>id Display Name</code>\n"
        "Puts a model in the members' picker, with that name.\n"
        "Find the id with <code>/models</code> or the proxy's model list; copy it exactly.\n"
        "Example: <code>/model add glm-5.3-flash GLM 5.3 Flash</code>\n\n"
        "<b>/model del</b> <code>id</code>\n"
        "Takes it back out. You still see every model yourself.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Username change requests</b>\n\n"
        "<b>/renames</b>\n"
        "Lists members waiting for a new username (they ask from the dashboard).\n\n"
        "<b>/rename</b> <code>current</code>\n"
        "Approves that member's request: uses the name they asked for, closes the "
        "request and emails them.\n"
        "Example: <code>/rename komal</code>\n"
        "(Override with <code>/rename komal komal.iqbal</code> if they typed it wrong.)\n\n"
        "<b>/deny</b> <code>current</code>\n"
        "Declines the request; they keep their current name.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👥 <b>Members and folders</b>\n\n"
        "<b>/family list</b>\n"
        "Shows the member tiers, every private folder and how many people are in each. "
        "Start here when you forget what exists.\n\n"
        "<b>/family add</b> <code>slug username</code>\n"
        "Lets one member into one private folder.\n"
        "• <code>slug</code> is the short group name, e.g. <code>iqbal-family</code>\n"
        "• <code>username</code> is their MySweetPea login, e.g. <code>komal</code>\n"
        "Access is instant if they have signed in to Nextcloud before, otherwise it "
        "applies at their first sign-in.\n"
        "Example: <code>/family add iqbal-family komal</code>\n\n"
        "<b>/family remove</b> <code>slug username</code>\n"
        "Takes that access away again, in Nextcloud and in the login system.\n"
        "Example: <code>/family remove iqbal-family komal</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🗂 <b>Creating a private folder</b>\n\n"
        "<b>/family new</b> <code>slug Folder Name</code>\n"
        "Creates a new private folder inside the shared one, plus the group that decides "
        "who can see it.\n"
        "• <code>slug</code> — lowercase letters, digits and dashes; the permanent internal name\n"
        "• <code>Folder Name</code> — what members actually see\n"
        "Example: <code>/family new khan-family Khan Family</code>\n"
        "Every member can open the shared folder, but only people you add afterwards can "
        "see this sub-folder; nobody else is told it exists.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>Housekeeping</b>\n\n"
        "<b>/sync</b> — attach a photo library to any member who has signed into\n"
        "Immich but has none yet (also runs by itself every hour)\n"
        "<b>/whoami</b> — your Telegram chat id (needed to authorise another device)\n"
        "<b>/help</b> — this page\n\n"
        "<i>Only the admin account can use these commands.</i>"
    )


def cmd_list():
    st, d = ak("GET", "/core/groups/?ordering=name&page_size=100")
    groups, folders = [], folder_list()
    if st == 200:
        for g in d.get("results", []):
            name = g["name"]
            # Only garden-relevant groups: the two tiers plus family groups.
            if name not in ("sweetpea", "seedling") and not name.endswith("-family"):
                continue
            st2, grp = ak("GET", "/core/groups/%s/" % g["pk"])
            members = (grp.get("users_obj") or []) if st2 == 200 else []
            groups.append((name, len(members)))

    out = ["🌿 <b>Garden members and folders</b>", "━━━━━━━━━━━━━━━━━━━━", "👥 <b>Groups</b>"]
    for name, n in groups:
        out.append("• <code>%s</code> — %d %s" % (
            html.escape(name), n, "member" if n == 1 else "members"))
    out += ["", "🗂 <b>Inside the shared folder</b>"]
    out += ["• %s" % html.escape(f) for f in folders] if folders else ["• (empty)"]
    out += ["", "<i>Add someone: /family add &lt;slug&gt; &lt;username&gt;</i>"]
    return "\n".join(out)


def cmd_new(chat, args):
    if len(args) < 2:
        return ("Usage: <code>/family new &lt;slug&gt; &lt;Folder Name&gt;</code>\n"
                "Example: <code>/family new khan-family Khan Family</code>")
    slug, name = args[0], " ".join(args[1:]).strip()
    if not SLUG_RE.match(slug):
        return ("Bad slug: use lowercase letters, digits and dashes (2-31 characters), "
                "e.g. <code>khan-family</code>.")
    if not (1 <= len(name) <= 40) or "/" in name:
        return "Bad folder name (1-40 characters, no slashes)."
    if name in folder_list():
        return "A folder named <b>%s</b> already exists." % html.escape(name)

    gid_ = gid(slug)
    created = ak_group(slug) is None
    if created:
        st, _ = ak("POST", "/core/groups/", {"name": slug, "is_superuser": False})
        if st not in (200, 201):
            return "⚠️ authentik group create failed (%s)" % st

    occ("group:add", gid_, "--display-name=" + slug)
    nc_sh("mkdir -p %s && chown -R www-data:www-data %s" % (
        shlex.quote(FILES_DIR + "/" + name), shlex.quote(FILES_DIR + "/" + name)))
    occ("groupfolders:scan", FOLDER_ID)
    occ("groupfolders:permissions", "-g", gid(BASE_SLUG), FOLDER_ID, name,
        "--", "-read", "-write", "-create", "-delete", "-share")
    occ("groupfolders:permissions", "-g", gid_, FOLDER_ID, name,
        "--", "+read", "+write", "+create", "+delete", "+share")

    return (
        "✅ <b>%s</b> is ready\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• private group <code>%s</code> %s\n"
        "• folder added to the shared space\n"
        "• visible only to its members\n\n"
        "<b>Next:</b> <code>/family add %s &lt;username&gt;</code>"
    ) % (html.escape(name), html.escape(slug),
         "created" if created else "already existed", html.escape(slug))


def cmd_add(chat, args, remove=False):
    verb = "remove" if remove else "add"
    if len(args) < 2:
        return ("Usage: <code>/family %s &lt;slug&gt; &lt;username&gt;</code>\n"
                "Example: <code>/family %s iqbal-family komal</code>") % (verb, verb)
    slug, username = args[0], args[1]
    if not SLUG_RE.match(slug):
        return "Bad slug: use lowercase letters, digits and dashes."
    g = ak_group(slug)
    if g is None:
        return ("No family group <code>%s</code>.\n"
                "Create it first: <code>/family new %s &lt;Folder Name&gt;</code>"
                % (html.escape(slug), html.escape(slug)))
    u = ak_user(username)
    if u is None:
        return ("No garden member called <code>%s</code>.\n"
                "Check the spelling of their MySweetPea username." % html.escape(username))

    if remove:
        ak("POST", "/core/groups/%s/remove_user/" % g["pk"], {"pk": u["pk"]})
    else:
        st, _ = ak("POST", "/core/groups/%s/add_user/" % g["pk"], {"pk": u["pk"]})
        if st not in (200, 204):
            return "⚠️ login-system membership failed (%s)" % st

    # Nextcloud side: match the NC account by email, else it applies at first login.
    nc_note = "Nextcloud: applies at their first sign-in"
    try:
        listing = json.loads(occ("user:list", "--info", "--output=json"))
        email = (u.get("email") or "").lower()
        ncu = next((k for k, v in listing.items()
                    if (v.get("email") or "").lower() == email), "")
        if ncu:
            occ("group:" + ("removeuser" if remove else "adduser"), gid(slug), ncu)
            nc_note = "Nextcloud: %s" % ("access removed" if remove else "access granted")
    except Exception as e:  # noqa: BLE001 - NC side is best effort
        nc_note = "Nextcloud: skipped (%s)" % str(e)[:80]

    head = ("🚪 <b>%s</b> no longer has access to <b>%s</b>" if remove
            else "✅ <b>%s</b> now has access to <b>%s</b>")
    return (head + "\n━━━━━━━━━━━━━━━━━━━━\n%s") % (
        html.escape(username), html.escape(slug), nc_note)


def dispatch(text, chat_id):
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].split("@")[0].lower()
    rest = parts[1:]

    if cmd in ("/start", "/help"):
        return cmd_help(chat_id)
    if cmd == "/whoami":
        return "chat id: <code>%s</code>" % chat_id
    if cmd in ("/sync", "/immich"):
        return cmd_sync()
    if cmd == "/models":
        return cmd_models()
    if cmd == "/model":
        sub = rest[0].lower() if rest else ""
        if sub == "add" and len(rest) >= 3:
            return cmd_model_add(rest[1:])
        if sub in ("del", "remove") and len(rest) == 2:
            return cmd_model_del(rest[1])
        return "Usage: <code>/model add &lt;id&gt; &lt;Display Name&gt;</code>  ·  <code>/model del &lt;id&gt;</code>"
    if cmd in ("/renames", "/requests"):
        return cmd_renames()
    if cmd == "/rename":
        return cmd_rename(rest)
    if cmd == "/deny":
        if not rest:
            return "Usage: <code>/deny &lt;current-username&gt;</code>"
        return cmd_deny(rest[0].lower())
    if cmd == "/family":
        sub = rest[0].lower() if rest else "list"
        if sub == "list":
            return cmd_list()
        if sub == "new":
            return cmd_new(chat_id, rest[1:])
        if sub == "add":
            return cmd_add(chat_id, rest[1:])
        if sub == "remove":
            return cmd_add(chat_id, rest[1:], remove=True)
        return "Unknown: /family %s" % html.escape(sub)
    return "Unknown command %s — try /help" % html.escape(cmd)


# ---------------------------------------------------------------- webhook

class Handler(BaseHTTPRequestHandler):
    server_version = "gardener/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[gardener] %s %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self._json(404, {"ok": False})

    def do_POST(self):
        if WEBHOOK_SECRET and self.path != "/webhook/gardener-" + WEBHOOK_SECRET:
            self._json(404, {"ok": False})
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            update = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False})
            return
        # Answer Telegram immediately; work happens before the reply anyway.
        msg = update.get("message") or update.get("edited_message") or {}
        text = msg.get("text") or ""
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id and text.startswith("/"):
            if ALLOWED and chat_id not in ALLOWED:
                tg(chat_id, "🔒 Not authorised.\nYour chat id is <code>%s</code>." % chat_id)
            else:
                try:
                    reply = dispatch(text, chat_id)
                except Exception as e:  # noqa: BLE001
                    reply = "⚠️ %s" % html.escape(str(e)[:300])
                if reply:
                    sys.stderr.write("[gardener] reply %s: %s\n" % (chat_id, reply[:300]))
                    tg(chat_id, reply)
        self._json(200, {"ok": True})


def selftest():
    assert gid("sweetpea") == "f09715042ac606bdbacdea360bc6008bc45e8da27a59ad3a0909580727304304"
    assert media_path("abc") == "/nextcloud/data/abc/files/Immich Media"
    assert reconcile.__doc__ and "Idempotent" in reconcile.__doc__
    assert SLUG_RE.match("iqbal-family") and not SLUG_RE.match("Bad Slug")
    assert "Vaultwarden setup request" in vault_message({"name": "A <B>", "email": "a@b"})
    assert "&lt;B&gt;" in vault_message({"name": "A <B>", "email": "a@b"})
    vault_calls = []
    def fake_vault_dash(method, path, body=None):
        if method == "GET":
            return 200, {"requests": [{"id": "r1", "name": "A", "email": "a@b"}]}
        vault_calls.append((path, body))
        return 200, {}
    def fake_vault_tg(chat, text):
        vault_calls.append(("tg", chat))
        return 200
    saved_vault = (dash, tg, ALLOWED)
    globals()["dash"], globals()["tg"], globals()["ALLOWED"] = fake_vault_dash, fake_vault_tg, {"123"}
    try:
        assert vault_once() == 1
    finally:
        globals()["dash"], globals()["tg"], globals()["ALLOWED"] = saved_vault
    assert ("tg", "123") in vault_calls
    assert any(c[0] == "/api/bot/vault-requests/notified" and c[1]["id"] == "r1" for c in vault_calls)
    assert dispatch("", 1) is None
    assert "chat id" in dispatch("/whoami", 42)
    assert "Gardener" in dispatch("/help@GardenerAdminBot", 1)
    assert "Unknown" in dispatch("/family nope", 1)
    assert "Usage" in dispatch("/family new", 1)
    assert "Bad slug" in dispatch("/family new BAD name", 1)
    # Reconcile removes an unexpected cross-member path and creates the real dir.
    calls = []
    def fake_im(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/admin/users":
            return 200, [{"id": "u1", "email": "member@example.invalid", "quotaSizeInBytes": 0}]
        if path == "/api/libraries":
            return 200, [{"id": "l1", "ownerId": "u1", "name": MEDIA_FOLDER,
                          "importPaths": ["/nextcloud/data/other/files/Immich Media"]}]
        return 200, {}
    saved = (im, nc_members, nc_sh, occ)
    globals()["im"] = fake_im
    globals()["nc_members"] = lambda: {"abc": "member@example.invalid"}
    globals()["nc_sh"] = lambda cmd: calls.append(("nc", cmd, None)) or ""
    globals()["occ"] = lambda *args: calls.append(("occ", " ".join(args), None)) or ""
    try:
        _reconcile_locked()
    finally:
        globals()["im"], globals()["nc_members"], globals()["nc_sh"], globals()["occ"] = saved
    assert any(c[0] == "PUT" and c[2]["importPaths"] == [media_path("abc")] for c in calls)
    assert any(c[0] == "nc" and "data/abc/files/Immich Media" in c[1] for c in calls)
    assert any(c[0] == "nc" and "data/abc/files/README.md" in c[1] for c in calls)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    import threading
    threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=vault_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
