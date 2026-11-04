"""验证只读HTTP探测和Tempo服务发现，不访问故障主机。"""

import asyncio
import shlex
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import httpx

from app.agents.tool_registry import ToolRegistry
from app.observability.tempo_query import TempoQueryClient, SearchResult


class ProbeHandler(BaseHTTPRequestHandler):
    """提供壳页、业务失败、跳转与超时响应的本地测试服务。"""

    def do_GET(self):
        if self.path == "/slow":
            time.sleep(1.5)
        self.send_response(503 if self.path == "/business" else 302 if self.path == "/redirect" else 200)
        self.send_header("Content-Type", "text/html")
        if self.path == "/redirect":
            self.send_header("Location", "/business")
        self.end_headers()
        try:
            self.wfile.write(b"<html>Application shell</html>" if self.path != "/business" else b"dependency unavailable")
        except BrokenPipeError:
            pass

    def log_message(self, *args):
        pass


class DiagnosticProbeTests(unittest.TestCase):
    """使用真实本地HTTP与模拟远端curl、Tempo验证证据边界。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.registry = ToolRegistry()

    def test_shell_200_is_not_business_health(self):
        result = asyncio.run(self.registry._probe_http(self.base_url + "/"))
        self.assertTrue(result["success"])
        self.assertEqual(result["http_status"], 200)
        self.assertTrue(result["probe_attempted"])
        self.assertIn("Application shell", result["body_excerpt"])
        self.assertFalse(result["health_verified"])
        self.assertEqual(result["requested_url"], self.base_url + "/")

    def test_503_and_redirect_are_not_success(self):
        failed = asyncio.run(self.registry._probe_http(self.base_url + "/business"))
        self.assertFalse(failed["success"])
        self.assertEqual(failed["http_status"], 503)
        self.assertTrue(failed["probe_attempted"])
        self.assertIsNone(failed["return_code"])
        self.assertIsNone(failed["transport_error"])
        redirect = asyncio.run(self.registry._probe_http(self.base_url + "/redirect"))
        self.assertFalse(redirect["success"])
        self.assertEqual(redirect["http_status"], 302)
        self.assertEqual(redirect["redirect_count"], 0)
        self.assertEqual(redirect["redirect_url"], "/business")

    def test_local_timeout_is_transport_failure(self):
        result = asyncio.run(self.registry._probe_http(self.base_url + "/slow", timeout_seconds=1))
        self.assertFalse(result["success"])
        self.assertIsNone(result["return_code"])
        self.assertTrue(result["transport_error"])
        self.assertTrue(result["probe_attempted"])

    def test_url_is_one_quoted_argument_and_ssh_identity_is_validated(self):
        captured = []

        async def execute(command, *args, **kwargs):
            captured.append(shlex.split(command))
            return {"success": False, "requires_confirmation": True, "error": "需确认"}

        self.registry._execute_command = execute
        url = "http://example.test/a?q='; echo INJECTED; #"
        result = asyncio.run(self.registry._probe_http(url, target_host="example-host"))
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(captured[0][-2:], ["--url", url])
        self.assertNotIn("--location", captured[0])
        self.assertFalse(asyncio.run(self.registry._probe_http(url, target_host="host;echo bad"))["success"])
        self.assertFalse(asyncio.run(self.registry._probe_http("file:///etc/passwd"))["success"])
        self.assertFalse(asyncio.run(self.registry._probe_http("http://host/\n--output /tmp/a"))["success"])
        self.assertEqual(len(captured), 1)

    def test_original_security_check_is_not_bypassed(self):
        with patch("app.agents.tool_registry.subprocess.run") as run:
            result = asyncio.run(self.registry._probe_http("http://example.test/$(id)", target_host="example-host"))
        self.assertFalse(result["success"])
        self.assertFalse(result["probe_attempted"])
        run.assert_not_called()

    def test_remote_timeout_keeps_curl_exit_code(self):
        async def execute(command, *args, **kwargs):
            parts = shlex.split(command)
            write_out = parts[parts.index("--write-out") + 1]
            metadata = write_out.replace("\\n", "\n").replace("\\t", "\t")
            for key, value in {"http_code": "000", "time_total": "1.001", "url_effective": "http://example.test/slow",
                               "redirect_url": "", "num_redirects": "0", "content_type": ""}.items():
                metadata = metadata.replace("%{" + key + "}", value)
            return {"success": False, "return_code": 28, "output": metadata + "\ncurl: (28) timeout"}
        self.registry._execute_command = execute
        result = asyncio.run(self.registry._probe_http("http://example.test/slow", target_host="example-host"))
        self.assertFalse(result["success"])
        self.assertEqual(result["return_code"], 28)
        self.assertIn("timeout", result["transport_error"])
        self.assertTrue(result["probe_attempted"])

    def test_invalid_parameters_and_ssh_failure_are_not_http_attempts(self):
        for args in [{"url": "file:///etc/passwd"},
                     {"url": "http://example.test/", "target_host": "host;evil"},
                     {"url": "http://example.test/", "ssh_user": "bad user"},
                     {"url": "http://example.test/", "timeout_seconds": 0}]:
            result = asyncio.run(self.registry._probe_http(**args))
            self.assertFalse(result["probe_attempted"])
            self.assertIsNone(result["http_status"])
        self.registry._execute_command = AsyncMock(return_value={
            "success": False, "return_code": 255, "output": "ssh: connect to host: Connection refused"})
        result = asyncio.run(self.registry._probe_http("http://localhost:8080/business", target_host="unreachable-host"))
        self.assertFalse(result["probe_attempted"])
        self.assertIsNone(result["http_status"])
        self.assertTrue(result["transport_error"])

    def test_tempo_service_discovery_uses_tag_values_api_and_window(self):
        calls = []

        def response(request):
            calls.append(request)
            return httpx.Response(200, json={"tagValues": ["backend-real", "frontend-real"]})

        async def run():
            client = TempoQueryClient()
            client._client = httpx.AsyncClient(base_url="http://tempo", transport=httpx.MockTransport(response))
            try:
                return await client.list_services("30m")
            finally:
                await client.close()

        result = asyncio.run(run())
        self.assertEqual(result["services"], ["backend-real", "frontend-real"])
        self.assertEqual(calls[0].url.path, "/api/search/tag/service.name/values")
        self.assertEqual(int(calls[0].url.params["end"]) - int(calls[0].url.params["start"]), 1800)

    def test_unknown_and_known_empty_service_cannot_imply_health(self):
        with patch.object(TempoQueryClient, "list_services", new=AsyncMock(return_value={"services": ["backend-real"]})), \
             patch.object(TempoQueryClient, "search_error_traces", new=AsyncMock(return_value=SearchResult())) as search:
            unknown = asyncio.run(self.registry._analyze_trace(service_name="guessed-name", error_only=True))
            self.assertFalse(unknown["success"])
            self.assertTrue(unknown["unknown_service"])
            self.assertEqual(unknown["available_services"], ["backend-real"])
            search.assert_not_called()
            empty = asyncio.run(self.registry._analyze_trace(service_name="backend-real", error_only=True))
            self.assertTrue(empty["success"])
            self.assertEqual(empty["verification_status"], "insufficient_evidence")
            self.assertFalse(empty["health_verified"])


if __name__ == "__main__":
    unittest.main()
