"""验证真实预构建 ReAct 在网页确认工具后立即结束，而非继续等待邮件。"""

import tempfile
import unittest
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agents import tool_registry
from app.agents.langgraph.nodes import react_agent as react_module
from app.services.approval_service import ApprovalService, approval_session


class ReviewModel(FakeMessagesListChatModel):
    """按预定工具调用返回消息，避免测试访问外部模型。"""

    def bind_tools(self, tools, **kwargs):
        return self


class ReactWebPauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_returns_before_next_model_step(self):
        command = "cd /opt/retail-store && docker ps"
        model = ReviewModel(responses=[
            AIMessage(content="", tool_calls=[{
                "name": "execute_command", "id": "blocked-command", "args": {
                    "command": command, "target_host": "192.168.108.128", "ssh_user": "dotops",
                },
            }]),
            AIMessage(content="", tool_calls=[{
                "name": "ask_user_confirmation", "id": "ask-review", "args": {
                    "command": command, "target_host": "192.168.108.128", "operation": "读取容器状态", "risk": "L2",
                },
            }]),
            AIMessage(content="不应该在审批之前调用第三次模型"),
        ])
        with tempfile.TemporaryDirectory() as directory, patch.object(tool_registry, "approval_service", ApprovalService(directory)), \
                patch.object(react_module, "approval_service", tool_registry.approval_service), \
                patch.object(react_module, "ChatOpenAI", return_value=model), patch.object(react_module, "_react_agent", None):
            token = approval_session.set("pause-test")
            try:
                result = await react_module.react_agent_node({"user_query": "只读检查", "messages": [], "execution_history": []})
            finally:
                approval_session.reset(token)
        self.assertIsNone(result["diagnosis_result"])
        self.assertEqual(result["confirmation_request"]["channel"], "web")
        self.assertEqual(result["execution_history"][-1]["tool"], "ask_user_confirmation")
        self.assertEqual(model.i, 2)


if __name__ == "__main__":
    unittest.main()
