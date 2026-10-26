# 本地测试 Web 服务

这是独立运行的 Nginx 服务，作为后续只读诊断的真实目标，不是模拟诊断结果。
先启动本地平台，确保 `dotops-local_default` 网络存在，再从仓库根目录执行：

```powershell
docker compose -f compose.test.yml up -d
```

- 本机访问：`http://localhost:18080/`，健康接口为 `/health`。
- DotOps 后端容器访问：`http://test-web/health`。
- 配置只绑定本机端口，不挂载 Docker 控制接口，不赋予 Agent 启停容器的权限。

手动制造故障和恢复，仅操作这个测试服务：

```powershell
docker compose -f compose.test.yml stop test-web
docker compose -f compose.test.yml start test-web
```

停止后 HTTP 连接应失败；恢复后 `/health` 应返回 HTTP 200。
Agent 已接入 `probe_test_web` 只读探测工具，可从故障诊断页面发起检查。
模型建议与真实探测证据分开展示，服务的启动和停止仍由操作者手动完成。

验收时先启动检查，再停止检查，最后恢复并重新检查。目标错误不会自动重启服务。
