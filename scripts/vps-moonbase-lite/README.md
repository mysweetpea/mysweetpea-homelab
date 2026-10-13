# moonbase-lite

Moonfin `/Moonfin/*` shim for media servers that run **AIOStreams** instead of
Jellyfin + the Moonbase plugin. One stdlib Python file, loopback-only, fronted
by nginx (`upstream moonbase_lite { 127.0.0.1:3002; 127.0.0.1:3000 backup; }` —
the backup makes it fail silent to the old 404 behaviour when the shim is down).

Serves: Ping, Defaults, Settings (scoped, Resolved, Profile push, delete,
Admin/PushDefaults, SSE Stream), Messages (read + audience filter), MDBList
ratings (proxy + cache), Themes (list/serve/upload), Notification prefs, web
stubs. Everything else 404s — clients degrade gracefully (same as no plugin).

## Files
- `moonbase_lite.py` — the service (deploy to `/opt/moonbase-lite/`)
- `config.example.json` → `config.json` (mdblist server key lives here, chmod 600)
- `seed-defaults.py` — builds `data/defaults.json` (fleet defaults) from a
  Moonfin settings export: keeps the `global` scope, strips API keys, filters
  home rows to `resume,latestmedia`, pins the theme
- `themes/` — theme JSONs served to clients (incl. `mysweetpea_obsidian`)
- `moonbase-lite.service`, `nginx-moonbase-lite-proxy.snippet.conf`
- `test-moonbase-lite.sh` — smoke matrix (needs `TOKEN` = a real device token)

## Ops
```bash
sudo -S -p '' systemctl restart moonbase-lite && journalctl -u moonbase-lite -n 20
sudo -S -p '' nginx -t && sudo -S -p '' systemctl reload nginx
python3 seed-defaults.py settings.json data/defaults.json --theme mysweetpea_obsidian
sudo -S -p '' systemctl restart moonbase-lite   # after defaults.json change (no restart needed for themes/users)
```
Data: `/opt/moonbase-lite/data/{defaults.json,users/,themes/,messages.json,notifications/,mdblist-cache.json}`

## Family personas (rollout)
Personas live in the AIOStreams user config under `jellyfin.personas`
(`{id, name, avatar?, history, hidden?, address?, lock}` — `lock` IS the PIN,
6+ digits; `address` = own sign-in code for hidden users: `/jellyfin/p/<address>`).
Add via the dashboard **Users** tab, or by merging into `config.jellyfin.personas`
through `PUT /api/v1/user`. Instance setting `jellyfin.pinSignIn=true` is already
enabled, so members sign in with their name + PIN (5 wrong tries = 15 min lockout);
PINs must be 6+ digits. Vendor note: pin-only sign-in on a *public* instance leans
on the unguessable config/address URLs — keep PINs long.

## Known v1 limits
- Admin checks (PushDefaults, Admin/Themes) are best-effort: treat any valid
  device token as admin (household-trust model). Tighten if the server ever
  goes multi-tenant.
- Messages are read-only from the file `data/messages.json`; no write API.
- Seerr / Calendars / TMDb enrichment deliberately not ported (not needed on
  a catalog server; catalogs are curated in AIOStreams itself).
