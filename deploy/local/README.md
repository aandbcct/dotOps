# 本地部署

在仓库根目录运行：

```powershell
docker compose -f compose.local.yml up --build -d
docker compose -f compose.local.yml ps
```

| 服务 | 地址 | 默认账号 |
| --- | --- | --- |
| AIOps | http://localhost:8080/aiops/ | admin / admin123 |
| 后端文档 | http://localhost:18000/docs | |
| Neo4j | http://localhost:7474 | neo4j / password |
| Grafana | http://localhost:3000 | admin / admin123 |
| Prometheus | http://localhost:9090 | |
| Tempo | http://localhost:3200/ready | |
| Alertmanager | http://localhost:9093 | |

所有发布端口仅绑定本机。数据保存在 Docker 命名卷中。

独立测试 Web 服务的启动、停止及恢复见 [测试服务说明](../test-web/README.md)。

## 后端开发模式

本地 `aiops-platform/backend` 只读挂载到容器 `/app`，`/app/data` 仍使用原有可写数据卷。
Uvicorn 已启用自动重载，并使用轮询监听 Windows 挂载目录：

- 修改 `app/` 下的 Python 源码或 `skills/` 下的 Markdown 后，后端自动重载，无需构建镜像。
- 修改 `.env.local` 或 Compose 配置后，需要重新创建后端容器。
- 修改 `requirements.txt` 或 Dockerfile 后，需要重新构建并创建后端容器。
- 前端源码没有启用挂载和自动重载，仍需要重新构建前端。

自动重载会重启后端进程，进行中的请求会中断，内存会话和图检查点会丢失；数据卷中的文件不受影响。
此模式仅用于本机开发。

## 真实模式与演示模式

`.env.local` 中的 `MOCK_ENABLED=false` 为默认真实模式：知识查询失败不再回退模拟数据。
Trace、服务依赖和 RCA 页面接口尚未接入真实实现，返回 HTTP 503，明确说明未接入。
`MOCK_ENABLED=true` 显式启用演示数据，返回结果带 `source=mock_data`。
修改开关后重新创建后端，不需要重建镜像。

故障诊断页面分别显示模型建议、实际运行节点和 HTTP 原始证据。
流程结束不表示故障修复；未运行节点显示为“未执行”。

在故障诊断输入：

```text
诊断本地测试 Web 服务 test-web 的 HTTP 健康接口。只做只读探测，不使用 SSH，不执行命令。
```

该场景仅向模型提供保存计划、HTTP 探测和提交结论三个工具。
探测只允许 `http://test-web/health`，不跟随重定向，不开放任意 URL 或命令执行。
连接失败、超时或 HTTP 非 200 都保留原始结果，不能据此断言服务停止或已经找到根因。

## 模型配置

编辑仓库根目录的 `.env.local`（已被 Git 忽略）：

```dotenv
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_MODEL=your-model-name
OPENAI_API_KEY=your-api-key
```

本地模型服务使用 `http://host.docker.internal:端口/v1`，不要使用容器内的 `localhost` 指代 Windows 主机。

保存后重新创建后端，以加载新的环境变量：

```powershell
docker compose -f compose.local.yml up -d --force-recreate backend
```

当前本地配置使用 `https://jojocode.com/v1` 的 `gpt-6.1-sol`，
密钥通过 `.env.local` 中的 `${JOJOCODE_API_KEY}` 引用系统环境变量，不保存明文。
运行 Compose 的终端需要能读取该变量；修改 Windows 用户环境变量后，旧终端可能需要重新打开。
RAG 服务源码未包含在本仓库中，后续通过 `RAG_SERVICE_URL` 指定独立服务。
SSH 远程运维需要另外设置用户名并挂载自己的私钥；初始部署未挂载密钥。

## 停止与日志

```powershell
docker compose -f compose.local.yml logs --tail 100 backend
docker compose -f compose.local.yml down
```

`down` 保留数据卷。后端源码修改自动重载；修改依赖或前端源码后，重新执行带 `--build` 的启动命令。
监控服务的启动不代表已接入真实业务数据；当前 Prometheus 抓取自身与 Collector 指标。
后端当前缺少完整 OpenTelemetry 依赖，自动链路追踪未启用。

## 本次验证（2026-10-10）

八个容器运行中；前端、后端、Neo4j 健康检查通过。
通过前端代理完成管理员登录及登录后的 `/api/auth/me` 请求。
Neo4j 执行 `RETURN 1` 成功。
Grafana、Prometheus、Tempo、Alertmanager 就绪接口返回 HTTP 200。
Grafana 的 Prometheus 数据源查询成功，两个抓取目标均为 `up`。
Tempo 和 Alertmanager 的 Grafana 数据源健康检查接口未能提供有效验证，
上面的就绪结果来自服务自身接口；未验证业务链路、真实告警投递或模型调用。

## 模型接入验证（2026-10-26）

- `gpt-6.1-sol` 在 JojoCode 的 Chat Completions 接口成功返回结构化工具调用。
- 后端已重新创建，容器加载新地址及模型；后端健康检查和前端代理健康请求通过。
- `/api/multi-agent/process` 完成一次约 40 秒的概念问答请求，意图识别为 `GENERAL_QA`。
- 该请求的回答不正确：知识问答节点没有传入用户原始问题，后续提示词要求生成故障建议并含示例故障；缺失知识时还会使用模拟拓扑。HTTP 状态码问题因此返回 Redis 排查内容，不能视为业务验收通过。
- 本次没有执行系统命令，没有验证真实目标诊断、审批恢复、RAG 或真实追踪链路。
