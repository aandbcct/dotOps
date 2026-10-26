"""探测固定测试目标，不允许任意地址或重定向。"""

from datetime import datetime, timezone
from enum import Enum
from time import monotonic

import httpx


TEST_WEB_URL = "http://test-web/health"


class ProbeStatus(str, Enum):
    """探测结果，不将连接失败推断为服务停止。"""

    HEALTHY = "healthy"
    HTTP_ERROR = "http_error"
    CONNECTION_ERROR = "connection_error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"


async def probe_test_web(url: str = TEST_WEB_URL) -> dict[str, object]:
    """只发送固定目标的 GET 请求，返回实际状态而不修改服务。"""
    started = monotonic()
    result: dict[str, object] = {
        "target": url,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": "http_probe",
        "success": False,
        "status_code": None,
    }
    if url != TEST_WEB_URL:
        return {**result, "source": "unavailable", "status": ProbeStatus.BLOCKED, "error": "仅允许探测本地测试服务健康接口", "duration_ms": 0.0}
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                result.update(
                    success=response.status_code == 200,
                    status_code=response.status_code,
                    status=ProbeStatus.HEALTHY if response.status_code == 200 else ProbeStatus.HTTP_ERROR,
                )
    except httpx.TimeoutException:
        result.update(status=ProbeStatus.TIMEOUT, error="HTTP 探测超时，不能据此确定服务已停止")
    except httpx.RequestError as exc:
        result.update(status=ProbeStatus.CONNECTION_ERROR, error=f"HTTP 连接失败：{exc}")
    result["duration_ms"] = round((monotonic() - started) * 1000, 2)
    return result
