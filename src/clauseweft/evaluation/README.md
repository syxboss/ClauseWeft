# evaluation · 路由策略评估

对应 **F08 / A11**。路由回放检查候选资格，配对评估比较两侧交付与成本。多步需求验收核对一次真实运行是否完成了预期的项目操作。

## 拥有的资料

保存带版本的脱敏题集、允许模型与验收条件、配对基线、匿名产物与判分对应、汇总口径。冻结任务输入、规则与模型池、预算条件及各项指标后再运行。实际调用尝试和费用引用 runs，测试结果引用 testing；汇总记录区分已执行、失败和缺测。

| Python 操作 | 输入与结果 | 主要失败 |
|---|---|---|
| 回放策略 | 已标注的任务、目录和观测快照、规则版本 → 每题决定、候选理由、硬约束违反数 | 快照不完整则题目无结果；不从回放推断实际质量、耗时或费用 |
| 发起配对试验 | 同题输入与上限、固定模型和动态路由两个方案 → 两侧运行引用、对盲评者隐藏方案的产物 | 端点不可用或预算不足，标记未完成；保留另一侧结果 |
| 汇总盲评 | 分组冻结后的运行明细、判分和费用 → 通过数/总数、每个成功任务的总成本、耗时中位数、重试与人工改派明细 | 成功数为零时该比率无定义；未知费用单列，不计为零 |

`replay(dataset)` 接受带 `version` 和 `items` 的题集。每题含 `id`、`acceptance`、`allowed_endpoints` 和路由 `snapshot`。通过 `POST /api/v1/evaluations` 传入 `mode: "paired"`、`fixed_endpoint` 和同一题集，可登记两个策略的运行。平台按可信目录冻结模型范围、版本和价格。盲评页接口为 `GET /api/v1/evaluations/{id}/blind`，向 `/grades` 提交匿名 ID 到布尔值的映射。试验详情包含全部题数、覆盖率、最终判定与待核费用。

**验收例**：按 A11 冻结四类任务；同一道题出现两次重试，统计成本纳入这两次实际收费。报表分开展示无外发违规的回放结果与配对交付结果，并公开已执行题数/总题数。24 题是试点建议值。

例如，一个配对题集有两道题，其中一道缺少 `snapshot.task.budget_micros`，另一道有预算且模型可用。有效题的两侧仍会运行，每侧的 `total` 为 2、`unexecuted` 为 1。已派发但尚未取得可判定结果的题计入 `pending_execution`；已有产物等待盲评的题计入 `awaiting_grade`。固定端点明确返回限流时，该侧记为 `failed`，未核实的费用另计入 `pending_cost_items`。运行 `PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_evaluation_api.py' -v` 可复验上述情况。

## 多步需求验收

给多步工具任务设置 `allowed_tools=["search_requirements","create_requirement"]`，并填写 `requirement_query`。向 `GET /api/v1/runs/{id}/goal-assessment` 提交预期场景、检索关键词和完整需求标题。例如预期系统在未命中后新建“退款对账”时，查询 `?scenario=created&query=退款&title=退款对账`。HTTP 客户端应对查询值进行 URL 编码，凭据须具备该运行所属项目的读取权限。

| 场景 | 通过条件 |
|---|---|
| `existing` | 已确认的检索回执命中指定标题，最终答复引用该需求 ID。项目中有且只有一条该标题的需求，运行仅执行过检索 |
| `created` | 确认检索后无命中，又批准新建指定需求。服务端回执记录本次确实创建；实时项目记录的 ID 和标题与回执一致，最终答复引用该 ID |
| `rejected` | 检索无命中，负责人拒绝对指定标题的新建提议。运行已取消，已执行回执仅有检索，项目中没有该标题的需求 |

返回的 `status` 在运行期间为 `pending`，终态为 `passed` 或 `failed`。`checks` 给出逐项布尔结果，`evidence` 给出审批节点及回执工件、最终答复工件、拒绝提议和当前项目中的需求 ID。相同运行可以再次验收；之后修改需求标题可改变实时检查结果。检索与新建之间由其他操作创建了同名需求时，本次新建回执为 `existing`，相应的 `created` 验收会失败。三个场景可用 `PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_project_tools.py' -v` 复验。

关联设计：[模块与依赖](../../../docs/04-概要设计.md) · [接口与事务](../../../docs/05-详细设计.md)。
