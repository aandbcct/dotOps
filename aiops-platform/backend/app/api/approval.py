import os
import re
import json
from datetime import datetime
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from ..utils.email_sender import email_sender, extract_approval_id
from ..services.approval_service import approval_service

router = APIRouter(prefix="/api/approval", tags=["approval"])


def _reject_web_record(approval_id: str):
    approval_id = approval_id.strip().lower()
    try:
        record = approval_service.get(approval_id)
    except ValueError as exc:
        raise HTTPException(422, "审批 ID 无效。") from exc
    if record and record.get("channel", "email") != "email":
        raise HTTPException(409, "请通过诊断会话页面处理网页审批。")


class EmailReplyRequest(BaseModel):
    """邮件回复请求"""
    from_email: str = Field(..., description="发件人邮箱")
    subject: str = Field(..., description="邮件主题")
    body: str = Field(..., description="邮件正文")
    approval_id: Optional[str] = Field(None, description="审批ID（如果从主题中解析）")


class ApprovalStatusResponse(BaseModel):
    """审批状态响应"""
    approval_id: str
    status: str
    operation: Optional[str] = None
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    rejected_at: Optional[str] = None
    reject_reason: Optional[str] = None


@router.post("/reply")
async def process_email_reply(request: EmailReplyRequest):
    """
    处理邮件回复
    
    用户回复邮件时，邮件服务器可以调用此接口
    解析邮件内容，判断是批准还是拒绝
    """
    body = request.body.upper()
    subject = request.subject.upper()
    
    approval_id = (request.approval_id.strip().lower() if request.approval_id
                   else extract_approval_id(subject + " " + body))
    
    if not approval_id:
        return {
            "success": False,
            "error": "无法从邮件中解析审批ID"
        }
    
    _reject_web_record(approval_id)
    approval = email_sender.approval_manager.get_approval(approval_id)
    
    if not approval:
        return {
            "success": False,
            "error": f"未找到审批ID: {approval_id}"
        }
    
    is_approve = any(kw in body for kw in ['APPROVE', '批准', '同意', '确认', 'YES', '是'])
    is_reject = any(kw in body for kw in ['REJECT', '拒绝', '不同意', '取消', 'NO', '否'])
    
    if is_approve and not is_reject:
        result = email_sender.approval_manager.approve(
            approval_id, 
            approved_by=request.from_email
        )
        return {
            "success": True,
            "action": "approved",
            "approval_id": approval_id,
            "approval": result,
            "message": f"操作已批准，审批ID: {approval_id}"
        }
    
    elif is_reject:
        reject_reason = ""
        reason_patterns = [
            r'理由[：:]\s*(.+)',
            r'原因[：:]\s*(.+)',
            r'REASON[：:]\s*(.+)',
        ]
        for pattern in reason_patterns:
            match = re.search(pattern, request.body, re.IGNORECASE)
            if match:
                reject_reason = match.group(1).strip()
                break
        
        result = email_sender.approval_manager.reject(
            approval_id,
            reason=reject_reason
        )
        return {
            "success": True,
            "action": "rejected",
            "approval_id": approval_id,
            "approval": result,
            "message": f"操作已拒绝，审批ID: {approval_id}"
        }
    
    else:
        return {
            "success": False,
            "error": "无法确定审批意图，请在邮件中明确说明 APPROVE（批准）或 REJECT（拒绝）"
        }


@router.get("/status/{approval_id}", response_model=ApprovalStatusResponse)
async def get_approval_status(approval_id: str):
    """
    获取审批状态
    """
    approval = email_sender.approval_manager.get_approval(approval_id)
    
    if not approval:
        raise HTTPException(status_code=404, detail=f"未找到审批ID: {approval_id}")
    
    return ApprovalStatusResponse(
        approval_id=approval_id,
        status=approval.get("status"),
        operation=approval.get("operation"),
        approved_at=approval.get("approved_at"),
        approved_by=approval.get("approved_by"),
        rejected_at=approval.get("rejected_at"),
        reject_reason=approval.get("reject_reason")
    )


@router.post("/approve/{approval_id}")
async def manual_approve(approval_id: str, approved_by: str = "manual"):
    """
    手动批准操作
    """
    _reject_web_record(approval_id)
    result = email_sender.approval_manager.approve(approval_id, approved_by)
    
    if not result:
        raise HTTPException(status_code=404, detail=f"未找到审批ID: {approval_id}")
    
    return {
        "success": True,
        "approval_id": approval_id,
        "status": "approved",
        "approval": result
    }


@router.post("/reject/{approval_id}")
async def manual_reject(approval_id: str, reason: str = ""):
    """
    手动拒绝操作
    """
    _reject_web_record(approval_id)
    result = email_sender.approval_manager.reject(approval_id, reason)
    
    if not result:
        raise HTTPException(status_code=404, detail=f"未找到审批ID: {approval_id}")
    
    return {
        "success": True,
        "approval_id": approval_id,
        "status": "rejected",
        "approval": result
    }


@router.get("/pending")
async def list_pending_approvals():
    """
    列出所有待审批的操作
    """
    return {
        "success": True,
        "pending_count": len([r for r in email_sender.approval_manager.pending_approvals.values() if r.get("channel") != "web"]),
        "pending_approvals": [r for r in email_sender.approval_manager.pending_approvals.values() if r.get("channel") != "web"]
    }
