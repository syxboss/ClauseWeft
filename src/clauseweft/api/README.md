# 个人工作台、HTTP 与管理界面

从仓库根目录启动服务并打开终端打印的地址。交互启动会生成本次本机访问令牌，后台运行从 `CLAUSEWEFT_CONSOLE_TOKEN` 读取。

```sh
PYTHONPATH=src python3.11 -m clauseweft serve --config examples/workbench.json --state ~/.local/share/clauseweft
```

默认监听 `http://127.0.0.1:8766/`。一个状态目录由一个控制进程使用，需要保存在受信磁盘、被测仓库之外。使用 `--workers 4` 设置工作线程数，`--port 0` 让操作系统分配端口。

首次选择「个人配对」并输入本机访问令牌。配对在同一事务中保存本机所有者、个人工作空间和「我的项目」。此后能保存个人偏好、选择已连接的执行器并从任务页查看进度。配对返回有效期为 12 小时、可撤销的具名管理员会话。浏览器按本站来源保存这份会话，退出时服务端撤销它。到期后用本机访问令牌重新配对即可取得同一身份。请在本人信任的浏览器中配对。

已有成员或业务记录的实例保留原归属。成员选择「已有账号」，管理员选择「管理员」。在同一台机器上另开个人实例时，为新进程单独指定 `--port` 和 `--state`。独立示例空间仍可用于了解页面和任务结构。[首次使用与授权](../../../docs/10-工作空间与权限.md)

## 默认使用路径

顶部品牌标志返回工作台，同栏可切换工作空间、创建任务和查看当前账号。个人导航提供工作台、项目、任务和外部 Agent 入口，工作流和实例设置收在二级导航中。已有团队账号沿用基于权限的导航。“协作”包含需求看板、SOP 模板、Agent 角色、技能库与审批中心；“资源与管理”按权限列出模型配置、模型用量、审计日志与团队设置。手机通过“导航”展开这些入口及项目、页面筛选控件。

管理员从“资源与管理 → 模型配置”接入模型并向工作空间授权后，操作者在项目目录创建项目，回到工作台填写目标并选择模型，点“开始执行”即可创建默认 single 任务。直接任务的默认阶段为 general。要指定角色或选择仓库交付，打开顶部“新建任务”表单。工作台输入区也可直接选择“并行分析”或“代码交付”，随后填写对应的预算或仓库配置。

普通任务可省略预算，服务端根据当前模型币种、价目和请求上限生成有限的单次执行额度，预览返回 budget 及 source。多种合格币种无法自动合并时，需选择模型或显式给出币种和预算。配置缺失时明确提示，不预置模型、价格或固定美元额度。[简化任务与实际用量](../../../docs/11-任务执行与模型用量.md)

从「外部 Agent」创建的任务使用独立的 API，权限、文件过滤、评审及回执见[个人工作台与外部 Agent](../../../docs/12-个人工作台与外部Agent.md)。

### 运行请求

以下字段用于 `POST /api/v1/routing/previews` 和 `POST /api/v1/runs`。两者都接收 `task.text`，由 `project_id` 确定工作空间，传入 `workspace_id` 时必须匹配。

可传 `task.materials` 引用文本材料，数组每项含 `name` 和 `text`。最多 4 份，正文合计最多 64 KiB，以 UTF-8 字节计。模型请求、路由估价及请求证据均使用实际材料内容。

| 任务方式 | 请求字段 | 可观察的结果 |
|---|---|---|
| 普通任务 | 可省略 `mode`。选角色时在顶层传 `role_id`，附加技能传 `skill_ids`。角色的阶段和已绑定端点参与路由，同时指定相冲突的模型会被拒绝 | 运行包含 `task` 节点。选角色后可读取 `role_id`、`role_name`、`role_revision` 和实际请求上下文 |
| 复杂任务 | `mode=complex`，必填总 `budget`。可用 `task.pinned_endpoint` 固定全图模型，`skill_ids` 应用于规划、分支和汇总，`max_concurrency` 控制并发 | 预览返回 `preview_node=plan`。有效规划回执生成 `branch-1` 至 `branch-N` 及 `summary`，可按节点读取成果 |
| 内置仓库交付 | `mode=workflow`，提供 `repo_id`、`base_sha`、`suite_id`，可选 `max_fix_rounds`（0–3 轮测试失败自动返工） | 取得固定的需求基线、评审、补丁、实测和接纳节点；启用 `max_fix_rounds` 时测试失败自动带日志返工 `code` 节点 |
| 团队 SOP | 传已发布的 `template_id`，服务端取得该版本的执行方式、角色及依赖 | 模型节点由 worker 调用，人工关卡等待操作者决定 |
| 项目工具 | 传 `mode=tool`、`tool_id` 和显式总 `budget`，可选 `tool_approval_policy`（`always` / `auto_read_only`） | 模型拟参、人工确认（或只读自动执行）并记录回执、模型读取回执作答 |
| 多步项目工具 | 传 `mode=tool`、`allowed_tools` 和总 `budget`，可设 `max_tool_steps`、`requirement_query`、`tool_approval_policy` | 每轮 `decide-N` 读取目标与之前全部回执；`auto_read_only` 下只读检索自动执行，写入操作继续人工审批；支持 `GET runs/{id}/stream` SSE 实时事件流 |

复杂任务默认阶段为 `general`。模型规划须是仅含 `subtasks` 的 JSON 对象，值为 2 至 8 个字符串，每项去除首尾空白后长 1 至 4,000 字符。模板、仓库交付字段及逐节点 `pinned_endpoints` 用于其他任务方式。计划无效时 `plan` 进入 `needs_input`，原答复留在尝试记录，原因为 `invalid_complex_plan`。通过 `POST runs/{id}/resume` 可重试。有效分工已经生成后，如需更换整套计划则创建新任务。[可复现的创建及读取示例](../../../docs/11-任务执行与模型用量.md)

### 配置资料源与确认工具

`search_requirements` 检索本项目需求。`create_requirement` 根据已确认的标题、说明和优先级，在本项目创建待办需求。`search_reference` 检索管理员为项目登记的本地资料索引。检索按关键词匹配标题与正文，最多返回 8 条带 ID 和原文摘录的结果。

要接入资料索引，在启动配置中加入如下字段，将 `default` 换成目标项目 ID。资料文件为 UTF-8 JSON 数组，例如 `[{"id":"refund","title":"退款核对","text":"退款时核对订单编号。"}]`，每个文件最大 1 MiB，最多 2000 条。只有获授权的项目成员可通过工具使用这些资料。`data_scope` 默认为 `local_only`。服务启动时加载文件，更新后重启服务，新任务可使用新版本。

```json
{
  "project_references": {
    "default": {
      "name": "客服操作手册",
      "path": "/srv/clauseweft/reference/support.json",
      "data_scope": "local_only"
    }
  }
}
```

例如，提交 `{"mode":"tool","tool_id":"create_requirement","project_id":"default","task":{"text":"登记一条退款对账需求，验收时核对订单编号。"},"budget":{"currency":"CNY","limit_micros":100000}}`。币种与额度填写实际获授权模型的值。等待运行变为 `needs_input` 后，读取 `GET runs/{id}` 的 `tool_proposal`，核对参数。向 `POST runs/{id}/tool-decisions` 提交 `action`、`proposal_ref`、`reason`，携带运行当前 `If-Match` 及同一操作的幂等键。`approve` 执行当前提议并保存回执，`revise` 根据说明重新拟参，`reject` 结束运行。错误格式的参数支持退回修改。确认写入、回执、审计事件和幂等结果在同一事务提交。

### 多步调用与查重

单工具任务的最后一轮应交回答复。如果模型交回了另一次工具请求，运行会以 `additional_tool_requested` 等待处理。操作者可以重试答复，或另建多步任务并明确授权所需工具。

多步工具示例使用实际项目 ID，将下例提交到 `POST /api/v1/runs`。预算币种应与所用模型一致。

```json
{
  "mode": "tool",
  "project_id": "<项目 ID>",
  "task": {"text": "先查退款对账需求，缺失时申请新建，验收时核对订单编号。"},
  "allowed_tools": ["search_requirements", "create_requirement"],
  "requirement_query": "退款对账",
  "max_tool_steps": 2,
  "budget": {"currency": "CNY", "limit_micros": 100000}
}
```

每步审批沿用 `tool-decisions` 接口。使用当轮 `tool_proposal.proposal_ref`、当前运行版本以及本操作的幂等键。重复提交同一批准只执行一次。指定查重关键词后，服务端要求先检索；有匹配时最终答复须引用已有需求 ID。无匹配时须申请新建，再引用真实回执的 ID。若其他成员在审批前已登记匹配需求，回执标记 `disposition=existing` 并指向该记录。

省略 `requirement_query` 时，模型可根据目标在授权工具内自主选择。执行次数默认 4，可设 1 至 8。达到次数上限后仍可依据现有回执作答。非法决策进入 `needs_input`，重试会把校验反馈交给模型。同一运行的每次模型调用都计入总预算。可从 `runs/{id}/goal-assessment` 核对查重场景的业务结果。[验收字段与通过条件](../evaluation/README.md#多步需求验收)

## 可选需求与 SOP

项目属于具体工作空间，组织需求、运行、审批和成本记录，可绑定管理员向该空间分配的可信仓库。已有状态中的项目与资源归入默认团队空间，缺少项目归属的历史运行按需归入“默认项目”，原工件、候选 SHA、账本和决议保持可查询。项目归档后不能启动新运行。

需求中心保存标题、说明、优先级、负责人和状态。创建运行时传 `project_id` 和可选的 `requirement_id`，服务端检查所属项目并保存关联。需求创建后保留原项目归属，跨项目移动会返回 409。需求详情返回实际关联的运行数量、最近运行 ID 和状态。需求看板的业务状态由成员管理，执行结果从关联运行读取。

角色配置包含名称、阶段、提示和可选固定端点。SOP 模板保存节点、依赖、角色和人工审批节点，保存与发布是两个动作。发布时校验依赖图并冻结角色指令、端点和角色版本。启动时通过 `template_id` 选择最新已发布版本，正在执行的运行保留创建时的快照。更改角色后，需要保存模板的新版本再发布。

通用 SOP 的 `execution_mode` 为 `sop`，支持 `model` 与 `gate` 节点。节点字段为 `node_id`、`label`、`kind`、`stage`、`dependencies` 和可选 `role_id`、`group`。同组的独立模型节点读取相同上游，派发仍受模型身份、数据范围和费用约束。标准研发交付模板使用 `execution_mode=workflow`，审批、补丁整合和实测的必需依赖由服务端保留。

总览页的数据来自持久运行、节点、端点和账本，待审批列表直接对应 `needs_input` 状态的人工节点。项目筛选会作用于运行数量、费用、审批和近期运行。

## 账号和权限

| 身份 | 服务端权限 |
|---|---|
| 管理员 `admin` | 全实例管理成员、授权、工作空间、项目、业务资源、模型端点、可信配置、评估和完整审计 |
| 操作者 `operator` | 在显式获授权空间和项目内管理业务内容、角色、技能和 SOP，启动运行、暂停恢复、审批、导出及查看成本 |
| 只读成员 `viewer` | 读取显式获授权项目、需求、运行进度与执行证据，不允许业务写入和实例管理 |

成员密码至少 12 个字符，以独立随机盐和 scrypt 哈希保存。账号登录返回有效期 12 小时的随机 Bearer 会话，库内只保存令牌摘要。退出、禁用账号或轮换密码会撤销对应会话。变更账号角色会在后续请求认证时立即生效。连续 8 次错误密码会使该用户名暂停登录 5 分钟。

管理令牌对应 `bootstrap-admin`，适合首次配置和账号恢复。其操作和具名账号操作均写入平台审计记录。该令牌由进程环境管理，撤销时需轮换环境变量并重启服务。账号登录接口不将会话令牌写入幂等结果表。

`GET auth/me` 返回当前角色、`capabilities` 能力列表及 `workspace_ids,project_ids` 允许列表。非管理员项目访问同时要求空间与项目授权，新账号及从未保存显式授权的旧版迁移而来的非管理员默认无业务授权，后续升级保留已有允许列表。管理员通过成员表单或 API 显式分配；操作者在获授权空间创建项目后取得新项目访问权。空间授权不会开放其中全部项目。

列表和总览先按授权过滤，再分页和聚合。所有运行子资源、工件、上下文、历史、派生和写入沿所属项目检查。直接链接、旧幂等缓存和冻结快照不能绕过当前权限。完整账号、模型端点、可信配置、评估、实例审计及审计导出只供管理员。不同工作空间的角色、技能和模板不能交叉引用，运行只能使用管理员向本空间分配的仓库和模型端点。

当前访问模型面向同一组织中的受信实例，账号角色尚不支持在不同工作空间分别覆盖。组织间基础设施隔离、SSO 和逐动作自定义授权需要独立实现与验收。服务默认绑定本机回环地址，远程访问可通过受控反向代理进行，代理转发的 Host 应匹配后端回环地址和端口。

## 管理员自配模型

1. 按[网关说明](../gateway/README.md)启动独立服务，从 `GET /v1/models` 的响应头取得当前 `ETag`。如果网关启用入站认证，在可信配置的 `gateways.local` 中加入 `token_env`，填保存访问令牌的环境变量名称。
2. 在“模型配置”填写网关 ID、实际别名、带双引号的 ETag、数据去向、币种及输入/输出单价。界面以对应币种的金额/百万 token 输入，最多六位小数，服务端 API 使用整数微单位；免费也需明确填零。上下文、能力、适配等级和身份核验按需展开，底层身份核实后才能设置 `identity_verified`。
3. “探测连接”检查网关目录、别名和版本。“验证 JSON 输出”要求输入预算，派发固定端点的一次推理，可在“探测记录”查看与目录版本绑定的验证结果。
4. 管理员在模型表单选择“用于当前工作空间”，或在团队设置把登记模型加入工作空间的 endpoint_ids。操作者创建项目，在“任务”填写内容并选择获授权模型，即可提交普通任务。输出上限、数据范围、意图、预算、预览与协作流程按需在高级设置中调整。已完成的单任务先显示结果；高级流程按步骤列出产物和必要关卡。
5. “模型用量”按实际尝试、端点版本及币种读取统计，价格变更不重算历史，缺少合法 token 或外部结果未知时显示待核对。普通入口只处理文本，仓库交付中的文件由服务端按获准 Git 提交筛选。供应商地址与凭据只配置在网关。

## 可选代码交付的可信配置

在 `examples/workbench.json` 的副本中设置如下两项。路径换成本机仓库的绝对路径，镜像换成实际已安装、含 `/usr/local/bin/python` 的镜像 ID。可用 `docker image inspect --format '{{.Id}}' python:3.11-slim` 查看现有镜像 ID。此例约定程序从标准输入读取角色，向标准输出写出结果。必测 ID 和断言由负责人维护。

```json
{
  "repositories": {
    "demo": {
      "path": "/absolute/path/to/repo",
      "allowlist": ["app.py", "tests"],
      "paths": {"code": ["app.py"], "test-design": ["tests"]},
      "data_scope": "local_only",
      "suite_id": "csv",
      "suite_ids": ["csv"]
    }
  },
  "suites": {
    "csv": {
      "image": "sha256:<64位本机镜像ID>",
      "timeout_seconds": 20,
      "commands": {"export": ["python", "app.py"]},
      "cases": [
        {"id": "deny-member", "clause_id": "CSV-01", "command_id": "export",
         "stdin": "member\n", "stdout": "denied\n", "required_files": ["app.py"]}
      ]
    }
  }
}
```

在顶部“新建任务”的“执行方式”中选择“代码变更与验证（内置研发流程）”，填写仓库、基准提交、套件和总预算。API 使用 mode=workflow，未指定 template_id 时使用内置交付图。完整流程的两路需求评审、编码与测试设计组都要求可分配到不同的已核实模型。需求页可按冻结套件中的条款 ID 修订草稿，修订会触发重新评审。负责人关闭阻断意见并批准基线后，worker 派发编码和测试设计。在评审页选择工作包，提交整合并执行必测项。运行页提供测试报告及失败原因。需要修改实现时可退回工作包，确认新候选的证据后接纳并导出 JSON 交付清单。

交付清单包含基准和候选 SHA、补丁、需求、评审、实测结果和条款映射。`manifest.candidate.patch` 可以取出为标准补丁，在干净的同一基准上使用 `git apply --index` 重放，并用 `git write-tree` 对照 `manifest.candidate.tree_hash`。测试进程通过标准输入/输出与执行器交互，预期输出留在宿主的可信执行器内。当前适配器支持这种黑盒命令，测试环境需要固定的本机 Docker 镜像。

## HTTP 接入

所有应用接口位于 `/api/v1/`。客户端携带 `Authorization: Bearer <账号会话或管理令牌>`。除登录和只读路由预览外，每次写操作携带 `Idempotency-Key`。修改已有资源携带 `If-Match: "<所读 revision>"`。响应丢失时使用原键、原正文和原版本重发。409 表示幂等冲突或业务门槛未满足，412 要求重新读取，428 表示缺少前置版本。列表使用 `limit`、`cursor`、`next_cursor`，事件使用 `after`。

| 操作 | 入口及请求要点 |
|---|---|
| 配对、登录与退出 | `POST auth/pair` 仅接受本机管理令牌与空 JSON 对象，返回 `token,user,expires_at`。已配对身份在 `user.personal_owner` 标明。已有团队实例拒绝初次配对。 `POST auth/login` 接受 `username,password`，返回 `token,user,expires_at`。`GET auth/me` 返回当前账号。`POST auth/logout` 撤销当前账号会话 |
| 模型用量 | `GET usage` 接受 `workspace_id` 和可选 `project_id`，仅管理员与操作者。返回按模型版本的 `models` 及分币种 `totals`，含实际调用、token、价目、已结算、预留与待核对数量 |
| 平台总览 | `GET overview` 按 `workspace_id,project_id` 返回获授权范围的项目、运行、按币种分组实耗及预留、待审批节点和近期运行。管理计数与近期审计仅管理员 |
| 工作空间 | `GET/POST workspaces`，`GET/PATCH workspaces/{id}`。读取按授权过滤，维护仅管理员。字段包括名称、说明及允许的 `repository_ids,endpoint_ids` |
| 项目 | `GET/POST projects`，`GET/PATCH projects/{id}`。字段为 `name,description,repository_id,status,color`。`status` 为 `active/archived` |
| 需求中心 | `GET/POST requirements`，`GET/PATCH requirements/{id}`。字段为 `title,description,project_id,status,priority,assignee`。状态为 `backlog/ready/in_progress/review/done`，优先级为 `low/medium/high` |
| 角色 | `GET/POST roles`，`GET/PATCH roles/{id}`。字段包括 `name,description,stage,prompt,endpoint_id,skill_ids`，空端点表示可在运行时选模 |
| SOP 模板 | `GET/POST templates`，`GET/PATCH templates/{id}`，`POST templates/{id}/publish`。字段为 `name,description,execution_mode,nodes`。发布需模板当前 `If-Match`。`GET templates/{id}/versions` 返回已发布版本、发布时间、发布者、节点数和冻结节点快照 |
| 成员 | `GET/POST members`，`PATCH members/{id}`。创建接受 `username,display_name,role,password,workspace_ids,project_ids`，修改接受 `display_name,role,active,password,workspace_ids,project_ids`。所有读取与写入需要管理员 |
| 审计和配置 | `GET audit` 返回操作者、对象、动作、时间和依据。`GET settings` 返回工作区、仓库授权范围、套件 ID 和网关 ID |
| 模型与路由 | `GET/POST endpoints`，`PATCH endpoints/{id}`，`POST endpoints/{id}/probes`，`POST routing/previews`。连接探测提交空对象。JSON 验证提交 `kind=structured_output`、项目和对应币种的预算，返回可追踪费用的运行 |
| 启动与检查 | `POST runs` 接受上表的运行请求，可另传 `title,requirement_id`。`GET runs` 可按 `project_id,requirement_id,status` 筛选。可选的 `q` 在获授权项目的任务标题、原始目标中按字面匹配，先筛选再分页。例如 `GET runs?workspace_id=default&q=迁移&limit=30`，下一页沿用查询并传入 `cursor=next_cursor`。网页“任务”使用同一搜索，支持前后翻页。附件、个人偏好和模型答复不参与匹配。`GET runs/{id}`、`.../attempts`、`.../events`、`.../artifacts/{id}` 返回同一运行的证据 |
| 基线与评审 | `POST runs/{id}/specs` 传 `parent,clauses,reason`。`POST .../findings/{fid}/resolutions` 传 `action,reason,evidence,related_ids`。`POST .../approvals` 的 `kind=baseline` 绑定 `version`，`kind=delivery` 还绑定 `candidate_sha` |
| 整合与返工 | `POST .../integrations` 传有序的 `selected` 和 `reason`，冲突可再传人工选定的 `resolution_patch`。`POST .../tests` 指定候选 SHA，可切换登记的套件。`POST .../rework` 传 `node_id,reason`，仅退回选中的代码或测试设计工作包及下游 |
| 运行控制、导出 | `POST .../cancel`、`.../resume`、`.../reconciliations`、`.../exports`；对账传 `attempt_id,actual_micros,evidence`。终态取消后需另建运行 |
| 暂停与通用 SOP 审批 | `POST runs/{id}/pause` 暂停后续派发，`.../resume` 继续。`POST runs/{id}/gates/{node_id}` 接受 `action=approve/reject,reason`，批准节点或将其上游退回重做。研发交付流程使用专用基线审批、整合、测试和接纳接口 |
| 路由评估 | `POST evaluations` 接受 `mode=replay/paired`、`dataset={version,items}`。每题含 `id,acceptance,allowed_endpoints,snapshot`。配对运行另传 `project_id`、`fixed_endpoint` 和预算币种 `currency`，服务端按项目工作空间的模型、题目预算和数据策略启动两侧。通过 `GET evaluations/{id}/blind` 取匿名结果，向 `POST .../grades` 提交匿名 ID 到布尔判分的映射，`GET evaluations/{id}` 查看成本、覆盖率和通过数 |
| 多步需求目标验收 | `GET runs/{id}/goal-assessment` 接受 `scenario`、预期检索词 `query` 与需求完整标题 `title`，读取该运行的工具回执、拒绝决定、最终答案及项目需求。支持 `existing`、`created` 和 `rejected`。[验收条件与例子](../evaluation/README.md#多步需求验收) |

管理员在“模型配置”中点击“连接探测”，再点击“验证结构化输出”，选择当前工作空间中的进行中项目并填写验证费用上限。项目工作空间需要已获授权且启用的模型。通过 API 调用时，先从 `GET projects?workspace_id=<空间ID>` 获取实际项目 ID，然后向 `POST endpoints/{id}/probes` 提交如下请求体，携带目录当前版本的 `If-Match` 及幂等键。将示例 ID 和币种替换为实际配置。

```json
{"kind":"structured_output","project_id":"项目ID","workspace_id":"空间ID","budget":{"currency":"CNY","limit_micros":5000}}
```

JSON 验证和配对评估均可仅指定 `project_id`；同时传入的 `workspace_id` 必须与项目一致。兼容旧客户端时，省略项目会在指定空间（缺省为默认工作空间）选用仍处于进行中的旧 `default` 项目，或该空间唯一进行中的项目。没有可选项目或存在多个候选时，需显式指定项目。离线路由回放无需项目。

[端到端测试](../../../tests/test_workbench.py)从 HTTP 入口启动任务，经过独立网关和临时 Git 仓库，在固定 Docker 镜像实测后接纳并导出。运行方法见[构建与运行](../../../docs/06-构建与运行.md)。

## 技能、上下文与检查点

### 个人长期偏好

具名成员在账号菜单打开“我的偏好”，可按当前工作空间保存最多 4,000 字的写作习惯、语言或输出格式。新任务默认不带入偏好。提交单任务或追问时勾选“本次使用我的偏好”，服务端会把当前版本作为一条用户消息发给实际模型，并保存到项目成员可读的运行上下文。修改或清空偏好只影响以后的选择。共享管理员令牌不代表具名成员，个人偏好需要使用账号登录。

例如，先在“我的偏好”写下“默认用简体中文”。完成直接任务或并行分析后，在最终答复旁点“记住要点”，核对当前空间与已有偏好，在空白输入框写“发布评审先列阻断项”，再点“追加到我的偏好”。重新打开账号菜单可看到这两行。若另一个标签页同时修改了已有偏好，表单会展示最新内容并保留待追加的要点，供你重新确认。

读取 `GET me/preferences/{workspace_id}` 获得自己的 `instructions` 和 `revision`。使用 `PATCH`、幂等键和当前 `If-Match` 更新；首次写入的版本为 `0`。提交 `{"instructions":""}` 可清空。账号由会话确定，每次都检查当前空间授权。写入回执只含新版本号，实例审计记录该动作，不保存偏好正文。创建或预览单任务时，显式提交顶层 `"use_preferences": true`，模型输入估价包含这条消息。

### 团队技能和执行检查点

技能库保存 `name,description,instructions,data_scope,enabled`。角色通过 `skill_ids` 选择技能，发布 SOP 时冻结指令与技能修订。单任务也可显式传 `skill_ids`。运行中不会因编辑技能正文而替换已经冻结的指令。禁用技能会阻止使用它的新派发，收紧当前技能数据范围也会收紧运行路由。技能正文作为文本指令进入模型消息，当前支持范围见 [Harness 能力与审计](../../../docs/09-Harness能力与审计.md)。

每次派发在记录预算和派发意向时同步保存实际 `messages`、节点定义、上游引用、模型与配置版本。运行详情的“请求上下文”提供原始工件的 `context_sha256` 及脱敏展示的 `view_sha256`。旧调用没有保存实际消息时明确显示 `legacy_missing`。

| 操作 | API |
|---|---|
| 技能维护和历史 | `GET/POST skills`、`GET/PATCH skills/{id}`、`GET skills/{id}/history` |
| 资源历史 | `GET projects/{id}/history`，需求、角色和模板使用相同路径格式 |
| 请求证据 | `GET runs/{id}/context`，返回实际或继承尝试的消息、来源、技能修订和摘要 |
| 检查点 | `GET/POST runs/{id}/checkpoints`。保存传 `label,reason` 和运行的 `If-Match` |
| 派生运行 | `POST runs/{id}/forks`，传 `checkpoint_id,title,reason,budget` 和当前运行版本 |

保存检查点前，运行不能有准备派发、执行中或结果未知的节点，整合与测试作业也须结束。复杂任务可以保存稳定检查点，需要重新规划时创建新任务。派生支持文本单任务和通用 SOP，成功且没有人工关卡祖先的模型结果可复用。人工关卡重新审批，下游重新执行，费用由新预算管理。仓库交付继续使用专用需求、候选和实测流程。

资源列表、总览和运行列表支持 `workspace_id`，项目类查询可继续传 `project_id`。创建项目、角色、技能、模板和运行时显式提供业务 `workspace_id`，需求从其项目继承。该字段不同于部署配置中的实例标识。

## 审计查询、导出与离线核验

以下审计查询、校验与导出均要求管理员。`GET audit` 支持 `actor_id,action,resource,outcome,project_id,run_id,correlation_id` 精确筛选，以及正常分页。详情保留脱敏前后状态、版本、证据引用、前驱和当前摘要。`GET audit/verify` 核验链、资源历史、当前资源、发布快照及交付作业结果。成功写入与业务证据共同提交，失败或拒绝另记访问事实，读取、登录、导出和后台运行也有记录。

`GET audit/exports` 返回完整证明链、固定链头、筛选命中条目和脱敏证据。筛选标记关注范围，完整导出仍包含实例链与跨工作空间证据，因此只供实例管理员读取及保管。链内 `audit.export_prepared` 将证据、作业和业务记录的分区摘要绑定到 `manifest_seq`。浏览器直接保存原始响应，保留 JSON 数字表示。

下载后可在独立环境运行以下命令，核验链、附带内容摘要和证据视图。退出码 0 为通过，1 为不一致或证据缺失，2 为无法读取或格式无效。独立保管的原导出和链头用于判断数据库整体重写或回滚。

```sh
PYTHONPATH=src python3.11 -m clauseweft audit --input audit-export.json
```

[团队平台回归](../../../tests/test_platform.py)覆盖账号会话和服务端只读权限、成员禁用、旧状态迁移、项目归属检查、模板角色快照冻结，以及通用 SOP 实际派发到人工审批的完整过程。

## 继续追问与重新生成

读取 `GET runs/{id}` 的 `continuation_actions` 和当前 `revision`。向 `POST runs/{id}/continuations` 提交原版本的 `If-Match` 与独立幂等键。成功返回 201 和新的单任务；同一键、版本与正文重发可取得同一个任务。

| 请求正文 | 效果 |
|---|---|
| `{"action":"followup","text":"请解释第二点"}` | 在已完成的单任务、并行汇总或项目工具的最终答复后追问，可另传 `materials`、`budget` 和 `use_preferences` |
| `{"action":"regenerate"}` | 使用原问题、原材料和此前问答重新生成一次直接回答。可另传 `budget`、`use_preferences` 和 `max_output_tokens`（1–1,000,000）。已取消或需处理的单任务也可重新生成，涉及待核对调用时须先对账 |

新任务属于同一项目，保留来源、冻结的执行角色和技能。源任务关联的需求也会关联到新任务，需求状态与版本由原来的业务操作维护。每次调用都重新检查当前授权、模型及预算，历史调用曾受仅本地限制时新任务继续仅本地处理。默认按照包含对话的完整输入估算新额度，并受来源任务的显式上限约束。可以显式提供新预算。个人偏好由当前操作者逐次勾选。

工具任务完成后提供 `followup`。新任务执行单次文本回答。服务端从原任务读取最终答复和已批准工具回执，将回执中的结果一并发给模型，并在对话轮次的 `receipt_refs` 保存原工件引用。涉及项目资料源的追问在派发时重新验证资料版本，继续执行该来源的数据范围策略。

详情中的 `conversation` 给出来源 ID、版本、已保留的轮次与答复工件引用。历史按最近问答保留至多 8 轮、24 KiB 正文。最新一轮本身超过上限时使用带标记的首尾节选，较早轮次超出剩余额度时按整轮省略。完整输入和答复仍可从来源任务查看。
