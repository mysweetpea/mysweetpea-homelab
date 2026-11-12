#!/usr/bin/env python3
"""Give every Open WebUI member their own open-terminal, automatically.

Runs in-cluster on a schedule. Reads the user list and the terminal connection
list out of the open-webui pod (its sqlite is the only place OWUI keeps them),
creates a terminal deployment for each member that lacks one, and rewrites the
connection list with a per-user access grant so the terminal shows up in that
member's chats and nowhere else.

Idempotent: safe to run every 10 minutes. `--dry-run` prints what it would do.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys

NS = os.environ.get("NAMESPACE", "private")
MEMBER_ID = re.compile(r"^term-[0-9a-f]{8}$")
OWUI_DEPLOY = os.environ.get("OWUI_DEPLOY", "deploy/open-webui")
IMAGE = os.environ.get("TERMINAL_IMAGE", "ghcr.io/open-webui/open-terminal:0.17.7")
SHARED_CONNECTION_ID = os.environ.get("SHARED_CONNECTION_ID", "term-1")
DOC_LIBS = os.environ.get("DOC_LIBS", "reportlab python-pptx openpyxl python-docx")
DRY = "--dry-run" in sys.argv

READ_PY = r"""
import json, sqlite3
db = sqlite3.connect('/app/backend/data/webui.db')
users = [{"id": i, "email": e, "name": (n or e.split('@')[0]), "role": r}
         for i, e, n, r in db.execute("select id, email, name, role from user")]
row = db.execute("select value from config where key='terminal_server.connections'").fetchone()
models = []
for mid, base, name, params, meta in db.execute(
        "select id, base_model_id, name, params, meta from model where base_model_id is null and is_active=1"):
    models.append({"id": mid, "name": name, "params": params, "meta": meta})
print(json.dumps({"users": users, "models": models,
                  "connections": json.loads(row[0]) if row and row[0] else []}))
"""

MODEL_PY = r"""
import json, sqlite3, sys, time, uuid
payload = json.load(sys.stdin)
db = sqlite3.connect('/app/backend/data/webui.db')
now = int(time.time())
for clone in payload["clones"]:
    row = db.execute("select id from model where id=?", (clone["id"],)).fetchone()
    if row:
        db.execute("update model set base_model_id=?, name=?, params=?, meta=?, is_active=1, updated_at=? where id=?",
                   (clone["base_model_id"], clone["name"], clone["params"], clone["meta"], now, clone["id"]))
    else:
        db.execute("insert into model (id, user_id, base_model_id, name, params, meta, updated_at, created_at, is_active, version_id) "
                   "values (?,?,?,?,?,?,?,?,1,?)",
                   (clone["id"], clone["user_id"], clone["base_model_id"], clone["name"],
                    clone["params"], clone["meta"], now, now, str(uuid.uuid4())))
    db.execute("delete from access_grant where resource_type='model' and resource_id=? and principal_type='user' and principal_id=?",
               (clone["id"], clone["principal_id"]))
    db.execute("insert into access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at) "
               "values (?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), 'model', clone["id"], 'user', clone["principal_id"], 'read', now))

# Base models stay visible to admins only once every member has their own copies;
# otherwise members would see the same model twice, one of them without a terminal.
if payload.get("clones") and payload.get("base_ids"):
    for bid in payload["base_ids"]:
        db.execute("delete from access_grant where resource_type='model' and resource_id=? "
                   "and principal_type='user' and principal_id='*'", (bid,))
        for aid in payload.get("admin_ids", []):
            exists = db.execute("select 1 from access_grant where resource_type='model' and resource_id=? "
                                "and principal_type='user' and principal_id=?", (bid, aid)).fetchone()
            if not exists:
                db.execute("insert into access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at) "
                           "values (?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), 'model', bid, 'user', aid, 'read', now))
db.commit()
print("models synced:", len(payload["clones"]))
"""

WRITE_PY = r"""
import json, sqlite3, sys
conns = json.load(sys.stdin)
db = sqlite3.connect('/app/backend/data/webui.db')
db.execute("update config set value=? where key='terminal_server.connections'",
           (json.dumps(conns),))
db.commit()
print(len(conns))
"""


def run(*cmd, stdin=None, check=True):
    res = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if check and res.returncode != 0:
        print("FAILED:", " ".join(cmd), file=sys.stderr)
        print(res.stderr[:800], file=sys.stderr)
        raise SystemExit(1)
    return res


def kubectl(*args, stdin=None, check=True):
    return run("kubectl", "-n", NS, *args, stdin=stdin, check=check)


def slug_for(user_id):
    return hashlib.sha256(user_id.encode()).hexdigest()[:8]


def read_state():
    res = kubectl("exec", OWUI_DEPLOY, "--", "python", "-c", READ_PY)
    return json.loads(res.stdout)


def apply(manifest):
    if DRY:
        name = manifest.split("name: ", 1)[1].split("\n", 1)[0]
        print("  would apply:", name)
        return
    kubectl("apply", "-f", "-", stdin=manifest)


def exists(kind, name):
    return kubectl("get", kind, name, check=False).returncode == 0


def terminal_manifests(slug, user):
    name = f"open-terminal-{slug}"
    labels = lambda pad: (f"{pad}app: open-terminal\n{pad}role: member\n"
                          f"{pad}instance: {slug}")
    pvc = f"""apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: term-{slug}
  labels:
{labels('    ')}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn
  resources:
    requests:
      storage: 2Gi
---
apiVersion: v1
kind: Secret
metadata:
  name: term-{slug}
  labels:
{labels('    ')}
type: Opaque
stringData:
  api-key: "{secrets.token_hex(16)}"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  labels:
{labels('    ')}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: open-terminal
      instance: {slug}
  template:
    metadata:
      labels:
{labels('        ')}
    spec:
      securityContext:
        fsGroup: 1000
      containers:
        - name: terminal
          image: {IMAGE}
          env:
            - name: OPEN_TERMINAL_API_KEY
              valueFrom:
                secretKeyRef:
                  name: term-{slug}
                  key: api-key
            - name: HOME
              value: /home/user
          ports:
            - containerPort: 8000
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              memory: 512Mi
          volumeMounts:
            - name: data
              mountPath: /home/user
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: term-{slug}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  labels:
    app: open-terminal
    instance: {slug}
spec:
  selector:
    app: open-terminal
    instance: {slug}
  ports:
    - port: 8000
      targetPort: 8000
"""
    return name, pvc


def connection_for(slug, user):
    return {
        "id": f"term-{slug}",
        "name": f"{user['name']}'s workspace",
        "enabled": True,
        "url": f"http://open-terminal-{slug}.private.svc.cluster.local:8000",
        "path": "/openapi.json",
        "key": None,  # filled from the secret
        "auth_type": "bearer",
        "config": {
            "access_grants": [
                {
                    "principal_type": "user",
                    "principal_id": user["id"],
                    "permission": "read",
                }
            ]
        },
    }


def secret_key(slug):
    res = kubectl(
        "get", "secret", f"term-{slug}", "-o", "jsonpath={.data.api-key}", check=False
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    import base64

    return base64.b64decode(res.stdout.strip()).decode()


def install_doc_libs(slug):
    """One-time pip install so 'make me a PDF' works on the first try."""
    name = f"open-terminal-{slug}"
    res = kubectl(
        "exec",
        f"deploy/{name}",
        "--",
        "sh",
        "-c",
        f"python3 -c 'import reportlab, pptx' 2>/dev/null || pip3 install --user --quiet {DOC_LIBS}",
        check=False,
    )
    print(f"  doc libs {name}: {'ok' if res.returncode == 0 else 'deferred'}")


def main():
    state = read_state()
    members = [u for u in state["users"] if u["role"] == "user"]
    conns = state["connections"]

    wanted, created = [], []
    for user in members:
        slug = slug_for(user["id"])
        name = f"open-terminal-{slug}"
        print(f"member {user['email']} -> {name}")
        if not exists("deployment", name):
            name, manifests = terminal_manifests(slug, user)
            apply(manifests)
            created.append(slug)
        else:
            print("  terminal exists")
        key = secret_key(slug) or ""
        conn = connection_for(slug, user)
        conn["key"] = key
        wanted.append(conn)

    # Keep everything that is not a per-member terminal (the shared admin one,
    # or anything added by hand in the UI).
    kept = [c for c in conns if not MEMBER_ID.match(str(c.get("id", "")))]
    merged = kept + wanted

    # Per-member model presets: the chat picks its terminal from the selected
    # model's meta.terminalId, so each member needs their own copy of each base
    # model pointing at their own terminal.
    bases = state.get("models", [])
    admins = [u["id"] for u in state["users"] if u["role"] == "admin"]
    clones = []
    for user in members:
        slug = slug_for(user["id"])
        for base in bases:
            meta = json.loads(base.get("meta") or "{}")
            meta["terminalId"] = f"term-{slug}"
            clones.append({
                "id": f"{base['id']}.{slug}",
                "base_model_id": base["id"],
                "name": base["name"],
                "params": base.get("params") or "{}",
                "meta": json.dumps(meta),
                "user_id": admins[0] if admins else user["id"],
                "principal_id": user["id"],
            })
    if clones:
        if DRY:
            print(f"would sync {len(clones)} model presets:", [c["id"] for c in clones][:4])
        else:
            kubectl("exec", "-i", OWUI_DEPLOY, "--", "python", "-c", MODEL_PY,
                    stdin=json.dumps({"clones": clones, "base_ids": [b["id"] for b in bases],
                                      "admin_ids": admins}))
            print(f"model presets synced: {len(clones)}")

    stale = [c for c in conns
             if MEMBER_ID.match(str(c.get("id", "")))
             and c.get("id") not in {w["id"] for w in wanted}]

    changed = [(c.get("id"), c.get("key")) for c in conns] != [
        (c.get("id"), c.get("key")) for c in merged
    ]
    if changed:
        if DRY:
            print("would rewrite connections:", [c["id"] for c in merged])
        else:
            kubectl("exec", "-i", OWUI_DEPLOY, "--", "python", "-c", WRITE_PY,
                    stdin=json.dumps(merged))
            print("connections rewritten:", [c["id"] for c in merged])
            print("restarting open-webui so it reloads the connection list")
            kubectl("rollout", "restart", "deploy/open-webui")
    else:
        print("connections already current:", [c["id"] for c in merged])

    for conn in stale:
        print("stale connection for a removed member:", conn["id"])
        slug = conn["id"][5:]
        if not DRY:
            kubectl("delete", "deploy", f"open-terminal-{slug}", "--ignore-not-found")
            kubectl("delete", "svc", f"open-terminal-{slug}", "--ignore-not-found")

    if created and not DRY:
        for slug in created:
            kubectl("rollout", "status", f"deploy/open-terminal-{slug}",
                    "--timeout=150s", check=False)
            install_doc_libs(slug)


if __name__ == "__main__":
    main()
