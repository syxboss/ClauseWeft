# project_tools · 项目资料、MCP 外部工具与分级审批

提供受服务端权限、数据范围控制和人工/分级确认约束的项目内与受控 MCP 工具调用能力，支持内置工具（`search_requirements`、`search_reference`、`create_requirement`）以及可信配置登记的外部 `mcp:<server>/<tool>` 工具。

## 模块划分

- `decisions.py`：定义内置工具与 `mcp:<server>/<tool>` 校验规则、风险分级（`read_only`、`mutating`、`external_effect`）、参数 Schema 必填项核对、单工具与并行多工具调用（`{"action": "tools", "calls": [...]}`，单轮最多 4 项），以及查重后新建的顺序约束。
- `approvals.py`：校验提议、运行版本与冻结的 `mcp_digests` / `reference_digest`，执行批准（含并行批量执行、写入回执与审计）、退回修改或拒绝；在 `tool_approval_policy="auto_read_only"` 下，仅当单次或整批调用全部为 `read_only` 且授权未变更时自动放行，任何 `mutating` 或 `external_effect` 调用均暂停等待人工确认。
- `mcp.py`：零第三方依赖的受控 JSON-RPC 2.0 MCP 客户端，支持本机子进程 `stdio` 与 Streamable `http` 两种传输协议，执行环境变量白名单清洗、`token_env` 注入、1 MiB 响应体积上限与敏感凭据脱敏。
- `references.py`：加载项目登记的本地 UTF-8 JSON 资料索引，使用标准库 SQLite **FTS5**（BM25 相关性排序 + 子串回退）执行检索，并在新任务规划时按文件 `mtime_ns` 自动热更新索引（运行中任务仍固定校验创建时的 `reference_digest`）。

回归入口：`PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_project_tools.py' -v` 与 `test_p1_agent_capabilities.py`。

关联设计：[任务执行与模型用量](../../../docs/11-任务执行与模型用量.md) · [工作台 API](../api/README.md)。
