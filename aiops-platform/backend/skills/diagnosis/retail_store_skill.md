# AWS Retail Store 只读业务排障

本说明适用于明确提到 AWS Retail Store、retail-store 或 retail-sample 的部署，不适用于其他商城。以下是业务知识，主机 IP、端口、容器名和当前状态仍必须从本次环境验证。不能因为此文档提到故障接口就认定故障已注入。

## 入口与依赖

- UI 的实际页面通常为 `/home`、商品页 `/catalog`；根路径 `/` 可能根据请求客户端返回纯文本欢迎页。根路径 200、`/actuator/health` 为 UP 不证明页面及商品接口正常。
- 从部署目录的 Compose 文件和 `docker compose ps` 确认 UI 发布端口及具体容器，不默认 80，不修改部署。
- 优先用 `probe_http` 检查用户原始完整 URL；若用户只说首页，依据实际发布端口和本业务路由确认 `/home`，明确记录该入口来自业务 SOP 与当前部署。
- UI 调用 catalog 商品服务，后者业务接口为 `/catalog/products`。从 Compose 的 UI 配置中确认 catalog 实际地址/端口，不把容器 IP 和虚拟机 IP 混用。
- 可通过同网络现有 UI 容器只读执行 `curl -sS -i --max-time 10 http://<已确认的catalog地址>/catalog/products`，核对状态码及内容。容器内命令仍遵守现有审批策略。

## 有限的故障控制检查

该示例服务提供故障控制接口。若商品接口异常，可在同一网络读取 catalog 的 `GET /chaos/status`，响应包含错误状态和延迟开关。只读 GET 不改变状态；根据本次返回判断开关是否启用、状态码是否与商品接口一致。

恢复建议可以指出需要关闭已确认的开关，但不得在只读诊断中执行 POST、DELETE、重启或改配置。修复完成后应复测原业务 URL 和商品接口，不能只看容器 healthy。

## 日志和调用链

- 从运行配置中确认 `OTEL_SERVICE_NAME`，并用 `list_trace_services` 核对 Tempo 中的实际 service.name。当前示例常见名称为 ui、catalog、cart、orders、checkout，必须实时验证，不把 Compose 容器名当遥测名称。
- 先复现业务请求，再在相关时间范围内找 UI/下游的失败或慢请求，读取与该请求有关的 Span。部分故障中间件不会生成 catalog 服务端 Span，应结合上游客户端 Span 与业务接口返回码。
- 时间窗口内没有日志/Trace 不代表正常，要报告缺失及检索条件。不要只根据 checkout/orders 自调用正常就排除 UI → catalog 故障。

## 报告

依次说明：哪个业务请求异常、哪个依赖出现什么响应、原因的支持证据、影响、待验证部分及恢复建议。将失败 HTTP 请求和当前开关状态分别引用为证据，不把二者的关联写成已完成修复。
