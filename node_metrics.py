"""Fixed, read-only Prometheus queries; never accept browser-supplied queries."""
import asyncio
import math

JOIN = ' * on(instance) group_left(nodename) node_uname_info{job="node-exporter"}'
UP = ' and on(instance) (up{job="node-exporter"} == 1)'
QUERIES = {
    "ready": 'max by(node) (kube_node_status_condition{job="kube-state-metrics",condition="Ready",status="true"} and on(instance) (up{job="kube-state-metrics"} == 1))',
    "cpu_percent": '(100 * (1 - avg by(instance) (rate(node_cpu_seconds_total{job="node-exporter",mode="idle"}[5m])))' + UP + ')' + JOIN,
    "memory_percent": '(100 * (1 - node_memory_MemAvailable_bytes{job="node-exporter"} / node_memory_MemTotal_bytes{job="node-exporter"})' + UP + ')' + JOIN,
}


async def fetch_nodes(client, base_url):
    async def query(key, expression):
        response = await client.get(base_url.rstrip("/") + "/api/v1/query", params={
            "query": expression, "timeout": "3s", "lookback_delta": "120s",
        })
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success" or payload["data"]["resultType"] != "vector":
            raise ValueError("Invalid Prometheus response")
        result = {}
        for sample in payload["data"]["result"]:
            name = sample["metric"].get("node") or sample["metric"].get("nodename")
            value = float(sample["value"][1])
            if name and math.isfinite(value):
                result[name] = (value == 1 if key == "ready" else round(min(100, max(0, value)), 1))
        return result

    ready, cpu, memory = await asyncio.gather(*(query(k, v) for k, v in QUERIES.items()))
    names = sorted(ready.keys() | cpu.keys() | memory.keys())
    if not names:
        raise ValueError("No node metrics available")
    return [{"name": name, "ready": ready.get(name), "cpu_percent": cpu.get(name),
             "memory_percent": memory.get(name)} for name in names]
