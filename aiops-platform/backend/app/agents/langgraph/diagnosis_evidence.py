"""将真实工具观察与模型推测分开，并检查 HTTP 故障结论的必要证据。"""

import json
import re
import uuid
from enum import Enum
from urllib.parse import urlsplit, urlunsplit


class ConclusionStatus(str, Enum):
    """诊断结论的证据状态，不表示业务是否恢复。"""

    CONFIRMED = "confirmed"
    SUSPECTED = "suspected"
    INCONCLUSIVE = "inconclusive"


NON_OBSERVATION_TOOLS = {"ask_user_confirmation", "save_diagnosis_plan", "submit_diagnosis_result",
                         "save_execution_output", "wait_for_approval", "check_approval_status"}


def identify_observation(result: dict, tool: str = "") -> dict:
    """由执行端生成引用编号，模型不能用自编编号制造工具证据。"""
    if tool in NON_OBSERVATION_TOOLS:
        return result
    return {**result, "evidence_id": f"E-{uuid.uuid4().hex[:12]}"}


def _url_key(url: str) -> str:
    parts = urlsplit(url.rstrip("。，,；;）)"))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _http_failed(result: dict) -> bool:
    status = result.get("http_status")
    return bool(result.get("probe_attempted")) and (bool(result.get("transport_error")) or isinstance(status, int) and status >= 400)


def _command_excerpt(output: str) -> str:
    """优先展示状态码与错误线索，完整命令输出仍在诊断详情中保留。"""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    relevant = [line for line in lines if re.search(r"HTTP/|enabled|error|exception|timeout|failed|refused|503|500", line, re.I)]
    return "；".join((relevant or lines)[:4])[:360]


def _trace_excerpt(result: dict, service: str) -> str:
    """从原始 Span 提取入口和异常依赖，完整 Trace 留在执行详情。"""
    trace = result.get("trace") or {}
    if not trace:
        return f"服务 {service or '全部服务'}：检索到 {result.get('total_traces', 0)} 条调用链。"
    failures = []
    for span in trace.get("spans", []):
        attributes = span.get("attributes") or {}
        code = attributes.get("http.response.status_code", attributes.get("http.status_code"))
        if str(code).isdigit() and int(code) >= 400:
            address = attributes.get("url.full") or attributes.get("http.url") or span.get("operation_name", "")
            description = f"{address} → HTTP {code}"
            if description not in failures:
                failures.append(description)
    return f"Trace {result.get('trace_id', '')}：" + ("；".join(failures[:3]) if failures else f"发现 {trace.get('error_count', 0)} 个错误 Span。")


def _configuration_excerpt(body: dict, prefix: str = "") -> list[str]:
    """展开少量配置事实，避免在摘要中输出整个 JSON。"""
    facts = []
    for name, value in body.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(value, dict) and not prefix:
            facts.extend(_configuration_excerpt(value, key))
        elif isinstance(value, (str, int, float, bool)):
            facts.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    return facts[:4]


def assess_diagnosis(query: str, diagnosis: dict, history: list[dict]) -> dict:
    """只展示真实观察；缺少必要证据时降低结论状态而非认定系统健康。"""
    observations: dict[str, tuple[dict, dict]] = {}
    evidence = []
    limitations = []
    excluded = NON_OBSERVATION_TOOLS
    for entry in history:
        result = entry.get("result") or {}
        tool = entry.get("tool", "")
        if not isinstance(result, dict) or tool in excluded:
            continue
        if result.get("requires_confirmation"):
            continue
        if tool == "probe_http" and not result.get("probe_attempted"):
            limitations.append(f"HTTP 探测没有执行：{result.get('transport_error') or result.get('error') or '前置检查失败'}")
            continue
        if not result.get("success") and tool != "probe_http":
            limitations.append(f"{tool} 未取得有效结果：{str(result.get('error', '查询失败'))[:180]}")
            continue
        if result.get("source") in {"mock_data", "mock", "demo"}:
            limitations.append(f"{tool} 返回演示数据，未作为诊断证据。")
            continue
        observation_id = result.get("evidence_id")
        if not observation_id:
            continue
        args = entry.get("args") or {}
        source = args.get("target_host") or result.get("target_host") or "观测系统"
        if tool == "probe_http":
            url = result.get("requested_url") or args.get("url", "")
            source = f"{source} · {url}"
            observation = f"GET {url} → HTTP {result.get('http_status') or '无响应'}"
            if result.get("elapsed_ms") is not None:
                observation += f"，耗时 {result['elapsed_ms']} ms"
            if result.get("transport_error"):
                observation += f"；{result['transport_error']}"
            if result.get("body_excerpt"):
                try:
                    body = json.loads(result["body_excerpt"])
                except (ValueError, TypeError):
                    body = None
                if isinstance(body, dict) and body.get("requestId"):
                    observation += f"；请求 ID：{body['requestId']}"
                elif isinstance(body, dict) and observation_id in (diagnosis.get("cause_evidence_ids") or []):
                    observation += "；" + "，".join(_configuration_excerpt(body))
        elif tool == "execute_approved_command":
            executions = result.get("execution_results", [])
            source = "; ".join(str(item.get("target_host", "")) for item in executions)
            observation = _command_excerpt("\n".join(str(item.get("output") or item.get("error") or "") for item in executions))
        elif tool == "execute_command":
            observation = _command_excerpt(str(result.get("output", "")))
        elif tool == "analyze_trace":
            count = result.get("total_traces")
            observation = _trace_excerpt(result, args.get("service_name", ""))
            if count == 0:
                limitations.append("当前服务与时间范围内未检索到匹配 Trace；这不能证明业务健康。")
        else:
            observation = json.dumps({k: v for k, v in result.items() if k != "evidence_id"}, ensure_ascii=False, default=str)[:900]
        if not observation.strip():
            limitations.append(f"{tool} 没有返回可用于判断的内容。")
            continue
        observations[observation_id] = (entry, result)
        evidence.append({"id": observation_id, "tool": tool, "observation": observation, "source": source})

    # 旧版本过程记录曾带编号；已知的计划等非观察引用应忽略，不否定其余真实证据。
    process_ids = {(entry.get("result") or {}).get("evidence_id") for entry in history if entry.get("tool") in excluded}
    requested_ids = [item for item in (diagnosis.get("evidence_ids") or []) if item not in process_ids]
    valid_ids = [item for item in requested_ids if item in observations]
    gaps = []
    if not valid_ids:
        gaps.append("结论尚未引用有效的实际工具证据。")
    if any(item not in observations for item in requested_ids):
        # 无效引用不能生成证据；若其余有效观察已满足必要条件，不因多余引用否定事实。
        limitations.append("部分引用没有有效观察，已排除；结论仅依据下列实际证据。")

    http_incident = bool(re.search(r"http|网页|页面|首页|返回\s*[45]\d\d", query, re.I))
    if http_incident:
        urls = re.findall(r"https?://[^\s<>`\"'，。；）]+", query)
        expected = {_url_key(url) for url in urls}
        target_ips = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", query))
        probes = [(entry, result) for entry, result in observations.values() if entry.get("tool") == "probe_http"]
        matched = []
        for entry, result in probes:
            args = entry.get("args") or {}
            url = result.get("requested_url") or args.get("url", "")
            execution_host = args.get("target_host") or result.get("target_host")
            if execution_host and target_ips and execution_host not in target_ips:
                continue
            if not execution_host and urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"} and target_ips - {"127.0.0.1"}:
                continue
            if expected and _url_key(url) in expected:
                matched.append(result)
        if not expected:
            gaps.append("缺少用户确认的完整故障 URL，请补充协议、端口及业务路径；不能把猜测入口当作已确认对象。")
        if not matched:
            gaps.append("尚未用 probe_http 检查故障业务的完整 URL；根路径和健康端点不能替代业务页面。")
        elif not any(_http_failed(result) for result in matched):
            gaps.append("尚未在对应业务 URL 复现异常；HTTP 200 或空 Trace 不能用于确认故障原因。")
        elif not any(result.get("evidence_id") in valid_ids and _http_failed(result) for result in matched):
            gaps.append("结论没有引用对应业务 URL 的异常探测证据。")
        symptom_fingerprints = {(_url_key(result.get("requested_url", "")), str(result.get("target_host", ""))) for result in matched if _http_failed(result)}
        cause_ids = diagnosis.get("cause_evidence_ids") or []
        independent_causes = []
        for evidence_id in cause_ids:
            if evidence_id not in valid_ids:
                continue
            entry, result = observations[evidence_id]
            tool = entry.get("tool")
            if tool in {"list_trace_services", "analyze_service_dependency"} or tool == "analyze_trace" and result.get("total_traces") == 0:
                continue
            if tool == "probe_http":
                url = result.get("requested_url", "")
                if (_url_key(url), str(result.get("target_host", ""))) in symptom_fingerprints or re.search(r"/(?:health|actuator/health)/?$", urlsplit(url).path):
                    continue
                if not _http_failed(result) and not result.get("body_excerpt"):
                    continue
            independent_causes.append(evidence_id)
        if diagnosis.get("conclusion_status") == ConclusionStatus.CONFIRMED.value and not independent_causes:
            gaps.append("缺少原因证据 cause_evidence_ids；重复探测同一异常、服务名单或健康检查不能代替因果验证。")

    claimed = diagnosis.get("conclusion_status", ConclusionStatus.INCONCLUSIVE.value)
    status = claimed if claimed in {item.value for item in ConclusionStatus} else ConclusionStatus.INCONCLUSIVE.value
    if gaps:
        status = ConclusionStatus.INCONCLUSIVE.value
    selected = [item for item in evidence if item["id"] in valid_ids]
    cause_ids = set(diagnosis.get("cause_evidence_ids") or [])
    explicit_urls = {_url_key(url) for url in re.findall(r"https?://[^\s<>`\"'，。；）]+", query)}

    def display_priority(item: dict) -> int:
        entry, raw = observations[item["id"]]
        if entry.get("tool") == "probe_http" and _url_key(raw.get("requested_url", "")) in explicit_urls:
            return 0
        if item["id"] in cause_ids and re.search(r"HTTP\s+[45]\d\d|HTTP/\S+\s+[45]\d\d|enabled.{0,8}true|exception|refused|timeout", item["observation"], re.I):
            return 1
        return 4 if item["tool"] in {"list_trace_services", "analyze_service_dependency"} else 3

    ordered = sorted(enumerate(selected or evidence), key=lambda pair: (display_priority(pair[1]), -pair[0]))
    if http_incident:
        focused = [pair for pair in ordered if display_priority(pair[1]) <= 1]
        ordered = focused or ordered
    selected = []
    seen_probes = set()
    for _, item in ordered:
        entry, raw = observations[item["id"]]
        if item["tool"] == "probe_http":
            fingerprint = (_url_key(raw.get("requested_url", "")), (entry.get("args") or {}).get("target_host", ""))
            if fingerprint in seen_probes:
                continue
            seen_probes.add(fingerprint)
        selected.append(item)
        if len(selected) == 4:
            break
    return {
        "status": status,
        "problem": diagnosis.get("root_cause", "") if status != ConclusionStatus.INCONCLUSIVE.value else "证据不足，尚未确认故障根因。",
        "evidence": selected,
        "impact": diagnosis.get("impact", "尚未确认影响范围。"),
        "next_steps": diagnosis.get("recommendation", "请补充故障 URL 和发生时间，并继续收集证据。"),
        "limitations": list(dict.fromkeys(gaps + limitations)),
        "verification_gaps": gaps,
    }
