"""仅允许显式演示模式访问尚未接通真实数据的接口。"""

from fastapi import HTTPException

from app.core.config import settings


def require_mock_mode() -> None:
    """真实能力未接入时返回明确错误，不伪造成功结果。"""
    if not settings.MOCK_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_connected", "message": "真实数据尚未接入；模拟演示已关闭", "source": "unavailable"},
        )
