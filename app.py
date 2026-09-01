"""Read-only homelab status API. No credentials or upstream bodies leave this process."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import ssl
import time
from urllib.parse import quote, urlparse

import httpx
import pyroscope
from pyroscope.otel import PyroscopeSpanProcessor
from node_metrics import fetch_nodes
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

ROOT = Path(__file__).parent
INTERVAL = max(10, int(os.getenv("CHECK_INTERVAL_SECONDS", "30")))
SA = Path(os.getenv("SERVICE_ACCOUNT_PATH", "/var/run/secrets/kubernetes.io/serviceaccount"))
ARGO_NAMESPACE = os.getenv("ARGO_NAMESPACE", "argocd")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")
if not re.fullmatch(r"[a-z0-9-]+", ARGO_NAMESPACE):
    raise ValueError("Invalid Argo namespace")
DEFAULT_SERVICES = [
    {"id": "argocd", "name": "Argo CD", "url": "https://argocd.homelab.kristofvc.be", "path": "/healthz", "probe": "health"},
    {"id": "home-assistant", "name": "Home Assistant", "url": "https://homeassistant.homelab.kristofvc.be", "path": "/", "probe": "reachability"},
    {"id": "authentik", "name": "Authentik", "url": "https://auth.homelab.kristofvc.be", "path": "/-/health/ready/", "probe": "health"},
    {"id": "scruffy", "name": "Proxmox · Scruffy", "url": "https://scruffy.homelab.kristofvc.be", "path": "/", "probe": "reachability"},
    {"id": "roberto", "name": "Proxmox · Roberto", "url": "https://roberto.homelab.kristofvc.be", "path": "/", "probe": "reachability"},
    {"id": "grafana", "name": "Grafana", "url": "https://grafana.homelab.kristofvc.be", "path": "/api/health", "probe": "health"},
]
SERVICES = json.loads(os.getenv("SERVICES_JSON", json.dumps(DEFAULT_SERVICES)))
for service in SERVICES:
    parsed = urlparse(service["url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Service URLs must be credential-free HTTPS origins")
    if not service.get("path", "/").startswith("/") or service.get("probe") not in ("health", "reachability"):
        raise ValueError("Invalid service probe")

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
tracer = trace.get_tracer("homelab-dashboard")
duration = Histogram("homelab_check_duration_seconds", "Service probe duration", ["service"])
available = Gauge("homelab_service_available", "Probe success; see probe type for semantics", ["service"])
last_refresh = Gauge("homelab_last_refresh_timestamp_seconds", "Last completed refresh")
history = {s["id"]: deque(maxlen=120) for s in SERVICES}
snapshot = {"checked_at": None, "services": [], "deployments": [], "deployments_checked_at": None, "deployments_error": "Not checked yet", "trace_id": None,
            "nodes": [], "nodes_checked_at": None, "nodes_error": "Not checked yet"}


def now():
    return datetime.now(timezone.utc).isoformat()


def commit_url(repo, revision):
    """Only known GitHub URLs and full commit hashes become clickable links."""
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", repo or "")
    if match and re.fullmatch(r"[0-9a-fA-F]{40}", revision or ""):
        return f"https://github.com/{match[1]}/commit/{revision}"
    return None


def summarize_application(item):
    status, spec = item.get("status", {}), item.get("spec", {})
    sources = spec.get("sources") or [spec.get("source", {})]
    sync = status.get("sync", {})
    revisions = sync.get("revisions") or [sync.get("revision")]
    operation = status.get("operationState", {})
    name = item["metadata"]["name"]
    return {
        "name": name,
        "namespace": spec.get("destination", {}).get("namespace"),
        "sync": sync.get("status", "Unknown"),
        "health": status.get("health", {}).get("status", "Unknown"),
        "last_sync": operation.get("finishedAt"),
        "operation": operation.get("phase", "Unknown"),
        "argocd_url": f"https://argocd.homelab.kristofvc.be/applications/{ARGO_NAMESPACE}/{quote(name, safe='')}",
        "sources": [{"chart": s.get("chart"), "revision": revisions[i] if i < len(revisions) else None,
                     "desired": s.get("targetRevision"),
                     "commit_url": commit_url(s.get("repoURL"), revisions[i] if i < len(revisions) else None)}
                    for i, s in enumerate(sources)],
    }


async def check_service(client, service):
    start = time.monotonic()
    result = {"id": service["id"], "name": service["name"], "url": service["url"], "probe": service["probe"], "checked_at": now()}
    with tracer.start_as_current_span("service.check", attributes={"service.id": service["id"], "probe.type": service["probe"]}) as span:
        try:
            # Read headers only. Bodies may be large or contain sensitive data.
            async with client.stream("GET", service["url"].rstrip("/") + service["path"]) as response:
                code = response.status_code
            ok = 200 <= code < 300 if service["probe"] == "health" else 200 <= code < 400 or code in (401, 403)
            result.update(status=("healthy" if service["probe"] == "health" else "reachable") if ok else "unhealthy", http_status=code)
            span.set_attribute("http.response.status_code", code)
        except (httpx.HTTPError, OSError) as exc:
            ok = False
            result.update(status="unreachable", error=type(exc).__name__)
        result["latency_ms"] = round((time.monotonic() - start) * 1000)
        available.labels(service["id"]).set(int(ok))
        duration.labels(service["id"]).observe(result["latency_ms"] / 1000)
        if not ok:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "Probe failed"))
        return result


async def fetch_deployments(client):
    # Read on each poll: projected service-account tokens rotate.
    token = (SA / "token").read_text().strip()
    with tracer.start_as_current_span("argocd.applications.list"):
        response = await client.get(f"https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/{ARGO_NAMESPACE}/applications", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return sorted([summarize_application(x) for x in response.json()["items"]], key=lambda x: x["name"])


async def refresh(client, kube_client):
    global snapshot
    with tracer.start_as_current_span("homelab.refresh") as span:
        services = await asyncio.gather(*(check_service(client, s) for s in SERVICES))
        deployments = snapshot["deployments"]
        deployments_at = snapshot["deployments_checked_at"]
        error = None
        try:
            if kube_client is None:
                error = "Kubernetes access not configured"
            else:
                deployments = await fetch_deployments(kube_client)
                deployments_at = now()
        except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
            error = f"Deployment refresh failed ({type(exc).__name__}); previous results retained"
            span.set_status(trace.Status(trace.StatusCode.ERROR, "Deployment refresh failed"))
        for result in services:
            history[result["id"]].append(dict(result))
        nodes, nodes_at = snapshot.get("nodes", []), snapshot.get("nodes_checked_at")
        nodes_error = None
        try:
            if not PROMETHEUS_URL:
                nodes_error = "Prometheus niet geconfigureerd"
            else:
                with tracer.start_as_current_span("prometheus.nodes"):
                    nodes = await fetch_nodes(client, PROMETHEUS_URL)
                nodes_at = now()
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            nodes_error = f"Nodemeting mislukt ({type(exc).__name__}); vorige meting behouden"
        context = span.get_span_context()
        snapshot = {"checked_at": now(), "services": services, "deployments": deployments,
                    "deployments_checked_at": deployments_at, "deployments_error": error,
                    "nodes": nodes, "nodes_checked_at": nodes_at, "nodes_error": nodes_error,
                    "trace_id": format(context.trace_id, "032x") if context.is_valid else None}
        last_refresh.set(time.time())
        logging.info(json.dumps({"event": "refresh", "services": len(services), "failed": sum(s["status"] in ("unhealthy", "unreachable") for s in services), "deployment_error": bool(error), "trace_id": snapshot["trace_id"]}))


@asynccontextmanager
async def lifespan(app):
    provider = None
    profiler = False
    pyroscope_url = os.getenv("PYROSCOPE_SERVER_ADDRESS")
    if pyroscope_url:
        try:
            pyroscope.configure(
                application_name="homelab-dashboard",
                server_address=pyroscope_url,
                sample_rate=100,
                cpu_enabled=True,
                mem_enabled=True,
                mem_heap_sample_size=512 * 1024,
                enable_logging=False,
                tags={"cluster": "homelab", "namespace": "homelab-dashboard"},
            )
            profiler = True
        except Exception as exc:
            logging.error(json.dumps({"event": "profiler_start_failed", "error": type(exc).__name__}))
    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        provider = TracerProvider(resource=Resource.create({"service.name": "homelab-dashboard"}))
        if profiler:
            provider.add_span_processor(PyroscopeSpanProcessor())
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(timeout=5)))
        trace.set_tracer_provider(provider)
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        kube = None
        if (SA / "ca.crt").exists():
            kube = httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(SA / "ca.crt")), timeout=5, trust_env=False)
        async def poll():
            while True:
                try:
                    await refresh(client, kube)
                except Exception as exc:
                    logging.error(json.dumps({"event": "refresh_failed", "error": type(exc).__name__}))
                await asyncio.sleep(INTERVAL)
        task = asyncio.create_task(poll())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if kube:
                await kube.aclose()
            if provider:
                await asyncio.to_thread(provider.shutdown)
            if profiler:
                await asyncio.to_thread(pyroscope.shutdown)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
    return response


@app.get("/healthz")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def status():
    return {**snapshot, "interval_seconds": INTERVAL}


@app.get("/api/services/{service_id}")
async def service_detail(service_id: str):
    if service_id not in history:
        raise HTTPException(404)
    return {"history": list(history[service_id])}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.get("/")
async def index():
    return FileResponse(ROOT / "static/index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
