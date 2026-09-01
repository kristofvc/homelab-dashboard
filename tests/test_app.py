import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
import app


class ProjectionTests(unittest.TestCase):
    def test_commit_link(self):
        sha = "a" * 40
        self.assertEqual(app.commit_url("git@github.com:kristofvc/homelab.git", sha), "https://github.com/kristofvc/homelab/commit/" + sha)
        self.assertIsNone(app.commit_url("javascript:alert(1)", sha))
        self.assertIsNone(app.commit_url("https://github.com/a/b", "main"))

    def test_multi_source_and_no_secret_leak(self):
        item = {"metadata": {"name": "monitoring"}, "spec": {"sources": [{"chart": "stack", "targetRevision": "1.2.3", "helm": {"parameters": [{"value": "DO-NOT-LEAK"}]}}, {"repoURL": "git@github.com:kristofvc/homelab.git"}]}, "status": {"sync": {"status": "Synced", "revisions": ["1.2.3", "b" * 40]}, "health": {"status": "Healthy"}, "operationState": {"phase": "Succeeded", "finishedAt": "2026-08-31T12:00:00Z", "message": "DO-NOT-LEAK"}}}
        out = app.summarize_application(item)
        self.assertEqual(len(out["sources"]), 2)
        self.assertEqual(out["sources"][0]["revision"], "1.2.3")
        self.assertNotIn("DO-NOT-LEAK", json.dumps(out))
        self.assertEqual(out["last_sync"], "2026-08-31T12:00:00Z")

    def test_missing_status_is_unknown(self):
        out = app.summarize_application({"metadata": {"name": "new"}})
        self.assertEqual(out["health"], "Unknown")
        self.assertIsNone(out["last_sync"])


class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, code, kind="health"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(code))) as client:
            return await app.check_service(client, {"id": "test", "name": "Test", "url": "https://example.com", "path": "/healthz", "probe": kind})

    async def test_health_and_reachability_are_distinct(self):
        self.assertEqual((await self.probe(200))["status"], "healthy")
        self.assertEqual((await self.probe(401))["status"], "unhealthy")
        self.assertEqual((await self.probe(401, "reachability"))["status"], "reachable")
        self.assertEqual((await self.probe(302))["status"], "unhealthy")
        self.assertEqual((await self.probe(503, "reachability"))["status"], "unhealthy")

    async def test_timeout_is_sanitized(self):
        def fail(request):
            raise httpx.ReadTimeout("SECRET-UPSTREAM-DETAIL")
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            out = await app.check_service(client, app.SERVICES[0])
        self.assertEqual(out["status"], "unreachable")
        self.assertNotIn("SECRET", json.dumps(out))

    async def test_distinct_probe_url_keeps_display_link(self):
        requested = []
        def respond(request):
            requested.append(str(request.url))
            return httpx.Response(200)
        service = {"id": "unifi", "name": "UniFi", "url": "https://unifi", "probe_url": "https://home.kristofvc.be", "path": "/", "probe": "reachability"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            out = await app.check_service(client, service)
        self.assertEqual(requested, ["https://home.kristofvc.be/"])
        self.assertEqual(out["url"], "https://unifi")
        self.assertEqual(out["status"], "reachable")

    async def test_stale_deployments_are_retained(self):
        original = app.snapshot
        app.snapshot = {**original, "deployments": [{"name": "previous"}], "deployments_checked_at": "old"}
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
                with patch.object(app, "fetch_deployments", AsyncMock(side_effect=OSError("SECRET"))):
                    await app.refresh(client, client)
            self.assertEqual(app.snapshot["deployments"], [{"name": "previous"}])
            self.assertEqual(app.snapshot["deployments_checked_at"], "old")
            self.assertNotIn("SECRET", json.dumps(app.snapshot))
        finally:
            app.snapshot = original


class RouteTests(unittest.TestCase):
    def setUp(self):
        # No lifespan: tests must never make real network requests.
        self.client = TestClient(app.app)

    def tearDown(self):
        self.client.close()

    def test_api_cache_and_security_headers(self):
        with patch.object(app, "refresh") as refresh:
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        refresh.assert_not_called()

    def test_static_health_metrics_and_unknown_service(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        avatar = self.client.get("/static/momcorp.png")
        self.assertEqual(avatar.status_code, 200)
        self.assertEqual(avatar.headers["content-type"], "image/png")
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertIn("homelab_check_duration_seconds", self.client.get("/metrics").text)
        self.assertEqual(self.client.get("/api/services/arbitrary-url").status_code, 404)
        self.assertEqual(self.client.post("/api/status").status_code, 405)

    def test_profiling_is_opt_in(self):
        self.assertIsNone(app.os.getenv("PYROSCOPE_SERVER_ADDRESS"))


if __name__ == "__main__":
    unittest.main()
