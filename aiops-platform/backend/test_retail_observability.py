"""验证真实遥测响应解析、查询参数和页面契约，不连接外部服务。"""

import base64
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.agents.grafana_log_client import GrafanaLogClient
from app.observability.config import GrafanaConfig, get_observability_config
from app.observability.tempo_query import TempoQueryClient
from app.observability.trace_view import dependency_view, trace_detail


def trace_fixture() -> dict:
    """使用 Tempo OTLP JSON 的资源分组和 Base64 标识格式。"""
    def encoded(value: str) -> str:
        return base64.b64encode(bytes.fromhex(value)).decode()

    trace_id = encoded("a" * 32)
    batches = []
    for name, span_id, parent in (("ui", "1" * 16, "3" * 16), ("catalog", "2" * 16, "1" * 16)):
        batches.append({
            "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": name}}]},
            "scopeSpans": [{"spans": [{
                "traceId": trace_id, "spanId": encoded(span_id), "parentSpanId": encoded(parent),
                "name": "GET /catalog", "kind": "SPAN_KIND_SERVER",
                "startTimeUnixNano": "1793625827000000000", "endTimeUnixNano": "1793625827100000000",
                "status": {"code": 2 if name == "catalog" else 0},
            }]}],
        })
    return {"batches": batches}


class RetailObservabilityTests(unittest.IsolatedAsyncioTestCase):
    """覆盖实际接入发现的格式差异及失败边界。"""

    def test_tempo_payload_preserves_ids_errors_and_dependency(self):
        trace = TempoQueryClient()._parse_trace_response(trace_fixture())
        self.assertEqual(trace.trace_id, "a" * 32)
        self.assertEqual(trace.span_count, 2)
        self.assertEqual(trace.root_spans[0].service_name, "ui")
        self.assertTrue(trace.has_errors)
        self.assertEqual(trace_detail(trace)["spans"][1]["parentSpanID"], "1" * 16)
        self.assertIsNone(trace_detail(trace)["spans"][0]["parentSpanID"])
        self.assertEqual(trace_detail(trace)["spans"][0]["externalParentSpanID"], "3" * 16)
        graph = dependency_view([trace], "1h")
        self.assertEqual(graph["edges"][0]["error_rate"], 100)
        self.assertEqual(graph["edges"][0]["source"], "ui")

    def test_resource_spans_payload_is_supported(self):
        payload = {"resourceSpans": trace_fixture()["batches"]}
        self.assertEqual(TempoQueryClient()._parse_trace_response(payload).span_count, 2)

    def test_tempo_query_url_uses_environment(self):
        with patch.dict(os.environ, {"TEMPO_URL": "http://tempo:3200"}, clear=True):
            self.assertEqual(get_observability_config().tempo.query_url, "http://tempo:3200")

    async def test_search_uses_numeric_time_and_traceql_for_errors(self):
        def handler(request):
            self.assertTrue(request.url.params["start"].isdigit())
            self.assertEqual(int(request.url.params["end"]) - int(request.url.params["start"]), 3600)
            self.assertIn('resource.service.name = "orders"', request.url.params["q"])
            self.assertIn("status = error", request.url.params["q"])
            return httpx.Response(200, json={"traces": []})
        async with TempoQueryClient() as client:
            await client._client.aclose()
            client._client = httpx.AsyncClient(base_url="http://tempo", transport=httpx.MockTransport(handler))
            result = await client.search_error_traces(service_name="orders")
            self.assertNotIn("error", result.metrics)

    async def test_grafana_logs_use_epoch_time_and_parse_labels(self):
        def handler(request):
            payload = json.loads(request.content)
            self.assertTrue(payload["from"].isdigit())
            self.assertEqual(int(payload["to"]) - int(payload["from"]), 3600000)
            return httpx.Response(200, json={"results": {"A": {"frames": [{
                "schema": {"fields": [{"name": "labels"}, {"name": "Time"}, {"name": "Line"}]},
                "data": {"values": [[json.dumps({"service_name": "orders"})], [1793625827000], ["test log"]]},
            }]}}})
        async with GrafanaLogClient(GrafanaConfig(url="http://grafana")) as client:
            client._loki_ds_uid = "loki"
            client._client = httpx.AsyncClient(base_url="http://grafana", transport=httpx.MockTransport(handler))
            result = await client.query_logs('{service_name="orders"}', start="1h ago")
        self.assertTrue(result["success"])
        self.assertEqual(result["result_count"], 1)
        self.assertEqual(result["results"][0]["labels"]["service_name"], "orders")

    async def test_grafana_query_error_is_not_reported_as_empty_success(self):
        async with GrafanaLogClient(GrafanaConfig(url="http://grafana")) as client:
            client._loki_ds_uid = "loki"
            client._client = httpx.AsyncClient(base_url="http://grafana", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": {"A": {"error": "invalid query"}}})))
            result = await client.query_logs('{service_name="orders"}')
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
