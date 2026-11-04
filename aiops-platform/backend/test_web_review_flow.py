"""验证同一诊断检查点的网页批准、拒绝、恢复和重复提交。"""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.main import app
from app.api import multi_agent, approval as approval_api
from app.api.auth import get_current_user, require_admin
from app.agents import tool_registry
from app.agents.langgraph import graph as graph_module
from app.agents.langgraph.nodes.react_agent import _tool_registry
from app.services.approval_service import ApprovalService


class WebReviewFlowTests(unittest.IsolatedAsyncioTestCase):
    """模型与远端命令替身只负责可控输入，真实图、状态存储和 API 完整运行。"""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = ApprovalService(self.directory.name)
        self.user = SimpleNamespace(username="review-admin", is_admin=True)
        self.command = "cd /opt/retail-store && docker ps"
        self.execute = AsyncMock(return_value={
            "success": True, "return_code": 0, "output": "catalog running",
            "command": self.command, "target_host": "192.168.108.128",
        })
        self.patches = [
            patch.object(multi_agent, "approval_service", self.service),
            patch.object(approval_api, "approval_service", self.service),
            patch.object(tool_registry, "approval_service", self.service),
            patch.object(_tool_registry, "_execute_direct", self.execute),
            patch.object(graph_module, "intent_parse_node", self.parse),
            patch.object(graph_module, "skill_match_node", AsyncMock(return_value={"matched_skills": []})),
            patch.object(graph_module, "react_agent_node", self.react),
        ]
        for item in self.patches:
            item.start()
        self.graph_patch = patch.object(multi_agent, "_graph", graph_module.build_aiops_graph())
        self.graph_patch.start()
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[require_admin] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        self.graph_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def parse(self, state):
        entities = {"servers": [{"value": "192.168.108.128"}],
                    "ssh_users": [] if "缺用户名" in state["user_query"] else [{"value": "dotops"}]}
        return {"intent_type": "DIAGNOSE", "intent_data": {"entities": entities}, "entities": entities}

    async def react(self, state):
        if state.get("approval_status") == "executed":
            return {"diagnosis_result": {"is_final": True, "root_cause": "已取得容器状态，未声称业务恢复。"}, "confirmation_request": None}
        blocked = await _tool_registry.execute("execute_command", command=self.command,
                                               target_host="192.168.108.128", ssh_user="dotops")
        self.assertTrue(blocked["requires_confirmation"])
        approval = await _tool_registry.execute("ask_user_confirmation", operation="读取容器状态", risk="L2",
                                                impact="仅查看", command=self.command, target_host="192.168.108.128")
        self.assertTrue(approval["success"])
        return {"confirmation_request": approval, "approval_status": None, "diagnosis_result": None,
                "execution_history": state.get("execution_history", []) + [{"tool": "ask_user_confirmation", "result": approval}],
                "iteration_count": state.get("iteration_count", 0) + 1}

    async def start(self, query="网页500", session="web-flow"):
        response = await self.client.post("/api/multi-agent/process", json={"query": query, "session_id": session})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def review(self, body, approved=True, **extra):
        return await self.client.post(f"/api/multi-agent/sessions/{body['session_id']}/review", json={
            "approval_id": body["review"]["approval_id"], "approved": approved, **extra,
        })

    async def test_approve_continues_same_task_and_replay_does_not_execute_again(self):
        body = await self.start()
        self.assertEqual(body["confirmation_kind"], "command_approval")
        self.assertTrue(body["execution_result"]["requires_approval"])
        self.assertIn("等待页面", body["execution_result"]["execution_note"])
        self.execute.assert_not_awaited()
        restored = await self.client.get("/api/multi-agent/sessions/web-flow")
        self.assertEqual(restored.json()["review"]["approval_id"], body["review"]["approval_id"])
        response = await self.review(body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["session_id"], body["session_id"])
        self.assertIsNone(result["confirmation_kind"])
        self.assertEqual(result["review"]["execution_status"], "succeeded")
        self.assertFalse(result["warning_cleared"])
        self.execute.assert_awaited_once_with(self.command, "192.168.108.128", "dotops")
        self.assertEqual((await self.review(body)).status_code, 200)
        self.assertEqual(self.execute.await_count, 1)

    async def test_reject_is_terminal_and_cannot_be_reversed(self):
        body = await self.start()
        result = await self.review(body, False, reason="先不执行")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["stages"]["dynamic_execution"]["status"], "rejected")
        self.execute.assert_not_awaited()
        self.assertEqual((await self.review(body, True)).status_code, 409)

    async def test_saved_decision_can_resume_after_interrupted_checkpoint_update(self):
        body = await self.start()
        self.service.decide(body["review"]["approval_id"], body["session_id"], True, self.user.username)
        result = await self.review(body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["review"]["execution_status"], "succeeded")
        self.execute.assert_awaited_once()

    async def test_stream_and_legacy_entrypoints_share_session_guard(self):
        await self.start()
        for endpoint in ("/api/multi-agent/process/stream", "/api/multi-agent-lg/process", "/api/multi-agent-lg/process/stream"):
            response = await self.client.post(endpoint, json={"query": "不能覆盖会话", "session_id": "web-flow"})
            self.assertEqual(response.status_code, 409, response.text)
        app.dependency_overrides.clear()
        response = await self.client.post("/api/multi-agent/process/stream", json={"query": "检查"})
        self.assertEqual(response.status_code, 401)

    async def test_stream_result_contains_structured_session_response(self):
        response = await self.client.post("/api/multi-agent/process/stream", json={"query": "只读检查", "session_id": "stream-test"})
        self.assertEqual(response.status_code, 200, response.text)
        event = json.loads(response.text.removeprefix("data: ").strip())
        self.assertEqual(event["data"]["session_id"], "stream-test")
        self.assertEqual(event["data"]["confirmation_kind"], "command_approval")

    async def test_failed_command_finishes_without_reporting_success(self):
        self.execute.return_value = {"success": False, "error": "SSH 连接失败", "return_code": 255}
        body = await self.start()
        response = await self.review(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["stages"]["dynamic_execution"]["status"], "failed")
        self.assertEqual(response.json()["review"]["execution_status"], "failed")
        self.assertEqual((await self.review(body)).status_code, 200)
        self.assertEqual(self.execute.await_count, 1)

    async def test_cannot_replace_command_or_approve_foreign_session(self):
        body = await self.start()
        self.assertEqual((await self.review(body, command="reboot")).status_code, 422)
        other = await self.start(session="other-flow")
        response = await self.client.post("/api/multi-agent/sessions/other-flow/review", json={
            "approval_id": body["review"]["approval_id"], "approved": True,
        })
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(other["review"]["approval_id"], body["review"]["approval_id"])
        self.execute.assert_not_awaited()

    async def test_ssh_information_resumes_before_command_review(self):
        body = await self.start(query="缺用户名")
        self.assertEqual(body["confirmation_kind"], "ssh_user")
        self.assertIsNone(body["review"])
        response = await self.client.post("/api/multi-agent/sessions/web-flow/ssh", json={"ssh_user": "dotops"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["confirmation_kind"], "command_approval")
        self.assertEqual((await self.review(body)).status_code, 200)
        self.execute.assert_awaited_once()

    async def test_restart_has_no_checkpoint_and_does_not_replay(self):
        body = await self.start()
        with patch.object(multi_agent, "_graph", graph_module.build_aiops_graph()):
            self.assertEqual((await self.client.get("/api/multi-agent/sessions/web-flow")).status_code, 404)
            self.assertEqual((await self.review(body)).status_code, 404)
        self.execute.assert_not_awaited()

    async def test_legacy_endpoints_cannot_bypass_web_review(self):
        body = await self.start()
        approval_id = body["review"]["approval_id"]
        for prefix in ("/api/approval/approve/", "/api/multi-agent/approve/"):
            response = await self.client.post(prefix + approval_id)
            self.assertEqual(response.status_code, 409, response.text)
        self.execute.assert_not_awaited()

    async def test_web_review_requires_login_and_admin(self):
        body = await self.start()
        app.dependency_overrides.clear()
        self.assertEqual((await self.client.get("/api/multi-agent/sessions/web-flow")).status_code, 401)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(username="viewer", is_admin=False)
        self.assertEqual((await self.client.get("/api/multi-agent/sessions/web-flow")).status_code, 403)
        self.assertEqual((await self.review(body)).status_code, 403)
        self.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
