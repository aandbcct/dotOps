# 真实只读诊断验收

验证日期：2026-10-26。模型：`gpt-6.1-sol`。模式：`MOCK_ENABLED=false`。

| 场景 | 发起入口 | 实际工具证据 |
| --- | --- | --- |
| 正常运行 | 前端代理 `/aiops/api/multi-agent/process` | HTTP 200，healthy，24.41 ms |
| 手动停止测试服务 | 同一诊断接口 | connection_error，无 HTTP 状态码；主机名解析失败，1308.31 ms |
| 手动恢复测试服务 | 浏览器故障诊断页面 | HTTP 200，healthy，10.94 ms |

三次诊断均调用 `save_diagnosis_plan`、`probe_test_web`、`submit_diagnosis_result`，没有调用 Shell、SSH、审批或修复工具。
停止场景只说明探测点当前无法访问目标，不能将其当作服务停止的充分证明。
恢复动作由验收操作者通过 Docker 完成，不是 Agent 自动修复。

页面检查：HTTP 原始结果含目标、时间、状态码或错误、耗时；未执行的知识问答和审批节点不显示为完成。
Trace 页面在真实模式下显示未接入提示和空数据，不再自动生成模拟 Trace。

后端回归：在 Linux 后端容器执行 `python -m unittest test_real_diagnosis test_ai_chat_stream -v`，17 项通过。
前端 TypeScript 与 Vite 构建通过。真实模式与演示模式的分支由独立测试覆盖，当前运行环境保持真实模式。

边界：本次只验证固定 Web 健康接口，不代表 SSH、数据库、RAG、真实 Trace、RCA 或审批修复链路已接通。
