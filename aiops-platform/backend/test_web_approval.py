"""网页审批的会话隔离、不可反转决定与执行幂等验证。"""

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from unittest.mock import MagicMock

from app.core.config import settings
from app.services.approval_service import ApprovalService, approval_session
from app.agents.tool_registry import ToolRegistry


class WebApprovalTests(unittest.TestCase):
    """使用临时目录与模拟执行器验证审批，不连接实际主机。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.service = ApprovalService(self.directory.name)
        self.channel = patch.object(settings, "APPROVAL_CHANNEL", "web")
        self.channel.start()
        self.binding = patch("app.agents.tool_registry.approval_service", self.service)
        self.binding.start()
        self.token = approval_session.set("session-a")
        self.registry = ToolRegistry()

    def tearDown(self):
        approval_session.reset(self.token)
        self.binding.stop()
        self.channel.stop()
        self.directory.cleanup()

    def create(self):
        return self.service.create("重启服务", "L2", "短暂中断", "systemctl restart nginx", "test-host", "operator")

    def test_web_without_smtp_and_original_ssh_user(self):
        async def run():
            await self.registry._execute_command("systemctl restart nginx", "test-host", ssh_user="operator")
            result = await self.registry._ask_user_confirmation("重启服务", "L2")
            self.assertTrue(result["success"])
            self.assertEqual(result["channel"], "web")
            self.assertEqual(result["ssh_user"], "operator")
            self.assertEqual(result["commands"], ["systemctl restart nginx"])
        with patch.object(settings, "SMTP_USER", ""), patch.object(settings, "SMTP_PASSWORD", ""):
            asyncio.run(run())

    def test_decision_is_immutable_and_session_scoped(self):
        record = self.create()
        aid = record["approval_id"]
        with self.assertRaises(PermissionError):
            self.service.decide(aid, "session-b", True, "other")
        approved = self.service.decide(aid, "session-a", True, "reviewer")
        self.assertEqual(self.service.decide(aid, "session-a", True, "other"), approved)
        with self.assertRaises(ValueError):
            self.service.decide(aid, "session-a", False, "other")
        self.assertEqual(ApprovalService(self.directory.name).get(aid), approved)

    def test_pending_command_identity_cannot_be_replaced(self):
        async def run():
            await self.registry._execute_command("systemctl restart nginx", "test-host", ssh_user="operator")
            for override in [{"target_host": "other-host"}, {"ssh_user": "root"}, {"command": "systemctl restart mysql"}]:
                result = await self.registry._ask_user_confirmation("重启", "L2", **override)
                self.assertFalse(result["success"])
            self.assertEqual(self.service.get_pending("session-a"), [])
        asyncio.run(run())

    def test_pending_rejected_and_wrong_session_never_execute(self):
        record = self.create()
        execution = AsyncMock()
        self.registry._execute_direct = execution
        self.assertFalse(asyncio.run(self.registry._execute_approved_command(record["approval_id"]))["executed"])
        self.service.decide(record["approval_id"], "session-a", False, "reviewer")
        self.assertFalse(asyncio.run(self.registry._execute_approved_command(record["approval_id"]))["success"])
        token = approval_session.set("session-b")
        try:
            self.assertFalse(asyncio.run(self.registry._check_approval_status(record["approval_id"]))["success"])
        finally:
            approval_session.reset(token)
        execution.assert_not_called()

    def test_success_and_failure_execute_only_once(self):
        for successful in [True, False]:
            record = self.create()
            aid = record["approval_id"]
            self.service.decide(aid, "session-a", True, "reviewer")
            execution = AsyncMock(return_value={"success": successful, "output": "模拟输出"})
            self.registry._execute_direct = execution
            first = asyncio.run(self.registry._execute_approved_command(aid))
            again = asyncio.run(self.registry._execute_approved_command(aid))
            self.assertEqual(first["success"], successful)
            self.assertTrue(again["replayed"])
            execution.assert_awaited_once_with("systemctl restart nginx", "test-host", "operator")
            self.assertEqual(self.service.get(aid)["execution_status"], "succeeded" if successful else "failed")

    def test_claim_is_durable_and_exclusive(self):
        aid = self.create()["approval_id"]
        self.service.decide(aid, "session-a", True, "reviewer")
        self.assertTrue(self.service.claim_execution(aid, "session-a")[1])
        self.assertFalse(self.service.claim_execution(aid, "session-a")[1])
        self.assertFalse(ApprovalService(self.directory.name).claim_execution(aid, "session-a")[1])

    def test_concurrent_claim_has_one_winner(self):
        aid = self.create()["approval_id"]
        self.service.decide(aid, "session-a", True, "reviewer")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: self.service.claim_execution(aid, "session-a")[1], range(16)))
        self.assertEqual(sum(claims), 1)

    def test_security_recheck_blocks_dangerous_command_and_replays_failure(self):
        record = self.service.create("删除", "L2", "影响", "rm -rf /", "test-host", "operator")
        aid = record["approval_id"]
        self.service.decide(aid, "session-a", True, "reviewer")
        execution = AsyncMock()
        self.registry._execute_direct = execution
        result = asyncio.run(self.registry._execute_approved_command(aid))
        self.assertFalse(result["success"])
        execution.assert_not_called()
        self.assertTrue(asyncio.run(self.registry._execute_approved_command(aid))["replayed"])

    def test_email_strategy_shares_decision_record(self):
        from app.utils.email_sender import ApprovalManager
        with patch.object(settings, "APPROVAL_CHANNEL", "email"):
            record = self.create()
        self.assertEqual(record["channel"], "email")
        with patch("app.utils.email_sender.approval_service", self.service):
            manager = ApprovalManager(self.directory.name)
            manager.approve(record["approval_id"], "email_reply")
            self.assertEqual(self.service.get(record["approval_id"])["status"], "approved")
            with self.assertRaises(ValueError):
                manager.reject(record["approval_id"])

    def test_email_id_parser_accepts_uuid_and_legacy_without_truncation(self):
        from app.utils.email_sender import extract_approval_id
        aid = "ABCDEFAB-1234-5678-ABCD-123456789ABC"
        self.assertEqual(extract_approval_id(f"APPROVE {aid}"), aid.lower())
        self.assertEqual(extract_approval_id("REJECT ABCDEF12"), "abcdef12")
        self.assertIsNone(extract_approval_id("APPROVE abcdefab-1234"))

    def test_email_manager_cannot_decide_web_record(self):
        from app.utils.email_sender import ApprovalManager
        record = self.create()
        with patch("app.utils.email_sender.approval_service", self.service):
            manager = ApprovalManager(self.directory.name)
            for action in [manager.approve, manager.reject]:
                with self.assertRaises(PermissionError):
                    action(record["approval_id"].upper())
        self.assertEqual(self.service.get(record["approval_id"])["status"], "pending")

    def test_email_reply_api_accepts_uuid_and_legacy_and_blocks_web(self):
        from app.api.approval import EmailReplyRequest, process_email_reply
        from app.utils.email_sender import ApprovalManager
        from fastapi import HTTPException
        with patch("app.utils.email_sender.approval_service", self.service), patch("app.api.approval.approval_service", self.service):
            manager = ApprovalManager(self.directory.name)
            with patch("app.api.approval.email_sender.approval_manager", manager):
                with patch.object(settings, "APPROVAL_CHANNEL", "email"):
                    aid = self.create()["approval_id"]
                legacy = manager.create_approval("操作", "L2", "影响", ["echo test"], "host", "test@example.com")
                for approval_id in [aid, legacy]:
                    result = asyncio.run(process_email_reply(EmailReplyRequest(
                        from_email="reviewer@example.com", subject="审批回复",
                        body=f"APPROVE {approval_id.upper()}")))
                    self.assertTrue(result["success"])
                    self.assertEqual(result["approval_id"], approval_id)
                web = self.create()["approval_id"]
                with self.assertRaises(HTTPException) as blocked:
                    asyncio.run(process_email_reply(EmailReplyRequest(
                        from_email="reviewer@example.com", subject="审批回复", body=f"APPROVE {web}")))
                self.assertEqual(blocked.exception.status_code, 409)

    def test_imap_parses_uuid_legacy_and_ignores_web(self):
        from app.utils.email_sender import EmailSender, ApprovalManager
        with patch("app.utils.email_sender.approval_service", self.service):
            manager = ApprovalManager(self.directory.name)
            with patch.object(settings, "APPROVAL_CHANNEL", "email"):
                aid = self.create()["approval_id"]
            legacy = manager.create_approval("操作", "L2", "影响", ["echo test"], "host", "test@example.com")
            web = self.create()["approval_id"]
            messages = []
            for action, approval_id in [("APPROVE", aid), ("REJECT", legacy), ("APPROVE", web)]:
                message = EmailMessage()
                message["Subject"] = f"{action} {approval_id.upper()}"
                message.set_content(action)
                messages.append(("OK", [(b"", message.as_bytes())]))
            connection = MagicMock()
            connection.search.return_value = ("OK", [b"1 2 3"])
            connection.fetch.side_effect = messages
            sender = EmailSender()
            sender.smtp_user = "test@example.com"
            sender.smtp_password = "test"
            sender.approval_manager = manager
            with patch("app.utils.email_sender.imaplib.IMAP4_SSL", return_value=connection):
                result = asyncio.run(sender._check_inbox_for_replies())
            self.assertEqual(result, {aid: "approved", legacy: "rejected"})

    def test_l3_and_missing_session(self):
        with self.assertRaises(ValueError):
            self.service.create("删除", "L3", "全部数据", "rm -rf /", "host", "root")
        blocked = asyncio.run(self.registry._ask_user_confirmation("删除", "L2", command="rm -rf /"))
        self.assertFalse(blocked["success"])
        token = approval_session.set(None)
        try:
            missing = asyncio.run(self.registry._ask_user_confirmation("重启", "L2", command="systemctl restart nginx"))
            self.assertFalse(missing["success"])
            self.assertIn("session_id", missing["error"])
        finally:
            approval_session.reset(token)


if __name__ == "__main__":
    unittest.main()
