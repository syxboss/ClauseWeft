# routing · 内容路由

对应 **F02 / A01、A02、A03**。F08 的离线回放复用同一选择规则。这里将任务和配置快照变成可解释的决策，实际计费调用由 runs 调度。

## 当前能运行的部分

[preview.py](preview.py) 的 `preview(snapshot)` 接受含 `task` 和 `models` 的字典，返回决定与输入摘要。在项目 CLI 中使用 `route --input examples/route-demo.json` 可离线运行[示例](../../../examples/route-demo.json)。该示例按“仅本地”策略选中 `local-review`，更便宜的云端候选带有 `data_scope` 淘汰原因。状态包括 `selected`、`blocked` 和需要澄清意图的 `needs_input`；不合法的快照会产生输入错误。离线示例的价格、能力与观测值均来自输入文件。

## 派发与复核

路由模块拥有规则版本与决策内容。每条决策至少记录任务意图、候选与逐项淘汰理由、排序依据、目标数据去向，以及任务、策略和端点配置的快照引用。目录与账本仍由各自模块维护。

| 路由操作 | 输入与结果 | 主要失败 |
|---|---|---|
| 生成正式预览 | 任务摘要、附件属性及阶段、候选快照、硬约束、价格和观测快照 → 有序候选或澄清/阻塞原因 | 意图歧义、缺少必需能力、没有合法候选 |
| 为并行组分配端点 | 各分支候选、模型身份和所需独立模型数 → 每角色对应的端点及理由 | 同模型的别名不能填满两个独立席位，返回缺口 |
| 派发/回退前复核 | 原决策、端点启用状态、最新数据策略与可用预算、失败分类 → 合格的下一候选或暂停 | 仅本地禁止回退到云；固定模型失败等待改派；认证失败不重复尝试同一凭据 |

生产接口沿用 `preview(snapshot) -> decision` 的纯计算边界，控制层保存和复核快照。运行示例可在仓库根目录执行 `PYTHONPATH=src python3 -m clauseweft route --input examples/route-demo.json`。

**验收例**：输入“评估是否要改代码”时应识别为评审；相同规则及观测快照应返回相同决定。固定云端模型也不能绕过仅本地约束。并行派发前的预算预留最终由 runs 在事务内裁决。

关联设计：[模块与依赖](../../../docs/04-概要设计.md) · [接口与事务](../../../docs/05-详细设计.md)。

### 离线快照字段

| 字段 | 输入约定 |
|---|---|
| `task.text`、`stage` | 非空任务文本。阶段为 `general`、`auto`、`requirements`、`review`、`code` 或 `test`。平台普通单任务默认 general，CLI 快照未指定时仍使用 auto。自动模式下缺少匹配或命中多个意图会返回 `needs_input`。 |
| `task.data_scope`、`preference` | 分别取 `local_only/public` 和 `cost/quality/latency`；手动锁定时可再传 `pinned_endpoint`。 |
| `task.input_tokens`、`max_output_tokens`、`budget_micros`、`required_capabilities` | 前三项为非负整数，能力为字符串数组。快照中的价格和预算以同一币种计，1 微单位为该币种的百万分之一。 |
| `models[]` 身份、访问和容量 | `endpoint_id` 唯一；`model_identity` 标识底层模型；`location` 为 `local/cloud`；`enabled` 和 `allowed` 都要为真；`context_tokens` 为正整数，`capabilities` 为能力数组。 |
| `models[]` 适配和排序 | `fit` 将意图映射到 1–5 的适配度，缺少该意图即淘汰。`priority` 越小越优先。可填写 `latency_ms` 和 `quality_bp`（0–10000），相应观测不足时使用人工优先级。 |
| `models[]` 价格 | `input_micros_per_million`、`output_micros_per_million` 表示每百万 token 的整数微单位价格；缺失表示未知，在这个硬预算示例中淘汰。 |

运行上方示例时，报告中的 `data_scope` 为 `local_only`，选中的 `local-review` 在候选列表中的 `location` 为 `local`。将任务改为 `public` 后，较便宜的 `cloud-general` 会排在首位，`location` 为 `cloud`。报告同时列出规则版本、输入摘要、候选估价和淘汰原因。实际派发前，服务端会根据受信配置重新核对。
