# Reserve-start sidecar

Activates reserved (`on_hold`) services at first connect and serves early
activation requests (`POST /api/user/*/reserve-start`, routed to its socket by
the reverse proxy). This is the production sidecar, release 2026.10.02.1,
rebased onto the Hexogate panel image so it no longer needs a copied runtime.

Build and run from this directory:

```bash
docker build -t hexogate-panel-reserve-start:local .
docker compose -f compose.yaml up -d
```

`compose.yaml` reuses the panel's `.env` and expects the paths production uses
today (`/var/lib/pasarguard/...`). Adjust the socket and volume paths there if
your panel runs under `/var/lib/hexogate`; the panel socket path must match
`UVICORN_UDS` in the panel's `.env`.
