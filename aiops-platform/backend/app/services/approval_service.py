"""统一审批记录、会话授权和一次执行控制。"""

import json
import os
import threading
import uuid
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import settings

approval_session: ContextVar[str | None] = ContextVar("approval_session", default=None)


class ApprovalService:
    """线程安全且原子持久化的审批服务。"""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or Path(__file__).resolve().parents[2] / "data" / "approvals")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, approval_id: str) -> Path:
        if not approval_id or any(c not in "0123456789abcdef-" for c in approval_id):
            raise ValueError("审批ID无效")
        return self.data_dir / f"{approval_id}.json"

    def _save(self, record: dict) -> None:
        path = self._path(record["approval_id"])
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, approval_id: str) -> dict | None:
        with self._lock:
            path = self._path(approval_id)
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def create(self, operation: str, risk: str, impact: str, command: str,
               target_host: str, ssh_user: str, session_id: str | None = None) -> dict:
        session_id = session_id or approval_session.get()
        channel = settings.APPROVAL_CHANNEL.lower()
        if channel not in {"web", "email"}:
            raise ValueError("不支持的审批渠道")
        if not session_id:
            raise ValueError("创建审批必须绑定会话 session_id")
        if risk.upper() == "L3":
            raise ValueError("L3 高风险命令禁止审批或执行")
        if not command.strip():
            raise ValueError("审批必须包含原始执行命令")
        record = {
            "approval_id": str(uuid.uuid4()), "session_id": session_id,
            "channel": channel, "operation": operation, "risk": risk,
            "impact": impact, "commands": [command], "target_host": target_host,
            "ssh_user": ssh_user, "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": None, "execution_result": None,
            "execution_status": "not_started",
        }
        with self._lock:
            self._save(record)
        return deepcopy(record)

    def _owned(self, approval_id: str, session_id: str) -> dict:
        record = self.get(approval_id)
        if record is None:
            raise ValueError("审批不存在")
        if not session_id or record.get("session_id") != session_id:
            raise PermissionError("审批与当前会话不匹配")
        return record

    def decide(self, approval_id: str, session_id: str, approved: bool,
               reviewer: str, reason: str = "") -> dict:
        with self._lock:
            record = self._owned(approval_id, session_id)
            desired = "approved" if approved else "rejected"
            if record["status"] != "pending":
                if record["status"] != desired:
                    raise ValueError("审批决定已生效，不可反转")
                return record
            record.update(status=desired, approved_by=reviewer, reason=reason,
                          decided_at=datetime.now(timezone.utc).isoformat())
            self._save(record)
            return record

    def get_pending(self, session_id: str) -> list:
        with self._lock:
            records = [json.loads(path.read_text(encoding="utf-8"))
                       for path in self.data_dir.glob("*.json")]
            return [r for r in records if r.get("session_id") == session_id and r.get("status") == "pending"]

    def claim_execution(self, approval_id: str, session_id: str) -> tuple[dict, bool]:
        """在执行前持久化占用；进程中断后也不自动重放副作用。"""
        with self._lock:
            record = self._owned(approval_id, session_id)
            if record.get("risk", "").upper() == "L3":
                raise ValueError("L3 高风险命令禁止执行")
            if record["status"] != "approved" or record["execution_status"] != "not_started":
                return record, False
            record["execution_status"] = "executing"
            self._save(record)
            return record, True

    def finish_execution(self, approval_id: str, session_id: str, result: dict) -> dict:
        with self._lock:
            record = self._owned(approval_id, session_id)
            if record["execution_status"] != "executing":
                raise ValueError("审批执行未占用或已结束")
            record["execution_result"] = result
            record["execution_status"] = "succeeded" if result.get("success") else "failed"
            self._save(record)
            return record


approval_service = ApprovalService()
