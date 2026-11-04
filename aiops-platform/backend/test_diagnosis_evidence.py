"""验证错误入口、空遥测和伪造引用不能冒充根因证据。"""

import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from app.agents.langgraph.diagnosis_evidence import assess_diagnosis, identify_observation
from app.agents.langgraph.nodes import react_agent
from app.agents.skill_manager import SkillManager


URL = "http://192.168.108.128:8888/home"


def probe(evidence_id, url=URL, status=500, target="192.168.108.128", **extra):
    return {"tool": "probe_http", "args": {"url": url, "target_host": target}, "result": {
        "evidence_id": evidence_id, "success": status == 200, "requested_url": url,
        "target_host": target, "http_status": status, "probe_attempted": True,
        "body_excerpt": "依赖响应异常", **extra,
    }}


def conclusion(*ids, causes=None):
    return {"root_cause": "依赖服务错误", "conclusion_status": "confirmed", "evidence_ids": list(ids),
            "cause_evidence_ids": causes or [], "impact": "商品页不可用", "recommendation": "先核对依赖"}


class DiagnosisEvidenceTests(unittest.TestCase):
    """确认结论必须绑定实际业务请求和独立的原因观察。"""

    def assess(self, history, diagnosis=None, query=None):
        return assess_diagnosis(query or f"商城页面 {URL} 返回500", diagnosis or conclusion("E1", "E2", causes=["E2"]), history)

    def test_shell_200_does_not_reproduce_business_failure(self):
        result = self.assess([probe("E1", url=URL.replace("/home", "/"), status=200)])
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("完整 URL", " ".join(result["limitations"]))

    def test_actual_503_with_independent_cause_is_retained(self):
        history = [probe("E1"), probe("E2", url="http://catalog:8080/chaos/status", status=200,
                                       body_excerpt='{"error_status":{"enabled":true,"code":503}}')]
        result = self.assess(history)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(result["evidence"]), 2)
        self.assertIn("HTTP 500", result["evidence"][0]["observation"])

    def test_timeout_is_an_observed_failure_but_not_a_cause(self):
        result = self.assess([probe("E1", status=None, transport_error="ReadTimeout")], conclusion("E1"))
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("ReadTimeout", result["evidence"][0]["observation"])

    def test_parameter_or_ssh_failure_is_not_http_reproduction(self):
        result = self.assess([probe("E1", status=None, probe_attempted=False, transport_error="SSH失败"), probe("E2", url="http://catalog:8080/catalog/products", status=503)])
        self.assertEqual(result["status"], "inconclusive")
        self.assertNotIn("E1", [item["id"] for item in result["evidence"]])

    def test_duplicate_probe_is_not_independent_cause(self):
        self.assertEqual(self.assess([probe("E1"), probe("E2")])["status"], "inconclusive")

    def test_same_localhost_url_on_wrong_machine_is_not_target_evidence(self):
        local = "http://localhost:8888/home"
        result = self.assess([probe("E1", url=local, target="10.0.0.99")], conclusion("E1"),
                             f"SSH 192.168.108.128 检查 {local} 页面500")
        self.assertEqual(result["status"], "inconclusive")

    def test_unspecified_url_does_not_accept_arbitrary_probe(self):
        result = self.assess([probe("E1"), probe("E2", url="http://catalog:8080/catalog/products", status=503)], query="192.168.108.128 商城首页500")
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("完整故障 URL", " ".join(result["limitations"]))

    def test_unknown_or_denied_evidence_id_cannot_confirm(self):
        blocked = {"tool": "execute_command", "result": {"evidence_id": "E2", "requires_confirmation": True}}
        self.assertEqual(self.assess([probe("E1"), blocked])["status"], "inconclusive")

    def test_process_reference_does_not_invalidate_independent_real_evidence(self):
        plan = {"tool": "save_diagnosis_plan", "result": {"success": True, "evidence_id": "plan"}}
        history = [plan, probe("E1"), probe("E2", url="http://catalog:8080/chaos/status", status=200,
                                            body_excerpt='{"enabled":true,"code":503}')]
        result = self.assess(history, conclusion("plan", "E1", "E2", causes=["E2"]))
        self.assertEqual(result["status"], "confirmed")
        self.assertNotIn("plan", [item["id"] for item in result["evidence"]])
        self.assertNotIn("evidence_id", identify_observation({"success": True}, "save_diagnosis_plan"))

    def test_unavailable_extra_query_cannot_override_valid_positive_evidence(self):
        failed_trace = {"tool": "analyze_trace", "result": {"success": False, "evidence_id": "E3", "error": "未知服务名"}}
        history = [probe("E1"), probe("E2", url="http://catalog:8080/chaos/status", status=200,
                                      body_excerpt='{"enabled":true,"code":503}'), failed_trace]
        result = self.assess(history, conclusion("E1", "E2", "E3", "invented", causes=["E2", "E3"]))
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual({item["id"] for item in result["evidence"]}, {"E1", "E2"})
        self.assertIn("已排除", " ".join(result["limitations"]))

    def test_empty_trace_is_a_limitation_not_cause_evidence(self):
        empty = {"tool": "analyze_trace", "args": {"service_name": "ui"}, "result": {"evidence_id": "E2", "success": True, "total_traces": 0}}
        result = self.assess([probe("E1"), empty])
        self.assertEqual(result["status"], "inconclusive")
        self.assertIn("不能证明", " ".join(result["limitations"]))

    def test_business_sop_matches_explicit_retail_deployment(self):
        skills = SkillManager().search_relevant_skills(f"SSH 192.168.108.128 /opt/retail-store {URL} 故障调用链", {"entities": {}})
        self.assertIn("retail_store_skill", skills)
        self.assertNotIn("retail_store_skill", SkillManager().search_relevant_skills("银行网站慢", {"entities": {}}))

    def test_summary_prioritizes_business_failure_and_cause_without_raw_trace(self):
        trace = {"tool": "analyze_trace", "args": {}, "result": {"success": True, "evidence_id": "E2", "trace_id": "trace-123", "trace": {
            "error_count": 2, "spans": [{"operation_name": "GET /home", "attributes": {"http.response.status_code": 500}},
                {"operation_name": "GET", "attributes": {"url.full": "http://catalog:8080/catalog/products", "http.response.status_code": 503}}],
        }}}
        result = self.assess([probe("E1"), trace])
        observation = result["evidence"][1]["observation"]
        self.assertIn("trace-123", observation)
        self.assertIn("catalog/products → HTTP 503", observation)
        self.assertNotIn('"spans"', observation)


class DiagnosisRetryTests(unittest.IsolatedAsyncioTestCase):
    """缺少证据只补查一次，第二次仍不足就明确结束为未定位。"""

    async def test_recheck_once_then_finish_inconclusive(self):
        fake = AsyncMock()
        fake.ainvoke.return_value = {"messages": [AIMessage(content="", tool_calls=[{
            "id": "submit", "name": "submit_diagnosis_result", "args": conclusion("forged"),
        }])]}
        state = {"user_query": f"{URL} 页面500", "messages": [], "execution_history": []}
        with patch.object(react_agent, "_get_react_agent", return_value=fake):
            first = await react_agent.react_agent_node(state)
            self.assertIsNone(first["diagnosis_result"])
            self.assertEqual(first["verification_attempts"], 1)
            second = await react_agent.react_agent_node({**state, **first})
            self.assertTrue(second["diagnosis_result"]["is_final"])
            self.assertEqual(second["diagnosis_assessment"]["status"], "inconclusive")
            self.assertIn("上次结论的证据缺口", fake.ainvoke.call_args.args[0]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
