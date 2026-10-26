"""验证流式对话对空候选块和空正文的处理。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api import ai_chat
from app.services.session_manager import SessionManager


class ChatStreamTests(unittest.IsolatedAsyncioTestCase):
    """通过接口生成器检查事件顺序与会话消息状态。"""

    async def collect_events(self, chunks):
        async def upstream():
            for chunk in chunks:
                yield chunk

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=upstream()))
            )
        )
        manager = SessionManager()
        with patch.object(ai_chat, "get_openai_client", AsyncMock(return_value=client)), patch.object(
            ai_chat, "session_manager", manager
        ):
            response = await ai_chat.chat_stream(
                ai_chat.ChatRequest(message="你好"), req=None, _rate_limit=None
            )
            events = [
                json.loads(frame.removeprefix("data: ").strip())
                async for frame in response.body_iterator
            ]
        session = await manager.get_session(events[0]["session_id"])
        return events, session

    def content_chunk(self, content):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content))]
        )

    async def test_metadata_chunks_do_not_break_answer_or_persistence(self):
        events, session = await self.collect_events(
            [
                SimpleNamespace(choices=[]),
                self.content_chunk(None),
                self.content_chunk("你"),
                self.content_chunk("好"),
                SimpleNamespace(choices=[]),
            ]
        )
        self.assertEqual([event["type"] for event in events], ["session", "content", "content", "done"])
        self.assertEqual(events[-1]["message_count"], 2)
        self.assertEqual(session.messages[-1].role, "assistant")
        self.assertEqual(session.messages[-1].content, "你好")

    async def test_empty_stream_reports_error_without_saving_empty_answer(self):
        events, session = await self.collect_events(
            [SimpleNamespace(choices=[]), self.content_chunk(None)]
        )
        self.assertEqual([event["type"] for event in events], ["session", "error"])
        self.assertIn("未返回有效的文本内容", events[-1]["error"])
        self.assertEqual([message.role for message in session.messages], ["user"])

    async def test_plain_stream_still_completes(self):
        events, session = await self.collect_events([self.content_chunk("正常回答")])
        self.assertEqual([event["type"] for event in events], ["session", "content", "done"])
        self.assertEqual(session.messages[-1].content, "正常回答")


if __name__ == "__main__":
    unittest.main()
