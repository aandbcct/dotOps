"""将真实 Tempo 链路转换为现有页面使用的响应结构。"""

import asyncio
from fastapi import HTTPException

from .tempo_query import TempoQueryClient, Trace


def trace_info(trace: Trace) -> dict[str, object]:
    """列表中的数量、错误和服务均从实际 Span 计算。"""
    root = min(trace.root_spans or trace.spans, key=lambda span: span.start_time)
    return {
        "traceID": trace.trace_id,
        "rootServiceName": root.service_name,
        "rootTraceName": root.operation_name,
        "startTime": trace.start_time.isoformat(),
        "durationMs": trace.total_duration_ms or 0,
        "spanCount": trace.span_count,
        "hasError": trace.has_errors,
        "services": trace.services_involved,
    }


def trace_detail(trace: Trace) -> dict[str, object]:
    """保留真实父子标识，供页面展示跨服务调用树。"""
    span_ids = {span.span_id for span in trace.spans}
    spans = [
        {
            "spanID": span.span_id,
            # 外部客户端的父 Span 不在本次采集范围内，作为页面树的边界根节点。
            "parentSpanID": span.parent_span_id if span.parent_span_id in span_ids else None,
            "externalParentSpanID": span.parent_span_id if span.parent_span_id not in span_ids else None,
            "operationName": span.operation_name,
            "serviceName": span.service_name,
            "startTime": span.start_time.isoformat(),
            "durationMs": span.duration_ms or 0,
            "tags": span.attributes,
            "hasError": span.is_error,
            "statusCode": span.status_code.value,
        }
        for span in sorted(trace.spans, key=lambda span: span.start_time)
    ]
    return {
        "traceID": trace.trace_id,
        "spans": spans,
        "services": trace.services_involved,
        "totalDurationMs": trace.total_duration_ms or 0,
        "hasError": trace.has_errors,
        "errorSpans": [span for span in spans if span["hasError"]],
        "source": "tempo",
    }


async def read_trace(trace_id: str) -> Trace:
    """查询失败时明确返回不可用，绝不生成替代数据。"""
    try:
        async with TempoQueryClient() as client:
            trace = await client.query_trace_by_id(trace_id)
    except Exception as exc:
        raise HTTPException(503, detail={"code": "not_connected", "source": "unavailable", "message": "Tempo 链路查询失败"}) from exc
    if trace is None:
        raise HTTPException(404, detail="链路不存在")
    return trace


async def read_traces(service_name: str | None, error_only: bool, slow_only: bool, lookback: str, limit: int) -> list[Trace]:
    """搜索后读取真实明细，避免把搜索摘要误当完整调用链。"""
    try:
        async with TempoQueryClient() as client:
            if error_only:
                result = await client.search_error_traces(service_name=service_name, lookback=lookback, limit=limit)
            else:
                result = await client.search_traces(service_name=service_name, lookback=lookback, limit=limit, min_duration="500ms" if slow_only else None)
            if "error" in result.metrics:
                raise RuntimeError(result.metrics["error"])
            # 页面一次读取多条明细时限制并发，避免压满本地 Tempo 的查询队列。
            semaphore = asyncio.Semaphore(6)

            async def fetch(trace_id: str) -> Trace | None:
                async with semaphore:
                    return await client.query_trace_by_id(trace_id)

            traces = await asyncio.gather(*(fetch(item["traceID"]) for item in result.traces))
        return [trace for trace in traces if trace is not None and (not slow_only or (trace.total_duration_ms or 0) >= 500)]
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, detail={"code": "not_connected", "source": "unavailable", "message": "Tempo 链路查询失败"}) from exc


def dependency_view(traces: list[Trace], lookback: str) -> dict[str, object]:
    """从采样链路的真实父子关系计算依赖，计数不代表全量业务请求。"""
    services: set[str] = set()
    calls: dict[tuple[str, str], list[tuple[float, bool]]] = {}
    for trace in traces:
        services.update(trace.services_involved)
        spans = {span.span_id: span for span in trace.spans}
        for span in trace.spans:
            parent = spans.get(span.parent_span_id)
            if parent and parent.service_name != span.service_name:
                calls.setdefault((parent.service_name, span.service_name), []).append((span.duration_ms or 0, span.is_error))
    edges = []
    for (source, target), values in calls.items():
        durations = sorted(value[0] for value in values)
        errors = sum(value[1] for value in values)
        edges.append({"source": source, "target": target, "call_count": len(values), "error_count": errors, "error_rate": errors / len(values) * 100, "avg_latency_ms": sum(durations) / len(values), "p99_latency_ms": durations[min(len(durations) - 1, int(len(durations) * .99))]})
    return {"nodes": [{"id": name, "name": name} for name in sorted(services)], "edges": edges, "total_services": len(services), "total_edges": len(edges), "lookback": lookback, "sampled_traces": len(traces), "source": "tempo"}
