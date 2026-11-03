# AWS Retail 本地观测接入

## 当前连接

```text
Ubuntu 192.168.108.128
  商城五个服务 -- OTLP/HTTP --> retail-collector:4318
  商城指标、node-exporter <-- Collector 内部抓取
  11 个业务容器的 JSON 日志 -- 只读文件采集 --> Collector
                         |
                         | OTLP/HTTP 至 192.168.108.1:4318
                         v
Windows dotops-local Collector --> Tempo（链路）/ Loki（日志）
Windows Prometheus --> 192.168.108.128:8889（应用与主机指标）
Grafana / dotOps 查询工具 --> Prometheus、Tempo、Loki
dotOps SSH 执行工具 --> dotops@192.168.108.128:22
```

- 商城：<http://192.168.108.128:8888>
- Grafana 看板：<http://127.0.0.1:3000/d/aws-retail-lab>
- dotOps 链路页：<http://127.0.0.1:8080/aiops/tracing>
- Grafana 使用现有本地管理员账号 `admin` / `admin123`。
- Tempo 与 Loki 保留周期为 48 小时，采集端不再做原配置的 10% 尾采样。
- Collector 统一不同语言运行时的同名 `go_*` / `process_*` 指标说明，避免聚合导出时 HELP 文本冲突；业务指标数值未修改。
- Ubuntu 增加了 Collector 和 node-exporter，目前共 13 个容器。Windows 增加了 Loki。

## 配置来源及日常操作

本目录的 `compose.override.yaml` 和 `collector.yaml` 是 Ubuntu 侧的部署模板。
远端配置分别位于 `/opt/retail-store/compose.override.yaml` 和 `/opt/retail-store/observability/collector.yaml`。
覆盖配置自动合并到原商城配置，开启 OTLP，并为日志增加 Compose 服务标签、10MB × 3 的轮转限制。
Collector 持久保存日志读取位置，按标签识别服务，不依赖固定容器 ID，也不挂载 Docker 管理 socket。

Ubuntu 日常启动命令：

```bash
cd /opt/retail-store
docker compose up -d --wait --wait-timeout 240
docker compose logs --tail 50 retail-collector
```

Windows 观测服务：

```powershell
cd D:\dev\dotOps
docker compose -f compose.local.yml up -d
```

SSH 私钥保存于 `dotops-local_backend-ssh` Linux 数据卷内，权限 600；不进入仓库。
该数据卷同时保存主机公钥和 SSH 配置，后端重建后仍可使用。主机身份验证保持开启。
`CMDB_SERVICE_LIST` 仅手动登记了本实验的服务名称，不代表已经实现资源发现或 Neo4j 拓扑导入。

Ubuntu 当前仍使用 DHCP 地址。地址或 VMware NAT 子网变更时，必须同步更新：本目录覆盖配置的端口绑定、Collector 上游地址、Prometheus 抓取目标、SSH 主机记录与配置，以及本机业务验证脚本。

## 已验证的证据（2026-11-02）

验收摘要：`D:\VMs\dotops-ubuntu\observability\acceptance.json`。
页面证据：同目录下的 `grafana.png` 和 `dotops-trace.png`。
业务验证脚本：`D:\VMs\dotops-ubuntu\smoke-test.ps1`，每次执行都会新增一笔测试订单，并保存每次 HTTP 请求的 Trace ID 到 `last-smoke-traces.json`。

| 验收项 | 结果 |
| --- | --- |
| Prometheus | 五个业务服务和 Ubuntu 主机共 6 个目标，均为 up |
| Loki | 收到 11 个商城容器的真实日志；dotOps 日志工具已实际查询到记录 |
| 商品请求 | `cb39bd990b604df181b3a063859b6cc9`，43 个 Span，包含 ui、catalog、cart，含 SQL 和 DynamoDB 操作 |
| 结算请求 | `40257a2ec6e4449ea7a01409d8b74cfd`，73 个 Span，覆盖 4 个服务及 Redis set |
| 下单请求 | `16ea342a08794589bf88c932a087506c`，70 个 Span，覆盖全部 5 个服务，包含订单 SQL 插入和 DynamoDB 删除 |
| 依赖关系 | 已从样本解析 ui → catalog/cart/checkout、checkout → orders；不是预置拓扑 |
| SSH 执行 | dotOps 的实际命令工具读取远程 `docker ps`，退出码 0 |
| 代码校验 | 20 项定向测试通过；前端构建通过 |

链路详情接口示例：

```text
GET http://127.0.0.1:18000/api/traces/16ea342a08794589bf88c932a087506c
```

dotOps 查询工具：

```text
POST /api/mcp/call/query_metrics
{"tool":"query_metrics","params":{"query":"up{job=\"aws-retail\",service!=\"\"}"}}

POST /api/mcp/call/query_logs
{"tool":"query_logs","params":{"query":"{service_namespace=\"aws-retail\",service_name=\"orders\"}","start":"1h ago","limit":20}}
```

## 运维场景覆盖与缺口

当前能提供服务可达性、应用请求数量和平均耗时、JVM/Node 运行时、订单连接池、Ubuntu CPU/内存/文件系统/网络、容器日志及跨服务数据库调用耗时。
这些证据适合开始验证服务停止、数据库断连、应用变慢、主机资源异常等基础场景；本次没有主动注入故障，也没有证明 Agent 已完成自动根因定位或修复。

仍有以下边界：

- 未安装 MariaDB/PostgreSQL/Redis 的专用 exporter，缺少数据库内部锁、慢查询、复制状态等指标。
- checkout 的原生 Prometheus 接口目前主要提供进程指标；HTTP 耗时通过真实链路查看。各语言指标名不同，不能直接套用统一的 `http_requests_total` 等默认查询。
- 日志按服务可查询，但尚未验证所有服务都能通过 Trace ID 一键关联日志；Java 的文件访问日志也不等于已采集的 stdout 日志。
- 健康检查与指标抓取也会产生链路；目前没有持续业务负载，查询结果可能以健康检查为主。可在链路页搜索 `catalog`，或直接查询上述业务 Trace ID。依赖统计最多基于 100 条搜索结果，不是全量请求统计。
- dotOps 的原“日志列表”仍是本地入库页面。真实容器日志通过 Loki/Grafana 和日志查询工具读取，本次未将其复制到 SQLite。
- 链路搜索、详情和服务依赖已支持真实 Tempo；旧的 `/api/traces/analyze` 与 RCA 演示接口仍受 Mock 模式限制，不冒充真实诊断。
- 告警规则、受控故障、错误链路召回率和诊断修复闭环尚待后续实验验收。

运维场景、原生故障接口、首批注入与恢复脚本、后续场景及记录标准见 [运维实验集](scenarios/README.md)。当前只准备实验，未注入故障、未宣称 Agent 诊断或修复成功。
