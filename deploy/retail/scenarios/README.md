# Retail 运维实验集

目标：亲自使用 dotOps 完成运维过程，沿真实执行轨迹回看设计，再积累可复核的实验数据。当前是本地实验环境，不是生产故障数据。

## 当前状态与文件

- `cases.json`：16 个用例，包含健康基线、5 个首批故障、5 个下一批场景及 5 个暂缓场景。所有故障均为 **not_run**，未证明 Agent 已能诊断或修复。
- `Invoke-RetailFault.ps1`：只支持 R00–R05，默认 Plan；Inspect 只读，Inject 注入，Recover 恢复。没有后台循环、自动修复或持续压测。
- `run-record.template.json`：每轮复制一份填写，记录节点、工具、证据、诊断结果、恢复和自己的理解。
- `runs/`：本地动作记录与实验材料，已忽略 Git。模板中的空值不能当作 0 或成功。

环境：Ubuntu `192.168.108.128`，SSH 用户 `dotops`，Compose 项目 `retail-sample`，目录 `/opt/retail-store`；Retail 镜像 v1.6.3。地址仍为 DHCP；地址变化后必须先更新整条观测链路及脚本，不能只改一个 URL。

2026-11-02 本轮只读核对：13 个容器运行；已配置健康检查的容器均 healthy；catalog 商品接口、catalog/cart/checkout/orders 健康接口均 HTTP 200。catalog 和 checkout 的 GET `/chaos/status` 均返回开关关闭；cart/orders 同一路径 GET 返回 405，与源码未定义 GET 一致。**这不是故障注入验收。**

本轮脚本验证：PowerShell 语法解析、16 个用例字段与编号检查、6 个 Plan 路径通过；实际 SSH Inspect 和注入前只读基线检查通过。模拟 SSH 覆盖 5 种注入/恢复命令、异常基线阻断及远端失败记录；模拟成功不计入真实故障覆盖率。脚本以 UTF-8 BOM 保存，兼容 Windows PowerShell 的中文读取。

2026-11-03 修复：Windows PowerShell 中文默认编码会把 SSH 返回的 UTF-8 容器 JSON 解码错误，导致注入前 ConvertFrom-Json 失败。SSH 调用现在临时使用 UTF-8 解码，并在成功或异常后恢复原终端编码；Inspect 的远端参数改用单引号，避免启动 SSH 时丢失内层双引号。已在 Windows PowerShell 5 的 GBK 条件下通过实际基线和 Inspect 检查，PowerShell 7 基线与异常恢复也通过。此次检查没有执行注入，catalog 故障开关仍关闭。

## dotOps 能力与实验边界

| 能力 | 当前证据 | 本实验怎样使用 / 尚缺什么 |
|---|---|---|
| 自然语言解析、SSH 信息补充、Skill 匹配、Agent 工具循环、结果收口 | `app/agents/langgraph/graph.py`、`nodes/ssh_check.py`、`nodes/react_agent.py`；目录均位于 `aiops-platform/backend/` | 图中存在这些环节；每轮记录实际 visited_nodes 和工具结果。SSH 前置节点只检查信息，不是实际登录验证 |
| 真实 SSH 和容器排查 | `app/agents/tool_registry.py` 的 execute_command；前次已实际读取 VM docker ps | 可检查 ps、logs、inspect、连接情况。不要读取完整容器环境变量而把数据库密码保存进实验材料 |
| 指标 | 同文件 mcp_call → query_metrics/query_metrics_range；`deploy/local/prometheus.yml` | 已接五个服务和主机；服务 up、请求、连接池、主机资源。未有数据库内部 exporter；up 不是业务健康 |
| 日志 | mcp_call → query_logs；`app/agents/grafana_log_client.py` | Loki 按服务和时间查询；不等于原日志列表页面，未证明全服务日志带 Trace ID |
| 真实调用链 | analyze_trace / analyze_service_dependency；`app/observability/tempo_query.py` | 可搜索、取详情和分析 Span。入口/下游错误不保证每次都有完整服务端 Span；必须记录缺失 |
| 诊断方法 | `skills/diagnosis/debug_skill.md`、`skills/network/connectivity_skill.md`、`skills/monitoring/log_analysis_skill.md`、`otel_trace_skill.md` | 是诊断步骤，不等于 Agent 已完成场景；各语言真实指标名不同，不能照抄不存在的默认指标 |
| 数据库、Redis、CPU、慢 SQL、死锁方法 | 数据库/诊断/容量/监控目录里的对应 Skill | 有文档和通用命令入口；不代表特定数据库凭据、工具、观测、数据和实验已准备 |
| 风险检查与审批工具 | tool_registry 的命令分级及审批工具；graph 的 human_review 中断 | 机制存在，真实恢复闭环尚未验证。首轮只读诊断，人工恢复。查询意图目前并没有普遍的工具权限隔离，不能只凭提示词声称禁止写入已被技术强制 |
| 告警自动触发 | 当前实验尚未验收 Retail 业务告警规则和触发路径 | 首批由人提交异常；不能据此报告自动发现耗时 |
| GNN / 智能 RCA / 异常检测 | 有工具和方法文档，但不代表适用于本实验数据；旧 `/api/rca` 和 `/api/traces/analyze` 仍受 Mock 模式限制 | 本集先验收真实工具取证和诊断，不把演示算法输出当本地业务根因证据 |
| Kubernetes、云主机、负载均衡、云数据库高可用 | 有相应 Skill | 本地 Docker Compose 无这些资源，不纳入当前可执行集；不需要为了覆盖所有 Skill 部署新平台 |

此前接入证据见 [观测接入说明](../README.md)；当日实测值与后续新运行结果必须分别记录。

## AWS 自带什么故障机制

已按 **v1.6.3 / d7a380b843e0759e58754f74188ea18252a51415** 核对源码，不能拿最新 main 或其他 AWS Workshop 的功能冒充当前部署能力。

| 服务 | 原生机制 | 影响范围及注意点 |
|---|---|---|
| catalog、cart、checkout、orders | POST `/chaos/status/503`；DELETE `/chaos/status` | 返回人工错误；catalog 业务路径 `/catalog/*`，cart `/carts*`，orders `/orders*`；checkout 的中间件挂在 `checkout/*splat`。不能假设健康、指标和所有路径都受影响 |
| 同上 | POST `/chaos/latency/1000`；DELETE `/chaos/latency` | 加入服务级延迟，不是慢 SQL，也不是单次请求专用开关 |
| 同上 | POST `/chaos/health`；DELETE `/chaos/health` | 人工使健康检查失败；必须另测业务。Docker 显示 unhealthy 不会仅因这个状态自动重启容器 |
| catalog、checkout | GET `/chaos/status` | 可查延迟和错误开关，不包含健康开关；cart/orders 没有对应 GET |
| ui | GET `/utility/status/{code}` | 仅这次 utility 请求返回指定状态，不会使商品/下单业务持续报错 |
| ui | GET `/utility/stress/{iterations}`、`/utility/panic` | 计算负载、进程退出；本轮不调用。restart:always 下退出可能快速重启，与人工 stop 的持久停止不同 |
| 官方 load-generator | 业务发压脚本 | 上游提供，不代表本机已经部署或容量已标定；可供后续并发场景复用 |

没有必要写一套新的业务故障实现。首批脚本只是通过 SSH 调用内部接口或 Docker stop/start；不增加宿主机映射端口、不修改业务源码。

上游依据：

- [catalog README](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/catalog/README.md)、[路由](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/catalog/main.go)、[故障中间件](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/catalog/middleware/chaos.go)
- [cart 故障控制器](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/cart/src/main/java/com/amazon/sample/carts/chaos/ChaosController.java)、[orders 故障控制器](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/orders/src/main/java/com/amazon/sample/orders/chaos/ChaosController.java)
- [checkout 控制器](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/checkout/src/chaos/chaos.controller.ts)、[中间件挂载](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/checkout/src/app.module.ts)
- [UI 工具接口](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/ui/README.md)、[负载生成器](https://github.com/aws-containers/retail-store-sample-app/blob/v1.6.3/src/load-generator/README.md)

特别注意：catalog 先挂故障中间件，再挂服务端追踪中间件。人工延迟可能不计入 catalog 的业务服务端 Span；提前返回的 503 可能没有那个 Span。应结合 UI 的下游 client Span、客户端耗时和开关证据定位，不能认定“SQL Span 快，所以请求没有延迟”。

## 场景目录

| 编号 | 场景 | 阶段 / 现状 |
|---|---|---|
| R00 | 健康基线，无故障 | 首批，先跑 |
| R01 | catalog 人工返回 503 | 首批，脚本已准备，未注入 |
| R02 | catalog 人工延迟 1000ms | 首批，脚本已准备，未注入 |
| R03 | 健康检查失败而商品业务正常 | 首批，脚本已准备，未注入 |
| R04 | catalog 容器停止 | 首批，脚本已准备，未注入 |
| R05 | catalog-db 停止 | 首批，脚本已准备，未注入 |
| R06 | cart 返回 503 | 下一批，需完善购物车会话取证 |
| R07 | checkout 延迟 | 下一批，需固定结算触发步骤 |
| R08 | orders 返回 503 | 下一批，需记录失败订单、Trace 与副作用 |
| R09 | checkout-redis 停止 | 下一批，需验证业务影响及恢复行为 |
| R10 | orders-db 停止 | 下一批，需验证数据库写入和失败后的重试行为 |
| R11 | UI CPU 升高 | 暂缓，需标定负载和资源上限 |
| R12 | 真实锁等待 | 暂缓，需真实阻塞事务和数据库证据；普通 SELECT 不一定被行锁阻塞 |
| R13 | 真实慢 SQL | 暂缓，需数据规模、真实 SQL、慢查询记录与执行计划 |
| R14 | 并发下单请求精准排障 | 暂缓，需日志 Trace ID 关联及选择性故障方案 |
| R15 | 服务间网络隔离 | 暂缓，需精确规则和回滚方案 |

RabbitMQ 停止、OpenSearch 停止、DynamoDB 停止暂不列成已准备场景：先确认实际业务路径和可观察影响。当前搜索默认未开启，不能默认停 OpenSearch 就会影响商品列表；消息发送失败也不能直接等价于订单创建失败。

建议顺序：R00 → R01 → R05 → R04 → R02 → R03。先做一次有人指导的学习轮，再做提示固定的评估轮，不能把调试过程中反复透露答案的结果混入独立诊断成功率。

## 第一次怎样操作

在 Windows PowerShell 中进入当前目录：

```powershell
cd D:\dev\dotOps\deploy\retail\scenarios
.\Invoke-RetailFault.ps1 -CaseId R01
.\Invoke-RetailFault.ps1 -CaseId R00 -Action Inspect
```

默认 Plan 不连接环境。Inspect 展示容器、开关、健康和商品响应，不替你判定完整业务成功。先打开商城商品页并保存正常内容、耗时及 Trace ID。商品页面的浏览器请求与脚本请求不是同一请求，必须按实际请求分别记录。

开始 R01 时才执行：

```powershell
.\Invoke-RetailFault.ps1 -CaseId R01 -Action Inject
```

脚本先检查 13 个容器、catalog 开关、健康和商品内容；只检查本批相关基线，不保证所有服务都没有隐藏故障。一次只做一个场景，不与他人同时操作。注入开关不会自动到期；若命令超时，结果可能未知，必须 Inspect 或执行对应恢复，不能把超时直接当未注入。

随后重新打开商品页，记录实际状态/内容/时间。**只复制 cases.json 中该用例的 prompt 给 dotOps，不把整份用例、注入命令或动作日志送给 Agent。** catalog 503 的真相与数据库停止的真相不同，只有明确取证后才能宣称识别了具体机制。

首轮让 dotOps 只读诊断；记录它是否实际遵守边界，不将文字限制当技术权限保证。人工恢复：

```powershell
.\Invoke-RetailFault.ps1 -CaseId R01 -Action Recover
.\Invoke-RetailFault.ps1 -CaseId R00 -Action Inspect
```

恢复后等待健康探测，连续三次验证相同商品请求及页面内容。恢复命令退出码为 0 不等于业务恢复。R05 启动数据库后应用可能需要重连时间；若仍失败，记录并继续诊断，不擅自将重启全部服务算作标准恢复。

脚本不清理数据、不重建容器、不删除 volume；stop/start 保留数据库数据。Agent 后续修复实验单独记录审批、执行者和验证结果。人工运行这个脚本成功，不等于 Agent 修复成功。

SSH 中断时，使用已有连接方式登录 Ubuntu 后执行对应恢复命令。R01/R02/R03 分别 DELETE 对应开关；R04/R05 分别 start catalog/catalog-db。不要用 down -v 恢复。

## 记录、评分与简历数字

每轮从模板复制记录，保存：注入动作、实际现象、输入原文、会话 ID、实际节点、全部工具参数/结果、Trace ID、查询时间窗口、最终结论、人工/Agent 恢复方式及业务验证。源代码含未提交修改时，记录 commit 之外还要保存本轮 diff，不能只报 HEAD。

首轮只读实验的通过条件：故障确实生效；定位正确；证据支持结论；没有未授权写入；人工恢复后业务验证通过。分别记录“服务定位正确”和“具体机制有证据”，不能因为猜中服务就算精确根因正确。

- 未复现故障：invalid_injection，不计作诊断成功；同时报告注入有效率。
- 故障有效但诊断错误/超时/工具不可用：保留为失败，不删掉难例。
- 诊断耗时：从提交 dotOps 到首次有证据的正确结论；只完成工具连接不算完成诊断。
- 恢复耗时：从恢复操作开始到业务恢复验证，不等于自动发现到恢复的全程 MTTR。
- 没有自动告警验收，不报告自动发现时长。没有可比人工基线，不报告“效率提升 X%”。
- 成功率写明确分子/分母，同时报告场景数与重复次数。学习轮与正式评估轮分开；同一场景重复结果不能冒充更多故障类型。
- 固定模型、提示、版本、超时与环境；发生代码修复后另开一组结果，保留改前失败。原始观测只保留 48 小时，实验后立即导出需要的证据并脱敏。

每轮还写三句话：我事先怎样判断；dotOps 哪一步符合/违背预期；这让我理解了当初哪个设计取舍。原始动机由本人确认，不从当前实现反推成当年事实。

新增场景先补 cases.json 的业务触发、真相、前提、证据、通过标准和恢复方法，再接入执行脚本。阻塞场景继续保持 not_run；不会因为预留了编号就计入已覆盖能力。
