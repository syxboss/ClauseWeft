# harness · 技能、请求上下文与检查点

`Harness` 负责冻结实际模型请求的上下文证据，并在运行处于稳定状态时保存检查点及派生独立运行。

## 主要能力

- **请求上下文证据**：`contexts(db, run)` 读取当前运行及允许继承来源尝试的实际消息、技能修订、模型端点与限制，返回原始摘要 `context_sha256` 与经 `audit.sanitize` 脱敏后的展示视图。缺少历史消息的旧尝试明确标为 `legacy_missing`。
- **检查点与派生**：在无活跃派发、无待核对调用且交付作业已结束时保存检查点；派生运行保留来源与可复用模型产物，人工关卡与预算独立重新生效，并按项目归属校验继承来源。

回归入口：`PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_harness.py' -v`。

关联设计：[Harness 能力与审计](../../../docs/09-Harness能力与审计.md) · [概要设计](../../../docs/04-概要设计.md)。
