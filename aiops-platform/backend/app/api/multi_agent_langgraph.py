"""兼容旧 LangGraph 地址，所有诊断统一使用受保护的会话入口。"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.api.auth import get_current_user
from app.api import multi_agent

router = APIRouter(prefix="/api/multi-agent-lg", tags=["multi-agent-langgraph"])


class LangGraphRequest(BaseModel):
    query: str
    stream: Optional[bool] = False
    session_id: Optional[str] = None


@router.post("/process")
async def process_multi_agent_query(request: LangGraphRequest, current_user=Depends(get_current_user)):
    return await multi_agent.process_multi_agent_query(multi_agent.MultiAgentRequest(**request.model_dump()), current_user)


@router.post("/process/stream")
async def process_multi_agent_stream(request: LangGraphRequest, current_user=Depends(get_current_user)):
    return await multi_agent.process_multi_agent_stream(multi_agent.MultiAgentRequest(**request.model_dump()), current_user)


@router.post("/approve")
async def approve_operation(current_user=Depends(get_current_user)):
    raise HTTPException(410, "请使用 /api/multi-agent/sessions/{session_id}/review 审批，或 /ssh 补充用户名。")


@router.get("/state/{session_id}")
async def get_session_state(session_id: str, current_user=Depends(get_current_user)):
    return await multi_agent.get_diagnosis_session(session_id, current_user)


@router.get("/health")
async def health_check():
    return {"status": "healthy", "service": "multi-agent-langgraph", "framework": "LangGraph", "canonical_api": "/api/multi-agent"}