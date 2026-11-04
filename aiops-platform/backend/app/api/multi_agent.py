import uuid
import asyncio
import json
import re
import warnings
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.langgraph import build_aiops_graph, AIOpsState
from app.core.config import settings
from app.api.auth import get_current_user, require_admin
from app.services.approval_service import approval_session, approval_service
from app.agents.langgraph.diagnosis_evidence import assess_diagnosis

router = APIRouter(prefix="/api/multi-agent", tags=["multi-agent"])

_graph = build_aiops_graph()
_session_locks: Dict[str, asyncio.Lock] = {}


def _reject_legacy_web_review(approval_id: str):
    try:
        record = approval_service.get(approval_id)
    except ValueError as exc:
        raise HTTPException(422, "审批 ID 无效。") from exc
    if record and record.get("channel") == "web":
        raise HTTPException(409, "网页审批必须通过绑定诊断会话的审查接口处理。")

warnings.filterwarnings("always", message=".*deprecated.*", category=DeprecationWarning)


class MultiAgentRequest(BaseModel):
    query: str
    stream: Optional[bool] = False
    session_id: Optional[str] = None


class MultiAgentResponse(BaseModel):
    query: str
    start_time: str
    stages: Dict[str, Any]
    final_decision: Optional[Dict[str, Any]]
    execution_result: Optional[Dict[str, Any]]
    duration_seconds: float


def _render_result(query: str, result: dict, start_time: datetime, session_id: str, running: bool = False):
    intent_data = result.get("intent_data", {})
    diagnosis_result = result.get("diagnosis_result") or {}
    confirmation_request = result.get("confirmation_request")
    approval_status = result.get("approval_status")
    matched_skills = result.get("matched_skills", [])
    # 模型声明不能作为已修复的证据，本接口尚无修复后复核机制。
    warning_cleared = False

    entities_raw = intent_data.get("entities", {})
    services_list = entities_raw.get("services", [])
    symptoms_list = entities_raw.get("symptoms", [])
    servers_list = entities_raw.get("servers", [])

    flat_entities = {
        "service": services_list[0].get("normalized", services_list[0].get("value", "")) if services_list else "",
        "symptom": symptoms_list[0].get("normalized", symptoms_list[0].get("value", "")) if symptoms_list else "",
        "time_range": entities_raw.get("time_range", ""),
        "servers": [s.get("normalized", s.get("value", "")) if isinstance(s, dict) else s for s in servers_list],
        "services": [s.get("normalized", s.get("value", "")) if isinstance(s, dict) else s for s in services_list],
        "symptoms": [s.get("normalized", s.get("value", "")) if isinstance(s, dict) else s for s in symptoms_list],
        "_raw": entities_raw,
    }

    final_decision = None
    if confirmation_request:
        final_decision = {
            "decision": "NEEDS_CONFIRMATION",
            "is_final": False,
            "confirmation_request": confirmation_request,
            "root_cause_summary": diagnosis_result.get("root_cause", ""),
            "reasoning": diagnosis_result.get("analysis_summary", ""),
            "action_plan": diagnosis_result.get("recommendation", ""),
            "risk_level": diagnosis_result.get("risk_level", "MEDIUM"),
        }
    elif diagnosis_result:
        problem_type = diagnosis_result.get("problem_type", "unknown")
        if not problem_type or problem_type == "unknown":
            dr_decision = diagnosis_result.get("decision", "")
            if dr_decision == "KNOWLEDGE_QA":
                problem_type = "knowledge_qa"
            elif diagnosis_result.get("knowledge_report"):
                problem_type = "knowledge_qa"

        final_decision = {
            "decision": "KNOWLEDGE_QA" if problem_type == "knowledge_qa" else "MANUAL_INTERVENTION",
            "is_final": True,
            "problem_type": problem_type,
            "root_cause": diagnosis_result.get("root_cause", diagnosis_result.get("knowledge_report", "")[:200]),
            "root_cause_summary": diagnosis_result.get("root_cause", diagnosis_result.get("knowledge_report", "")[:200]),
            "impact": diagnosis_result.get("impact", "无直接影响"),
            "recommendation": diagnosis_result.get("recommendation", diagnosis_result.get("knowledge_report", "")[:300]),
            "action_plan": diagnosis_result.get("recommendation", diagnosis_result.get("knowledge_report", "")[:300]),
            "risk_level": diagnosis_result.get("risk_level", "LOW"),
            "confidence": diagnosis_result.get("confidence", "MEDIUM"),
            "reasoning": diagnosis_result.get("analysis_summary", diagnosis_result.get("knowledge_report", "")),
            "analysis_summary": diagnosis_result.get("analysis_summary", diagnosis_result.get("knowledge_report", "")),
        }
    elif approval_status == "rejected":
        final_decision = {
            "decision": "REJECTED",
            "reason": "用户拒绝了操作请求",
        }
    elif result.get("need_ssh_login") or servers_list:
        host = servers_list[0].get("normalized", servers_list[0].get("value", str(servers_list[0]))) if servers_list else "目标服务器"
        final_decision = {
            "decision": "NEEDS_CONFIRMATION",
            "is_final": False,
            "confirmation_request": {
                "success": True,
                "requires_confirmation": True,
                "operation": f"确认 SSH 登录信息",
                "risk": "低风险（仅信息收集）",
                "impact": f"需要获取 SSH 用户名才能连接服务器 {host} 进行诊断",
                "message": f"需要连接服务器 {host} 进行诊断，请在查询中包含 SSH 用户名（例如：用 jaci 用户 SSH 登录）",
            },
            "root_cause_summary": f"缺少 {host} 的 SSH 登录用户名",
            "reasoning": f"意图识别检测到需要连接服务器 {host}，但未提供 SSH 用户名",
            "action_plan": "在查询中补充 SSH 用户名后重新提交，例如：用 <用户名> SSH 登录 8.136.186.115",
            "risk_level": "LOW",
        }
    else:
        final_decision = {
            "decision": "MANUAL_INTERVENTION",
            "reason": "诊断流程未产出最终结论",
        }

    end_time = datetime.now()
    iteration_count = result.get("iteration_count", 0)
    execution_history = result.get("execution_history", [])
    review = None
    for entry in reversed(execution_history):
        if entry.get("tool") == "ask_user_confirmation":
            approval_id = (entry.get("result") or {}).get("approval_id")
            if approval_id:
                review = approval_service.get(approval_id)
                break
    needs_ssh = bool(result.get("need_ssh_login") and not result.get("ssh_confirmed"))
    confirmation_kind = "ssh_user" if needs_ssh else "command_approval" if review and review.get("status") == "pending" else None

    skill_summary = ", ".join(matched_skills) if matched_skills else "未匹配到特定技能，使用通用诊断流程"
    skills_preview = result.get("skills_content", "")[:500]

    diagnosis_plan_from_history = None
    command_entries = []
    email_entry = None

    for entry in execution_history:
        tool = entry.get("tool", "")
        args = entry.get("args", {}) or {}
        res = entry.get("result", {}) or {}
        if tool == "save_diagnosis_plan":
            diagnosis_plan_from_history = {
                "plan_name": args.get("plan_name", args.get("check_type", "")),
                "check_type": args.get("check_type", ""),
                "commands": args.get("commands", []) if isinstance(args.get("commands"), list) else [],
                "reasoning": str(args.get("reasoning", ""))[:500],
            }

        if tool == "execute_command":
            output_str = ""
            if isinstance(res, dict):
                output_str = res.get("output", "") or res.get("error", "")
            elif isinstance(res, str):
                output_str = res

            if isinstance(output_str, str) and len(output_str) > 2000:
                output_str = output_str[:2000] + "\n... (truncated)"

            cmd_entry = {
                "command": args.get("command", "")[:200],
                "target_host": args.get("target_host", res.get("target_host", "") if isinstance(res, dict) else ""),
                "success": True if (isinstance(res, dict) and res.get("success")) else False,
                "output": output_str,
            }
            command_entries.append(cmd_entry)

        if tool == "execute_approved_command" and isinstance(res, dict):
            for execution in res.get("execution_results", []):
                command_entries.append({
                    "command": execution.get("command", ""),
                    "target_host": execution.get("target_host", ""),
                    "output": execution.get("output", execution.get("error", "")),
                    "success": execution.get("success", False),
                })

        if tool == "ask_user_confirmation" and isinstance(res, dict) and res.get("email_sent"):
            email_entry = {
                "email_sent": True,
                "approval_id": res.get("approval_id", ""),
                "to_email": res.get("to_email", ""),
                "operation": res.get("operation", ""),
                "risk": res.get("risk", ""),
                "message": res.get("message", ""),
                "status": "pending",
            }

    dynamic_status = "completed"
    if confirmation_request or result.get("need_ssh_login"):
        dynamic_status = "needs_confirmation"
    if email_entry:
        dynamic_status = "waiting_approval"
    if needs_ssh or (review and review.get("status") == "pending"):
        dynamic_status = "needs_confirmation"
    elif approval_status == "rejected":
        dynamic_status = "rejected"
    elif approval_status == "failed":
        dynamic_status = "failed"
    if running:
        dynamic_status = "processing"

    analysis_report = diagnosis_result.get("analysis_summary", "")
    if not analysis_report:
        km = result.get("knowledge_context", {})
        if isinstance(km, dict):
            analysis_report = km.get("knowledge_report", str(km)[:500])

    # 模型总结不能冒充知识检索结果。
    knowledge_report = (result.get("knowledge_context") or {}).get("knowledge_report", "")
    if not knowledge_report:
        knowledge_report = "本次未执行知识查询，或未返回知识数据。"

    stages = {
        "intent_parsing": {
            "intent": intent_data.get("intent", "GENERAL_QA"),
            "confidence": intent_data.get("confidence", "LOW"),
            "entities": flat_entities,
            "keywords": intent_data.get("keywords", []),
            "normalized_query": intent_data.get("normalized_query", "") or query,
        },
        "skill_matching": {
            "matched_skills": matched_skills,
            "skill_summary": skill_summary,
            "skills_content_length": len(result.get("skills_content", "")),
            "skills_preview": skills_preview,
        },
        "observability_analysis": {
            "analysis_report": analysis_report or "待分析",
            "diagnosis_plan": diagnosis_plan_from_history,
        },
        "knowledge_query": {
            "knowledge_report": knowledge_report,
            "service": flat_entities.get("service", ""),
        },
        "dynamic_execution": {
            "status": dynamic_status,
            "iterations": iteration_count,
            "command_entries": command_entries,
            "email_approval": email_entry,
        },
    }

    display_execution_result = {
        "tool_name": "ReAct Agent (LangGraph)",
        "template_name": "LangGraph StateGraph",
        "risk_assessment": diagnosis_result.get("risk_level", "MEDIUM"),
        "requires_approval": bool(review and review.get("status") == "pending") or bool(email_entry and approval_status not in {"executed", "rejected", "failed"}),
        "execution_note": "等待页面人工审查，尚未执行待批准命令。" if review and review.get("status") == "pending" else diagnosis_result.get("recommendation", "等待补充 SSH 用户名。" if needs_ssh else "详见工具执行记录。"),
    }

    visited = result.get("visited_nodes", [])
    node_labels = {
        "intent_parse": "意图识别", "ssh_check": "连接信息检查",
        "skill_match": "Skill 匹配", "react_agent": "工具诊断",
        "knowledge_qa": "知识问答", "approval": "审批请求登记", "review_execute": "执行已批准操作",
        "human_review": "人工补充信息", "finalize": "结果整理",
    }
    progress = [
        {"node": node, "title": label, "status": "finish" if node in visited else "wait"}
        for node, label in node_labels.items()
    ]
    knowledge_sources = (result.get("knowledge_context") or {}).get("data_sources", {})

    return JSONResponse(
        content={
            "query": query,
            "session_id": session_id,
            "review": review,
            "confirmation_kind": confirmation_kind,
            "data_mode": "mock" if settings.MOCK_ENABLED else "real",
            "data_sources": {"analysis": "model_generated", **knowledge_sources},
            "stage_progress": progress,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_seconds": (end_time - start_time).total_seconds(),
            "stages": stages,
            "final_decision": final_decision,
            "execution_result": display_execution_result,
            "warning_cleared": warning_cleared,
            "mode": "langgraph",
            "iteration_count": iteration_count,
            "diagnosis_result": diagnosis_result,
            "diagnosis_assessment": (result.get("diagnosis_assessment") or assess_diagnosis(query, diagnosis_result, execution_history)) if diagnosis_result else None,
            "raw_response": analysis_report or knowledge_report,
            "execution_history": execution_history,
            "saved_to": result.get("full_result_path"),
        },
        headers={
            "X-Backend": "langgraph",
            "X-Canonical-API": "/api/multi-agent/process",
        },
    )



@router.post("/process")
async def process_multi_agent_query(
    request: MultiAgentRequest,
    req: Request,
    current_user=Depends(get_current_user),
):
    """
    Multi-Agent 诊断查询 (已迁移至 LangGraph 后端)

    与旧版接口兼容的响应格式，后端已切换为 LangGraph 实现。
    """

    start_time = datetime.now()
    session_id = request.session_id or str(uuid.uuid4())

    config = {
        "configurable": {
            "thread_id": session_id
        },
        "recursion_limit": 100,
    }

    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "该诊断会话正在执行，请等待结果或刷新当前状态。")
    token = approval_session.set(session_id)
    try:
        async with lock:
            existing = await _graph.aget_state(config)
            if existing.values:
                raise HTTPException(409, "会话已存在，请继续审查或使用新的会话发起诊断。")
            result = await _graph.ainvoke(
                {
                    "user_query": request.query, "session_id": session_id,
                    "requester": getattr(current_user, "username", "test"),
                    "started_at": start_time.isoformat(),
                    "visited_nodes": [], "messages": [], "iteration_count": 0,
                    "need_ssh_login": False, "ssh_confirmed": False,
                    "warning_cleared": False, "matched_skills": [],
                    "skills_content": "", "execution_history": [],
                    "confirmation_request": None, "approval_status": None,
                    "diagnosis_result": None,
                }, config=config,
            )
        return _render_result(request.query, result, start_time, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"处理查询时发生错误: {str(e)}",
        )
    finally:
        approval_session.reset(token)


class ReviewDecision(BaseModel):
    """页面只能决定已有审批，不能提交或替换执行命令。"""
    model_config = ConfigDict(extra="forbid")
    approval_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    approved: bool
    reason: str = ""


class SshInformation(BaseModel):
    """补充当前暂停会话的 SSH 用户名。"""
    ssh_user: str


def _session_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}, "recursion_limit": 100}


async def _owned_state(session_id: str, user):
    snapshot = await _graph.aget_state(_session_config(session_id))
    if not snapshot.values:
        raise HTTPException(404, "诊断会话已失效或不存在；服务重启后请重新发起，旧命令不会自动重放。")
    if snapshot.values.get("requester") != user.username and not user.is_admin:
        raise HTTPException(403, "无权访问此诊断会话。")
    return snapshot


def _state_response(session_id: str, values: dict, running: bool = False):
    start = datetime.fromisoformat(values["started_at"]) if values.get("started_at") else datetime.now()
    return _render_result(values.get("user_query", ""), values, start, session_id, running=running)


@router.get("/sessions/{session_id}")
async def get_diagnosis_session(session_id: str, current_user=Depends(get_current_user)):
    snapshot = await _owned_state(session_id, current_user)
    lock = _session_locks.get(session_id)
    return _state_response(session_id, snapshot.values, running=bool(lock and lock.locked()))


@router.post("/sessions/{session_id}/review")
async def review_diagnosis_operation(session_id: str, decision: ReviewDecision, current_user=Depends(require_admin)):
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "会话正在恢复执行，请勿重复审批。")
    token = approval_session.set(session_id)
    try:
        async with lock:
            snapshot = await _owned_state(session_id, current_user)
            confirmation = snapshot.values.get("confirmation_request") or {}
            record = approval_service.get(decision.approval_id)
            if not record or record.get("session_id") != session_id:
                raise HTTPException(404, "当前会话没有此审批记录。")
            if record["status"] != "pending":
                expected = "approved" if decision.approved else "rejected"
                if record["status"] != expected:
                    raise HTTPException(409, "审批决定已生效，不可反转。")
                # 决定已保存但恢复中断时，允许同一决定继续恢复当前审批。
                if confirmation.get("approval_id") != decision.approval_id or not snapshot.next:
                    return _state_response(session_id, snapshot.values)
            if confirmation.get("approval_id") != decision.approval_id or not snapshot.next:
                raise HTTPException(409, "此操作不是当前会话正在等待的审批。")
            try:
                record = approval_service.decide(decision.approval_id, session_id, decision.approved, current_user.username, decision.reason)
            except (ValueError, PermissionError) as exc:
                raise HTTPException(409, str(exc)) from exc
            updates = {"approval_status": record["status"]}
            if not decision.approved:
                updates.update(confirmation_request=None, diagnosis_result={
                    "is_final": True, "root_cause": "操作已被用户拒绝，未执行该命令。",
                    "recommendation": "保留现有证据；如需其他诊断，请重新发起任务。",
                    "analysis_summary": decision.reason or "用户在页面拒绝了操作。",
                })
            await _graph.aupdate_state(_session_config(session_id), updates, as_node="approval")
            result = await _graph.ainvoke(None, config=_session_config(session_id))
            return _state_response(session_id, result)
    finally:
        approval_session.reset(token)


@router.post("/sessions/{session_id}/ssh")
async def supply_diagnosis_ssh_user(session_id: str, information: SshInformation, current_user=Depends(get_current_user)):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", information.ssh_user):
        raise HTTPException(422, "SSH 用户名格式无效。")
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        raise HTTPException(409, "会话正在执行，请稍后刷新。")
    token = approval_session.set(session_id)
    try:
        async with lock:
            snapshot = await _owned_state(session_id, current_user)
            if "human_review" not in snapshot.next:
                raise HTTPException(409, "当前任务没有等待补充 SSH 用户名。")
            entities = dict(snapshot.values.get("entities") or {})
            entities["ssh_users"] = [{"value": information.ssh_user, "normalized": information.ssh_user}]
            intent = dict(snapshot.values.get("intent_data") or {})
            intent["entities"] = entities
            await _graph.aupdate_state(_session_config(session_id), {
                "entities": entities, "intent_data": intent, "ssh_user": information.ssh_user,
                "ssh_confirmed": True, "need_ssh_login": False, "confirmation_request": None,
                "visited_nodes": snapshot.values.get("visited_nodes", []) + ["human_review"],
            }, as_node="human_review")
            result = await _graph.ainvoke(None, config=_session_config(session_id))
            return _state_response(session_id, result)
    finally:
        approval_session.reset(token)


@router.post("/process/stream")
async def process_multi_agent_stream(request: MultiAgentRequest, current_user=Depends(get_current_user)):
    """兼容流式地址，统一经受保护的会话入口处理后返回结果事件。"""
    result = await process_multi_agent_query(request, current_user)
    payload = f"data: {json.dumps({'type': 'result', 'data': json.loads(result.body)}, ensure_ascii=False)}\n\n"
    return StreamingResponse(iter([payload]), media_type="text/event-stream")

@router.post("/approve/{approval_id}")
async def approve_operation(approval_id: str):
    """
    手动批准审批请求
    """
    from app.utils.email_sender import email_sender as _es
    
    _reject_legacy_web_review(approval_id)
    approval = _es.approval_manager.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"未找到审批ID: {approval_id}")
    
    if approval.get("status") != "pending":
        return {
            "success": True,
            "approval_id": approval_id,
            "status": approval.get("status"),
            "message": f"审批已处理，当前状态: {approval.get('status')}"
        }
    
    result = _es.approval_manager.approve(approval_id, "api_manual")
    if not result:
        raise HTTPException(status_code=500, detail="审批操作失败")
    
    return {
        "success": True,
        "approval_id": approval_id,
        "status": "approved",
        "message": "审批已通过",
        "commands": result.get("commands", []),
        "target_host": result.get("target_host", ""),
    }


@router.post("/reject/{approval_id}")
async def reject_operation(approval_id: str):
    """
    手动拒绝审批请求
    """
    from app.utils.email_sender import email_sender as _es
    
    _reject_legacy_web_review(approval_id)
    approval = _es.approval_manager.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail=f"未找到审批ID: {approval_id}")
    
    if approval.get("status") != "pending":
        return {
            "success": True,
            "approval_id": approval_id,
            "status": approval.get("status"),
            "message": f"审批已处理，当前状态: {approval.get('status')}"
        }
    
    _es.approval_manager.reject(approval_id, "api_manual")
    
    return {
        "success": True,
        "approval_id": approval_id,
        "status": "rejected",
        "message": "审批已拒绝"
    }


@router.get("/approvals/pending")
async def list_pending_approvals():
    """
    列出所有待审批的操作
    """
    from app.utils.email_sender import email_sender as _es
    from pathlib import Path
    
    data_dir = Path("data/approvals")
    pending = []
    for fp in data_dir.glob("*.json"):
        try:
            with open(fp) as f:
                d = json.load(f)
                if d.get("status") == "pending" and d.get("channel") != "web":
                    pending.append(d)
        except Exception:
            pass
    
    return {
        "success": True,
        "count": len(pending),
        "pending": pending
    }


@router.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "multi-agent",
        "backend": "LangGraph",
        "deprecated": False,
        "note": "后端已迁移至 LangGraph 实现。旧版 Orchestrator 模式不再维护。",
    }
