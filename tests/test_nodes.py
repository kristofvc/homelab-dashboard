import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import app
from node_metrics import fetch_nodes, QUERIES


class NodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_missing_and_nonfinite_metrics(self):
        def respond(request):
            query = request.url.params["query"]
            self.assertEqual(request.url.params["lookback_delta"], "120s")
            if query == QUERIES["ready"]:
                values = [("node", "calculon", "1"), ("node", "flexo", "0")]
            elif query == QUERIES["cpu_percent"]:
                values = [("nodename", "calculon", "12.345"), ("nodename", "flexo", "NaN")]
            else:
                values = [("nodename", "calculon", "80.12")]
            return httpx.Response(200, json={"status":"success", "data":{
                "resultType":"vector", "result":[{"metric":{label:name,"secret":"DO-NOT-LEAK"},
                "value":[1,value]} for label,name,value in values]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            nodes = await fetch_nodes(client, "http://prometheus")
        self.assertEqual(nodes, [
            {"name":"calculon","ready":True,"cpu_percent":12.3,"memory_percent":80.1},
            {"name":"flexo","ready":False,"cpu_percent":None,"memory_percent":None}])
        self.assertNotIn("DO-NOT-LEAK", json.dumps(nodes))

    async def test_empty_or_invalid_result_is_not_zero_usage(self):
        for payload in [{"status":"error"}, {"status":"success","data":{"resultType":"vector","result":[]}}]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json=payload))) as client:
                with self.assertRaises(ValueError):
                    await fetch_nodes(client,"http://prometheus")

    async def test_failed_poll_retains_values_and_timestamp(self):
        old = {**app.snapshot,"nodes":[{"name":"calculon"}],"nodes_checked_at":"old"}
        with patch.object(app,"snapshot",old), patch.object(app,"PROMETHEUS_URL","http://prometheus"), patch.object(app,"fetch_nodes",AsyncMock(side_effect=httpx.ReadTimeout("SECRET"))):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
                await app.refresh(client,None)
            self.assertEqual(app.snapshot["nodes"],old["nodes"])
            self.assertEqual(app.snapshot["nodes_checked_at"],"old")
            self.assertIn("ReadTimeout",app.snapshot["nodes_error"])
            self.assertNotIn("SECRET",json.dumps(app.snapshot))
