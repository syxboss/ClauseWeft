# project_tools · 项目资料、需求与审批工具

提供受服务端权限和人工确认约束的项目内工具调用能力，当前支持 `search_requirements`、`search_reference` 和 `create_requirement`。

## 模块划分

- `decisions.py`：定义工具白名单、参数与长度校验、单步/多步提示词与终止条件，以及查重后新建的顺序约束。
- `approvals.py`：校验提议与运行版本，按负责人决定执行批准（写入回执与审计）、退回修改或拒绝。
- `references.py`：在服务启动时按可信配置加载项目本地 UTF-8 JSON 资料索引，并执行关键词检索与数据范围控制。

回归入口：`PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_project_tools.py' -v`。

关联设计：[任务执行与模型用量](../../../docs/11-任务执行与模型用量.md) · [工作台 API](../api/README.md)。
