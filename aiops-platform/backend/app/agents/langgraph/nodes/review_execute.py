"""审批恢复后执行已保存的原始操作，并把真实结果交回诊断任务。"""

from app.agents.langgraph.state import AIOpsState
from app.agents.langgraph.diagnosis_evidence import identify_observation
from app.agents.langgraph.nodes.react_agent import _tool_registry


async def review_execute_node(state: AIOpsState) -> dict:
    confirmation = state.get("confirmation_request") or {}
    result = await _tool_registry.execute(
        "execute_approved_command", approval_id=confirmation.get("approval_id", ""),
        wait_for_approval=False,
    )
    result = identify_observation(result)
    history = state.get("execution_history", []) + [{
        "tool": "execute_approved_command",
        "args": {"approval_id": confirmation.get("approval_id")}, "result": result,
    }]
    updates = {
        "execution_history": history, "confirmation_request": None,
        "approval_status": "executed" if result.get("success") and result.get("executed") else "failed",
    }
    if updates["approval_status"] == "failed":
        updates["diagnosis_result"] = {
            "is_final": True, "root_cause": "审批后的操作未成功执行。",
            "recommendation": "请根据执行错误检查连接或命令，不能据此认定业务已恢复。",
            "analysis_summary": result.get("error", "操作失败，详见工具执行记录。"),
        }
    return updates
