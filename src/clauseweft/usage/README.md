# usage · 模型用量与费用汇总

按实际派发时的端点版本价目与供应商回执 token 统计模型用量和费用，不以估算值填补缺失或非法回执。

## 核心规则

- **价目冻结**：`pricing_snapshot(model)` 在派发前固定 `endpoint_id`、`endpoint_revision`、`model_identity`、`currency` 及输入/输出微单位单价，后续改价不重算历史尝试。
- **回执归一化**：`normalized_usage(value)` 明确区分 `reported`、`missing` 与 `invalid`，失败尝试、重试和待核对预留均保留在统计口径中，不同币种分列汇总。
- **权限边界**：统计仅聚合当前操作者获授权工作空间及项目内的实际尝试。

回归入口：`PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_usage.py' -v`。

关联设计：[任务执行与模型用量](../../../docs/11-任务执行与模型用量.md) · [概要设计](../../../docs/04-概要设计.md)。
