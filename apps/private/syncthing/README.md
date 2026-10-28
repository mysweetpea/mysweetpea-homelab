# Syncthing hub

Always-on sync peer for personal devices (and later, per-person family folders).

- Image `ghcr.io/syncthing/syncthing:2.1.7`, namespace `private`, `Recreate` (RWO).
- Config 5Gi on `longhorn` (2 replicas — device identity is precious).
- Data 100Gi on `longhorn-single` (1 replica — every file also exists on a device; Longhorn is thin).
- Sync port `192.168.20.217:22000` TCP+UDP via MetalLB. GUI is **not** published — use
  `kubectl -n private port-forward svc/syncthing-gui 8384:8384`.
- NetworkPolicy `allow-syncthing-egress` opens 22000/tcp+udp and relay 22067; discovery uses 443
  which the shared `allow-internet-egress` policy already allows.
- Hub settings baked in: device name `MySweetPea Hub`, default folder path `/var/syncthing/data`,
  default folder versioning `staggered`.
