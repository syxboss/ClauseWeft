# 给仓库编码 Agent 的指南

本文件适用于在此仓库修改 ClauseWeft 源码、测试和文档的编码助手。运行时角色、输入和交接条件见 [Agent 设计](docs/08-Agent设计.md)。

## 定位改动

从 `pyproject.toml` 查看安装包与终端入口。源码启动命令是 `PYTHONPATH=src python3.11 -m clauseweft`，核心实现位于 `src/clauseweft/`。阅读目标模块的 README 和相应测试，再沿现有入口追踪调用。设计文档覆盖后续计划，当前已开放的接口及适配器见[构建与运行](docs/06-构建与运行.md)。

| 改动涉及 | 优先阅读 | 验证入口 |
|---|---|---|
| 任务方式、角色交接、自动拆分与汇总 | `api/run_planning.py`、`workflow`、`runs/results.py`、`api/web/scripts/` | `test_simple_tasks.py`、`test_complex_workflow.py`、`test_harness.py`、`test_console_tasks.py` |
| 工具选择、资料查询、审批和追问 | `project_tools/`、`api/run_routes.py`、`api/conversations.py` | `test_project_tools.py`、`test_conversations.py` |
| 个人配对、外部 Agent 和冻结候选评审 | `api/personal_pairing.py`、`api/agent_jobs.py`、`agent_runtime/` | `test_personal_pairing.py`、`test_agent_jobs.py`、`test_agent_runtime.py` |
| 选模、网关、预算和重试 | `routing`、`catalog`、`gateway`、`runs` | `test_route_preview.py`、`test_gateway.py`、`test_execution.py` |
| 需求、独立评审、依赖和返工 | `requirements`、`reviews`、`workflow`、`api/delivery.py` | `test_decisions.py`、`test_delivery_actions.py` |
| 补丁、隔离测试和交付导出 | `workspace`、`testing`、`api/delivery_jobs.py`、`api/delivery_evidence.py` | `test_delivery.py`、`test_workbench.py` |
| 工作台 HTTP、存储和配对评估 | `api/application.py`、`api/evaluation_routes.py`、`storage`、`evaluation` | `test_workbench.py`、`test_evaluation_api.py` |
| 账号权限、项目需求、角色和 SOP 发布 | `api/accounts.py`、`api/access_policy.py`、`api/platform.py`、`workflow` | `test_platform.py`、`test_workspace_permissions.py`、`test_sop.py` |
| 技能、请求上下文、检查点和审计证明 | `harness`、`audit`、`storage`、`runs` | `test_harness.py`、`test_audit.py`、`test_delivery_actions.py` |

## 保持关键契约

- 复杂任务的模型只提供分工说明。服务端在当前规划尝试成功的事务中创建分支和汇总依赖，每个分支接收原目标和自己的分工，全部成功后汇总。普通任务引用角色时，服务端冻结角色版本和技能，用户目标单独保存。
- 路由预览使用冻结输入，在本地计算。实际派发前再次检查当前授权、端点绑定、模型身份和预算。持久化派发意向和预留后才向独立网关发请求；结果未知时保留可对账的记录。
- API 客户端重试同一写操作时沿用原幂等键。修改已有版本资源时检查 `If-Match`。业务写入、版本变更、事件和幂等结果在对应事务中提交。
- 需求审批绑定版本，代码评审和测试绑定候选。输入变更、取消或迟到回执应保留历史，同时检查当前世代是否仍可发布结果。
- 模型提交的补丁受工作包路径限制。将候选应用于私有 Git 工作区，用固定镜像执行已登记的验收命令。通过与否取决于服务端保管的必测清单、真实退出码和输出证据。
- 团队权限由服务端核验，签署和审计记录具名账号。运行保存发布时的模板与角色快照，编辑草稿或并发发布不能替换已经准备的执行计划。
- 实际消息工件与派发意向共同提交。派生运行保留来源并重新取得人工审批。审计成功事实与业务修改共同提交，拒绝、失败、读取和导出也可追溯。导出保存原始响应，离线校验同时核对链和证据分区。

## 验证与交付

在仓库根目录运行以下命令。将第一条命令中的文件名替换为相关专项；CLI 或分发变更另加第三条检查。

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_execution.py' -v
PYTHONPATH=src python3.11 -m unittest discover -s tests
python3.11 scripts/check_distribution.py source
```

测试用临时目录、固定输入和本地模型替身。需要验证真实容器执行时，使用本机固定镜像 ID 设置 `CLAUSEWEFT_TEST_IMAGE`，在结果中注明 Docker 测试是否实际执行。修改用户可见行为时更新相应文档和可复现示例，交接时列出变更、验证结果和剩余限制。[贡献指南](CONTRIBUTING.md) · [安全报告指南](SECURITY.md)
