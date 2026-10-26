import json
import re
import os
from typing import Dict, Any, Optional
import httpx
from neo4j import GraphDatabase
from openai import OpenAI
from app.core.config import settings
from .schemas import KnowledgeResult, TopologyInfo
from ..utils.logger import get_logger

logger = get_logger("knowledge")

class KnowledgeExpertAgent:
    """
    Knowledge Expert Agent (记忆层)
    核心职责：结合 KG 拓扑与 RAG 知识，提供决策依据。
    同时集成 debug_skill.md 中的常规排查方法。
    """
    
    def __init__(self):
        self.client = OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )
        self.neo4j_driver = None
        self.debug_skill_content = self._load_debug_skill()
    
    def _load_debug_skill(self) -> str:
        """
        加载 debug_skill.md 的内容
        """
        try:
            debug_skill_path = os.path.join(os.path.dirname(__file__), "..", "debug_skill.md")
            if os.path.exists(debug_skill_path):
                with open(debug_skill_path, 'r', encoding='utf-8') as f:
                    return f.read()
        except Exception as e:
            logger.warning(f"Could not load debug_skill.md: {e}")
        return ""
    
    def _get_neo4j_driver(self):
        if self.neo4j_driver is None:
            self.neo4j_driver = GraphDatabase.driver(
                settings.NEO4J_URI,
                auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
            )
        return self.neo4j_driver
    
    def close(self):
        if self.neo4j_driver is not None:
            self.neo4j_driver.close()
            self.neo4j_driver = None
    
    def _build_prompt(self, service: str, symptom: str, topology_info: Dict, rag_context: str) -> str:
        debug_skill_section = ""
        if self.debug_skill_content:
            symptom_lower = symptom.lower()
            
            relevant_sections = []
            if any(keyword in symptom_lower for keyword in ["disk", "磁盘", "空间", "full", "no space"]):
                relevant_sections.append("1. Disk Full (磁盘空间不足)")
            if any(keyword in symptom_lower for keyword in ["network", "网络", "连接", "timeout", "dns"]):
                relevant_sections.append("2. Network Broken (网络连接异常)")
            if any(keyword in symptom_lower for keyword in ["memory", "内存", "oom", "out of memory"]):
                relevant_sections.append("3. Out of Memory (内存溢出/不足)")
            
            if relevant_sections:
                debug_skill_section = f"""

## 常规排查方法参考 (来自 debug_skill.md)
以下是针对当前问题的常规排查步骤：

{self.debug_skill_content[:2000]}

请参考以上排查方法，结合实际情况给出建议。
"""
        
        return f"""你是一个运维知识问答助手，只能依据下面实际提供的内容回答。
不得声称已连接未提供数据的系统，不得把模拟数据当成事实。
没有证据时明确说明，建议不等于已执行的操作，也不等于已确认根因。

系统为你检索了以下上下文：

拓扑关系 (KG): {json.dumps(topology_info, ensure_ascii=False, indent=2)}
RAG 知识库参考: {rag_context}
{debug_skill_section}
Task
针对服务 {service} 的现象 {symptom}：

拓扑洞察：检查 KG 中的依赖关系。是否依赖了近期有变更或故障的组件（如 Redis, DB）？
经验复用：对比历史案例，寻找最相似的解决方案。
SOP 推荐：匹配最标准的应急处置步骤。
常规排查：参考 debug_skill.md 中的常规排查方法。

Output Format
结构化建议：

拓扑风险点: [仅列出有真实证据支持的风险；否则注明无数据]
推荐方案: [仅提出建议，不表示已执行]
执行 SOP: [仅引用实际检索到的文档；没有文档则注明未接入]
排查建议: [参考 debug_skill.md 中的具体排查步骤]"""

    async def query(self, service: str, symptom: str, topology_info: Dict = None, rag_context: str = None, user_query: str = "") -> KnowledgeResult:
        if topology_info is None:
            topology_info = await self._query_knowledge_graph(service)
        if rag_context is None:
            rag_context = await self._query_rag(f"{service} {symptom} 故障处理")
        
        prompt = self._build_prompt(service, symptom, topology_info, rag_context)
        if user_query:
            prompt += f"\n用户原始问题：{user_query}\n优先回答原始问题。概念问答不需要生成故障分析报告。"
        
        try:
            response = self.client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            content = response.choices[0].message.content.strip()
        except Exception as e:
            content = f"知识分析暂时不可用: {str(e)}"
        
        return KnowledgeResult(
            service=service,
            symptom=symptom,
            knowledge_report=content,
            topology_info=TopologyInfo(**topology_info) if isinstance(topology_info, dict) else TopologyInfo(),
            rag_context=rag_context or "",
            data_sources={
                "answer": "model_generated",
                "topology": topology_info.get("source", "unavailable"),
                "rag": "mock_data" if settings.MOCK_ENABLED else ("ops_rag_service" if rag_context else "unavailable"),
            },
        )
    
    async def _query_knowledge_graph(self, service: str) -> Dict:
        if settings.MOCK_ENABLED:
            return self._get_mock_topology(service)
        try:
            driver = self._get_neo4j_driver()
            
            with driver.session() as session:
                result = session.run("""
                    MATCH (s {name: $name})
                    OPTIONAL MATCH (s)-[r:DEPENDS_ON]->(dep)
                    OPTIONAL MATCH (s)-[r2:RUNS_ON]->(run)
                    OPTIONAL MATCH (s)-[r3:CONNECTED_TO]->(conn)
                    RETURN s, 
                           collect(DISTINCT {name: dep.name, type: labels(dep)[0]}) as dependencies,
                           collect(DISTINCT {name: run.name, type: labels(run)[0]}) as runs_on,
                           collect(DISTINCT {name: conn.name, type: labels(conn)[0]}) as connections
                """, name=service)
                
                record = result.single()
                
                if record and record["s"]:
                    node = dict(record["s"])
                    return {
                        "service": service,
                        "properties": node,
                        "dependencies": [d for d in record["dependencies"] if d["name"]],
                        "runs_on": [r for r in record["runs_on"] if r["name"]],
                        "connections": [c for c in record["connections"] if c["name"]],
                        "source": "neo4j_kg"
                    }
                
                result = session.run("""
                    MATCH (s)
                    WHERE s.name CONTAINS $keyword OR s.name = $keyword
                    OPTIONAL MATCH (s)-[r:DEPENDS_ON]->(dep)
                    RETURN s.name as name, labels(s)[0] as type,
                           collect(DISTINCT {name: dep.name, type: labels(dep)[0]}) as dependencies
                    LIMIT 1
                """, keyword=service.replace("-service", ""))
                
                record = result.single()
                if record:
                    return {
                        "service": record["name"],
                        "type": record["type"],
                        "dependencies": [d for d in record["dependencies"] if d["name"]],
                        "source": "neo4j_kg_fuzzy"
                    }
                
                return {"service": service, "source": "no_data", "status": "no_data"}
                
        except Exception as e:
            logger.error(f"Neo4j 查询失败: {str(e)}")
            return {
                "service": service,
                "error": str(e),
                "source": "error",
                "status": "error",
            }
    
    async def _query_rag(self, query: str) -> str:
        if settings.MOCK_ENABLED:
            return "[模拟数据] 示例 SOP：先检查服务 HTTP 健康接口，再按实际响应继续排查。"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{settings.RAG_SERVICE_URL}/api/chat",
                    json={"query": query}
                )
                
                if response.status_code == 200:
                    data = response.json()
                    return data.get("answer", "")
        except Exception as e:
            logger.warning(f"RAG 服务 ({settings.RAG_SERVICE_URL}) 不可用: {str(e)}，没有检索上下文")
        return ""
    
    def _get_mock_topology(self, service: str) -> Dict:
        logger.warning(f"[MOCK DATA] 演示模式返回服务 '{service}' 的模拟拓扑，不能用于真实诊断决策。")
        
        topology_map = {
            "order-service": {
                "upstream": ["api-gateway", "user-service"],
                "downstream": ["payment-service", "inventory-service", "mysql-master", "redis-cluster"],
                "critical_deps": ["mysql-master", "redis-cluster"]
            },
            "payment-service": {
                "upstream": ["order-service"],
                "downstream": ["mysql-master", "kafka-cluster"],
                "critical_deps": ["mysql-master"]
            },
            "user-service": {
                "upstream": ["api-gateway"],
                "downstream": ["mysql-master", "redis-cluster"],
                "critical_deps": ["mysql-master", "redis-cluster"]
            },
            "prod-server-01": {
                "type": "Server",
                "ip": "192.168.1.10",
                "dependencies": ["session-cache-redis", "user-mysql-master"]
            },
            "prod-server-02": {
                "type": "Server",
                "ip": "192.168.1.12",
                "dependencies": ["order-postgres", "session-cache-redis"]
            }
        }
        
        mock_topology = topology_map.get(service, {})
        # 历史服务器示例保存的是名称列表，输出统一为结构化依赖。
        mock_topology = {**mock_topology, "dependencies": [
            {"name": name} for name in mock_topology.get("dependencies", [])
        ]}
        return {
            "service": service,
            "dependencies": [],
            **mock_topology,
            "recent_changes": [
                {"component": "redis-cluster", "change": "主从切换", "time": "5分钟前"}
            ],
            "_is_mock": True,
            "source": "mock_data",
            "disclaimer": "演示模式：此为模拟数据，不表示真实故障。"
        }
    
    async def get_topology_graph(self, service: str = None, depth: int = 2) -> Dict:
        try:
            driver = self._get_neo4j_driver()
            
            with driver.session() as session:
                if service:
                    result = session.run(f"""
                        MATCH path = (s {{name: $name}})-[*1..{depth}]-(related)
                        RETURN s, related, relationships(path) as rels
                    """, name=service)
                else:
                    result = session.run(f"""
                        MATCH path = (a)-[r:DEPENDS_ON|RUNS_ON|CONNECTED_TO]-(b)
                        RETURN a, b, relationships(path) as rels
                        LIMIT 100
                    """)
                
                nodes = {}
                edges = []
                
                for record in result:
                    for node_key in ["s", "a", "related", "b"]:
                        node = record.get(node_key)
                        if node:
                            node_id = node.element_id
                            if node_id not in nodes:
                                nodes[node_id] = {
                                    "id": node_id,
                                    "label": dict(node).get("name", "unknown"),
                                    "type": list(node.labels)[0] if node.labels else "Node",
                                    "properties": dict(node)
                                }
                    
                    rels = record.get("rels", [])
                    for rel in rels:
                        try:
                            edges.append({
                                "source": rel.start_node.element_id,
                                "target": rel.end_node.element_id,
                                "type": rel.type
                            })
                        except:
                            pass
                
                return {
                    "nodes": list(nodes.values()),
                    "edges": edges,
                    "source": "neo4j_kg"
                }
                
        except Exception as e:
            return {
                "nodes": [],
                "edges": [],
                "error": str(e),
                "source": "error"
            }
