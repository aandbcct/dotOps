from ..state import AIOpsState


async def approval_node(state: AIOpsState) -> dict:
    confirmation = state.get("confirmation_request", {})

    return {
        "approval_status": "pending",
        "messages": [
            {
                "role": "system",
                "content": (
                    f"等待用户审批（页面可批准或拒绝）: {confirmation.get('operation', '')} "
                    f"(风险: {confirmation.get('risk', '')})"
                ),
            }
        ],
    }
