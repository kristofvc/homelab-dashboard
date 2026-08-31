# Homelab Control Center

Read-only status and deployment overview for Kristof's homelab. FastAPI backend,
plain HTML/CSS/JavaScript frontend, one container, no database or frontend build.
Source lives here; Kubernetes desired state lives in `kristofvc/homelab`.

## What it does

- Checks Argo CD, Home Assistant, Authentik, Scruffy, Roberto and Grafana every 30s.
- Distinguishes explicit health endpoints from simple HTTP reachability (a login
  page or 401 is not proof that all application internals work).
- Lists Argo Applications through Kubernetes, including multi-source revisions,
  chart versions, sync/health and the last completed sync operation.
- Retains the last deployment result with a visible warning if refresh fails.
- Keeps 120 checks per service in memory; restarting clears this history.
- Preserves service links including UniFi. Provides read-only details and search.

Last sync is **not** a release history or proof that a particular image is running.
Argo reports desired-state reconciliation, not every application's internal state.
The backend projects a safe subset of Application fields; no Helm values, upstream
response bodies, credentials, raw exception messages or full CRs are returned.

## Local development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8080
```

Real HTTPS service checks run on startup, so you must be on the homelab LAN.
Outside Kubernetes the deployment panel explicitly shows unavailable; no fake data
is generated. Dependencies, including transitive dependencies, are pinned in
`requirements.txt`; `requirements.in` records the top-level dependencies.

## Runtime configuration

- `CHECK_INTERVAL_SECONDS`: defaults to 30, minimum 10; five-second HTTP timeouts.
- `SERVICES_JSON`: optional operator-controlled list of service definitions matching
  `DEFAULT_SERVICES` in app.py. Only trusted config, never accept arbitrary user URLs.
- `ARGO_NAMESPACE`: defaults to `argocd`.
- `SERVICE_ACCOUNT_PATH`: Kubernetes projected token/CA path. Tokens are re-read
  each poll to support rotation. The service account needs only `list` on
  `applications.argoproj.io` in the `argocd` namespace. No Secret access or writes.
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`: optional complete OTLP/HTTP URL ending
  `/v1/traces`. When unset, tracing is a no-op and the UI says so.

The browser only reads cached results; refresh never triggers upstream traffic.
Run **one Uvicorn worker and one replica** for this in-memory design. Background
checks emit JSON logs with trace IDs; no tokens or response bodies are logged.
`/healthz` reports process availability, not upstream application health.
`/metrics` exports service availability, latency and last successful refresh time.

## Tracing

Each refresh creates a `homelab.refresh` span with child spans for each service
check and the Kubernetes Application lookup. This traces work performed **by this
backend**, not browser rendering or the internals of Proxmox/Home Assistant.
The latest trace ID appears in the UI and refresh logs when tracing is enabled.

Tempo and an Alloy OTLP receiver are not installed by this source repo. Before
enabling trace export, provision them via homelab GitOps, restrict receiver access,
and connect Tempo to Grafana. Do not point this setting at an external collector
without reviewing the exported metadata. Do not claim tracing is live until a
trace can be queried from Tempo.

## Container delivery and security

CI runs tests, then publishes `ghcr.io/kristofvc/homelab-dashboard:sha-<full-commit>`
for main commits. Pull requests only run tests. Package visibility stays private
unless explicitly changed. No deployment credentials are stored in this repo.
The container runs as UID 10001 and supports a read-only filesystem.

The GitOps repo contains an **inactive** replacement deployment under
`apps/homelab-control-center`. Activate only after CI succeeds and image pulls are
configured. The old static dashboard remains live until that deliberate cutover.
Prefer a digest or immutable commit tag for deployment; do not deploy `latest`.

There is no application login in v1: it inherits the dashboard's LAN-only access.
It reveals internal service/deployment metadata. Do not expose it on the internet
without an authentication boundary. Adding Authentik protection is a separate
step; links to authenticated services do not authenticate this dashboard.
