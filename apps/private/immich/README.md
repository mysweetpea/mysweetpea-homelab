# Immich — pure viewer over Nextcloud

Immich does **not** own photo storage here. It reads each member's Nextcloud
folder read-only and acts as a viewer (timeline, faces, search). One copy of
every file, one backup target.

```
authentik SSO
  ├── Nextcloud  → files (member's own account)
  └── Immich     → library over /nextcloud/data/<nc-uid>/files/Immich Media
```

## How it is wired

| Piece | Where |
|---|---|
| Read-only mount of the Nextcloud volume | `values.yaml` → `persistence.nextcloud` (`existingClaim: nextcloud-nextcloud-lh`, `readOnly: true`) at `/nextcloud` |
| Node pinning | `defaultPodOptions.nodeSelector: k8s-master` — the NC volume is **RWO** and attached there; a second node would Multi-Attach |
| SSO | Immich OAuth against `https://auth.mysweetpea.cc/application/o/immich/` (provider 26); password login **disabled** (SSO only) |
| Upload block | New accounts inherit `oauth.defaultStorageQuota=0`; Gardener also enforces per-user quota `0` bytes → every upload returns "Quota has been exceeded"; external assets do not count against quota |
| Library per member | Gardener reconciler (`/sync`, hourly) attaches `Immich Media` to any member who has signed in |
| Member folder | `Immich Media/` inside their files, seeded from the Nextcloud skeleton so every new account gets it |
| Break-glass | SealedSecret `immich-breakglass` (API key with `systemConfig.update`) |

## Ops recipes

```bash
# re-enable the password form if authentik is down (break-glass)
KEY=$(kubectl -n private get secret immich-breakglass -o jsonpath='{.data.api-key}' | base64 -d)
# GET /api/system-config → set passwordLogin.enabled=true → PUT it back, header x-api-key: $KEY

# attach libraries by hand (Telegram)
/sync          # in @mysweetpea_gardener_bot

# force a library rescan
curl -X POST http://immich.lb:2283/api/libraries/<id>/scan -H "Authorization: Bearer <admin>"
```

## Gotchas measured in the 2026-11-10 audit

- **Re-scoping a library trashes its assets** (files untouched). Re-pointing
  `importPaths` off a folder permanently drops those assets from the timeline.
- `POST /api/trash/empty` accepts the job but the background worker can wedge
  (all queues report `completed: 0`); restarting the `immich-server` pod drains
  it in bursts. Leftover trash self-purges after `trash.days` (30).
- `DELETE /api/admin/users/<id>` **requires a JSON body**; without one it 400s
  with a schema error. `{"force": true}` deletes immediately.
- `GET /api/libraries` reports `assetCount` as 0 even when assets exist — trust
  `/api/search/metadata` or the DB, not that field.
- Import paths must exist; a non-existent path makes the create/update 400.
- The reconciler is serialised (lock) and repairs drifted paths, not just
  missing libraries. It also recreates a missing Nextcloud folder and replaces
  the complete import-path list, so an unexpected extra member path is removed.
- If DB rows advertise derivatives but their files are missing, regenerate them
  with an admin session: `PUT /api/jobs/thumbnailGeneration` with
  `{"command":"start","force":true}`. Immich queues only non-deleted,
  non-hidden assets; verify every visible `asset_file` path exists afterwards.
  This writes derived images only and never changes the Nextcloud originals.
- New-account `oauth.defaultStorageQuota` is Immich DB state (not chart values).
  After a fresh DB restore, set it back to `0` with the break-glass key and a
  full `GET /api/system-config` → `PUT /api/system-config` round trip.

## Verifying the member experience without a password (2026-11-10)

The web UI is cookie-auth, so you can prove the whole read path with a forged
session and no OAuth round trip:

1. `token=$(openssl rand -hex 32)`, `hash=$(printf %s "$token" | sha256sum | cut -d' ' -f1)`
2. `insert into session (id, token, "createdAt","updatedAt","userId","deviceType","deviceOS","appVersion","expiresAt") values (gen_random_uuid(), decode('<hash>','hex'), now(), now(), '<immich userId>', 'WEB','Linux','probe', now() + interval '1 hour');`
   (the column is **bytea = sha256 of the token**, not the token itself)
3. API: `curl -H "x-immich-user-token: $token" .../api/users/me` → 200.
4. Browser: set `immich_access_token` (httpOnly **true**) **and**
   `immich_is_authenticated=true` (httpOnly **false** — the SPA reads it from
   `document.cookie`; with httpOnly the app silently bounces to `/auth/login`).
   Then load `/` → lands on `/photos` with the timeline.
5. `delete from session where id='<id>'` when done.

External-library scan proof: drop a file in `<uid>/files/Immich Media` on the NC
PVC, `occ files:scan --path="<uid>/files/Immich Media"`, `POST /api/libraries/<id>/scan?refresh=true`,
then check `asset` (status active) and `asset_file` (rows of type `preview` +
`thumbnail`). Thumbnail/original via the API need a **user session** — the
break-glass key only carries `systemConfig.update` and 403s on assets.
