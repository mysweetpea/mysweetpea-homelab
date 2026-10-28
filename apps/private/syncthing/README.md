# Syncthing hub

Always-on sync peer for personal devices (and later, per-person family folders).
Replaced Resilio Sync (Oct 2026).

## Deployment

- Image `ghcr.io/syncthing/syncthing:2.1.7`, namespace `private`, `Recreate` (RWO).
- Config 5Gi on `longhorn` (2 replicas — holds the device identity; losing it means re-pairing every device).
- Data 100Gi on `longhorn-single` (1 replica — every file also exists on a device; Longhorn is thin).
- Sync port `192.168.20.217:22000` TCP+UDP via MetalLB. GUI is **not** published — use
  `kubectl -n private port-forward svc/syncthing-gui 8840:8384` (8888 is taken by gluetun, and the
  PC's own daemon owns 127.0.0.1:8384, so kubectl silently falls back to [::1] if you reuse 8384).
- NetworkPolicies: `allow-syncthing-egress` (22000 tcp+udp, relay 22067, discovery 443 via the shared
  internet policy) and `allow-syncthing-mesh-ingress` (`100.64.0.0/10` — NetBird CGNAT is not covered
  by the shared LAN/VPN policy).
- Hub settings baked in: device name `MySweetPea Hub`, default folder path `/var/syncthing/data`,
  default versioning `staggered` (maxAge 30 days), `ignorePerms` on, `minDiskFree` 5 GB,
  usage reporting declined (`urAccepted=-1`).

## Identity

| | |
|---|---|
| Device ID | `2CJ75UB-3QWPMCW-U3QJXYX-5SQ6Z23-OJOCMWQ-H4MTYIX-RSVT6CI-3ZDA6QB` |
| Sync address (LAN) | `tcp://192.168.20.217:22000` |
| Sync address (NetBird mesh) | `tcp://10.42.0.119:22000` (pod IP — changes if the pod is rescheduled) |
| Kuma monitor | id 70, TCP port check on `192.168.20.217:22000`, Gotify bound |

Clients use both addresses, comma separated, so at home they take the LAN path and off-LAN they ride
the mesh; if neither works Syncthing falls back to the public relay.

## Folders

| Folder ID | Label | Content | Hub path |
|---|---|---|---|
| `mnbx3-n7tay` | laptop | 7.6 GB / ~1,140 files | `/var/syncthing/data/laptop` |
| `tgcdm-utqpw` | iphone | 26.2 GB / ~7,470 files | `/var/syncthing/data/iphone` |

Members: PC (`K6SVQSK`), Laptop (`M5J6DJZ`), iPhone/Synctrain (`JJC5I2Q`), Hub.

Ignore patterns (every device, every folder — per-folder `.stignore` is the only defence):

```
Thumbs.db
desktop.ini
.DS_Store
._*
~$*
*.tmp
$RECYCLE.BIN
.sync
```

## Ops notes

- **Purging junk without 30 days of `.stversions` bloat:** pause the folder, set `versioning type ""`,
  `rm -rf` the junk, rescan, set `staggered` back, unpause.
- **Backups:** the config volume carries `recurring-job-group.longhorn.io/default=enabled` (nightly
  snapshot + offsite backup). The data volume is deliberately `disabled` — 34 GB × 7-day retention
  would exhaust the VPS backup target, and the data exists on every device.
- **NetBird routing trap:** Linux NetBird clients install a route for `192.168.20.0/24` that reaches
  node IPs but *not* MetalLB VIPs, which silently pushes Syncthing onto the public relay. On
  LAN-resident machines run `netbird networks deselect "MetalLB / node LAN" "private"` and restart
  Syncthing.
- **Family (planned):** one folder per person on this hub, shared only to that person's devices.
  No accounts or quotas exist — the hub admin (owner) is the only GUI.
