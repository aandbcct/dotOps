"""验证真实模式、演示模式、工具注册与执行证据边界。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException

from app.main import app
from app.api import knowledge, multi_agent
from app.core.config import settings
from app.agents.knowledge import KnowledgeExpertAgent
from app.agents.skill_manager import SkillManager
from app.agents.schemas import TopologyInfo
from app.agents.langgraph.nodes.react_agent import _convert_tool_registry_to_langchain, _tool_registry
from app.agents.langgraph import graph as graph_module


class RealDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    """测试不依赖云端模型、真实数据库或测试服务。"""

    async def test_trace_and_rca_real_mode_do_not_generate_mock(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            unavailable = HTTPException(503, detail={"code": "not_connected"})
            with patch.object(settings, "MOCK_ENABLED", False), patch("app.api.tracing.read_traces", side_effect=unavailable), patch("app.api.tracing.read_trace", side_effect=unavailable), patch("app.api.tracing.generate_mock_traces") as mock:
                for method, path in (("GET", "/api/traces/search"), ("GET", "/api/traces/dependency"), ("GET", "/api/traces/" + "a" * 32), ("POST", "/api/traces/analyze"), ("POST", "/api/rca/analyze"), ("GET", "/api/rca/history")):
                    response = await client.request(method, path, **({"json": {}} if method == "POST" else {}))
                    self.assertEqual(response.status_code, 503, path)
                    self.assertEqual(response.json()["detail"]["code"], "not_connected")
                mock.assert_not_called()

    async def test_trace_and_rca_demo_mode_are_labeled(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch.object(settings, "MOCK_ENABLED", True):
                traces = await client.get("/api/traces/search")
                report = await client.post("/api/rca/analyze", json={})
        self.assertEqual(traces.status_code, 200)
        self.assertEqual(traces.json()["source"], "mock_data")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["source"], "mock_data")

    async def test_topology_real_failure_and_no_data_never_fall_back(self):
        agent = KnowledgeExpertAgent()
        with patch.object(settings, "MOCK_ENABLED", False), patch.object(agent, "_get_mock_topology") as mock:
            with patch.object(agent, "_get_neo4j_driver", side_effect=RuntimeError("数据库未连接")):
                self.assertEqual((await agent._query_knowledge_graph("missing"))["source"], "error")
            driver = MagicMock()
            driver.session.return_value.__enter__.return_value.run.return_value.single.return_value = None
            with patch.object(agent, "_get_neo4j_driver", return_value=driver):
                self.assertEqual((await agent._query_knowledge_graph("missing"))["source"], "no_data")
            mock.assert_not_called()

    async def test_topology_demo_is_explicit_and_schema_compatible(self):
        agent = KnowledgeExpertAgent()
        with patch.object(settings, "MOCK_ENABLED", True), patch.object(agent, "_get_neo4j_driver") as driver:
            result = await agent._query_knowledge_graph("order-service")
            driver.assert_not_called()
        self.assertEqual(result["source"], "mock_data")
        self.assertEqual(TopologyInfo(**result).source, "mock_data")

    async def test_rag_real_failure_never_returns_mock_documents(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=SimpleNamespace(status_code=503))
        with patch.object(settings, "MOCK_ENABLED", False), patch.object(knowledge.httpx, "AsyncClient", return_value=client):
            result = await knowledge.query_rag(knowledge.RAGQueryRequest(query="测试"))
        self.assertEqual(result.documents, [])
        self.assertEqual(result.source, "unavailable")

    async def test_rag_demo_is_labeled(self):
        with patch.object(settings, "MOCK_ENABLED", True):
            result = await knowledge.query_rag(knowledge.RAGQueryRequest(query="测试"))
        self.assertEqual(result.source, "mock_data")

    async def test_response_records_actual_nodes_without_claiming_a_fix(self):
        result = {
            "visited_nodes": ["intent_parse", "knowledge_qa", "finalize"],
            "intent_data": {"entities": {}}, "diagnosis_result": None,
            "execution_history": [], "warning_cleared": True,
        }
        with patch.object(multi_agent._graph, "ainvoke", AsyncMock(return_value=result)):
            response = await multi_agent.process_multi_agent_query(multi_agent.MultiAgentRequest(query="测试"), req=None)
        body = json.loads(response.body)
        self.assertFalse(body["warning_cleared"])
        self.assertIn("未执行知识查询", body["stages"]["knowledge_query"]["knowledge_report"])
        self.assertEqual(next(step for step in body["stage_progress"] if step["node"] == "react_agent")["status"], "wait")
        self.assertEqual(next(step for step in body["stage_progress"] if step["node"] == "knowledge_qa")["status"], "finish")

    async def test_graph_records_only_nodes_actually_executed(self):
        with patch.object(graph_module, "intent_parse_node", AsyncMock(return_value={"intent_type": "GENERAL_QA"})), patch.object(
            graph_module, "knowledge_qa_node", AsyncMock(return_value={})
        ), patch.object(graph_module, "finalize_node", AsyncMock(return_value={})):
            graph = graph_module.build_aiops_graph()
            result = await graph.ainvoke(
                {"user_query": "概念问答", "visited_nodes": [], "messages": []},
                config={"configurable": {"thread_id": "node-test"}},
            )
        self.assertEqual(result["visited_nodes"], ["intent_parse", "knowledge_qa", "finalize"])

    def test_retired_probe_is_not_exposed_and_real_tools_remain(self):
        skills = SkillManager()
        self.assertNotIn("test_web_skill", skills.SKILL_REGISTRY)
        self.assertNotIn("probe_test_web", _tool_registry.list_tools())
        definitions = {item["function"]["name"] for item in _tool_registry.get_tools_for_llm()}
        tools = {item.name for item in _convert_tool_registry_to_langchain(_tool_registry)}
        self.assertEqual(tools, definitions)
        self.assertNotIn("probe_test_web", tools)
        self.assertTrue({"execute_command", "mcp_call", "analyze_trace"}.issubset(tools))

if __name__ == "__main__":
    unittest.main()
