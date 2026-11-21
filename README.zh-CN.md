# ClauseWeft · 个人 AI 研发工作台

**本机优先的个人 AI 研发工作台，支持多模型任务编排、容器隔离外部 Agent 与可验证代码交付。**

[English](README.md) | 简体中文

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](#第一次使用)
[![零第三方运行时依赖](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](#第一次使用)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ClauseWeft 在本机保存你的项目、任务、执行记录、模型用量账本和候选补丁。核心运行时仅依赖 **Python 3.11+ 标准库**，采用单实例 SQLite 持久化与独立模型网关。你可以让模型直接回答问题，让多个分支并行分析材料并汇总结论，执行经人工审批的项目工具，也可以把 Git 仓库交给容器隔离的外部 Agent（**Claude Code** / **DeepSeek Harness**）修改与只读评审，并逐项核查进度、差异与回执。

![ClauseWeft 个人工作台实机运行截图 — 并行任务运行轨迹](docs/images/workbench-live.webp)

*ClauseWeft 0.0.1 个人工作台实机运行界面：展示任务提交、并行分析实时运行轨迹（`拆分任务 → 子任务 1 & 子任务 2 执行中 → 结果汇总`）、最近任务与外部 Agent 记录。另可查看[浅色外观](docs/images/workbench-light.webp)与[手机端布局](docs/images/workbench-mobile.webp)。*

---

## 选择任务方式

| 要完成的工作 | 提交方式 | 说明 |
|---|---|---|
| 请模型直接读材料、回答问题或写草稿 | 默认 `single` | 可附带本地 UTF-8 材料、个人偏好，或选用工作空间中的角色与技能；支持继续追问和重新生成 |
| 把一个目标从多个角度并行分析，再汇总结论 | 选择 `complex` 并设总预算 | 模型先写分工，worker 并发执行各分支，全部分支成功后自动汇总 |
| 检索项目需求、查知识库、查重后申请新建 | 选择「项目工具」（`mode=tool`） | 设置允许的工具、执行次数与总预算；每步审批后由模型依据真实回执决定下一步 |
| 修改登记仓库并验证交付 | 选择 `workflow` | 提交基准 SHA、验收套件与预算；内置需求基线审批、独立评审、受限补丁整合、固定镜像实测与人工接纳 |
| 让 Claude Code 改代码，或请 DeepSeek Harness 评审候选 | 在「外部 Agent」选项目、仓库和执行器 | 容器隔离执行，支持取消运行、查看进度、下载 Git 补丁，并对冻结版本单独发起只读评审 |
| 重复执行已保存的标准流程 | 选择已发布的 SOP 模板 | 服务端读取该版本的冻结角色、节点、依赖与人工关卡 |

### 外部 Agent 实机执行与候选评审

![ClauseWeft 外部 Agent 实机截图 — Claude Code 补丁与 DeepSeek 评审入口](docs/images/external-agent.webp)

*外部 Agent 实机运行详情：记录完整状态流转、执行结果、可下载的 Git 补丁，以及一键使用 DeepSeek Harness 评审该固定候选的入口。*

任务分类与“拆分、执行、汇总”的次序参考 SuperAgent。ClauseWeft 将分工保存在同一运行的依赖图里，每个模型节点经同一套路由、租约、预算和网关回执执行。管理员可以追溯实际请求、模型版本、产物和人工决定。[源码对照](docs/03-竞品调研.md) · [提交示例](docs/11-任务执行与模型用量.md)

工作台支持引用本地 UTF-8 文件；完成的直接答复、并行汇总和工具任务均支持继续追问，并可在「任务」中按标题或原始目标检索。具名成员可在账号菜单保存语言、格式等个人偏好，按次选择是否发给模型。[任务、材料与追问](docs/11-任务执行与模型用量.md) · [个人偏好与项目工具接口](src/clauseweft/api/README.md)

---

## 第一次使用

### 从源码启动

使用 **Python 3.11 或更高版本**，在仓库根目录执行：

```sh
PYTHONPATH=src python3.11 -m clauseweft --version
PYTHONPATH=src python3.11 -m clauseweft doctor
PYTHONPATH=src python3.11 -m clauseweft serve --config examples/workbench.json --state ~/.local/share/clauseweft
```

1. 打开终端显示的回环地址（默认 `http://127.0.0.1:8766/`），在「个人配对」输入启动时显示的本机访问令牌（后台进程从 `CLAUSEWEFT_CONSOLE_TOKEN` 读取）。已有团队账号可从「已有账号」入口登录。
2. 管理员在[独立网关](src/clauseweft/gateway/README.md)中配置自己的模型地址、名称及凭据环境变量，通过「设置 → 模型配置」登记端点、币种和输入/输出单价并授权给工作空间。平台不预置可调用模型或虚构价目。
3. 普通问答从工作台直接提交。外部编码和评审从「外部 Agent」进入，需先按 `examples/agent-image` 的锁定依赖构建镜像，并配置执行器与允许使用的仓库。[外部 Agent 配置与操作](docs/12-个人工作台与外部Agent.md)

### 安装到独立虚拟环境

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install .
clauseweft --version
clauseweft doctor
clauseweft serve --config examples/workbench.json --state ~/.local/share/clauseweft
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。请将 `--state` 放在受信磁盘且位于被测 Git 仓库之外。[账号与数据归属](docs/10-工作空间与权限.md)

### 可复现的离线选模示例

无需启动服务或配置模型密钥，直接在本机验证路由规则：

```sh
PYTHONPATH=src python3.11 -m clauseweft route --input examples/route-demo.json
```

[示例输入](examples/route-demo.json)要求数据仅在本地处理（`data_scope: "local_only"`），因此会选中 `local-review`；将副本的 `task.data_scope` 改为 `public` 后，较低价的云端候选才会参与排序。

---

## 用量口径与交付范围

- **真实用量与计价**：模型用量按实际尝试与回执计算，价格固定到派发时的端点版本；不同币种分列，失败与重试计入记录，缺少用量或外部结果未知时明确显示待核对，并支持具名对账记录。
- **单实例与权限边界**：当前版本（**0.0.1**）采用单实例服务和 SQLite 持久化。个人所有者使用自己的项目；共享部署中，`admin`、`operator`、`viewer` 经由工作空间与项目白名单在菜单、直接链接与 API 上统一受控。
- **技能、检查点与离线审计**：受版本管理的技能作为文本指令参与上下文；稳定状态支持保存检查点并派生独立运行（预算与人工关卡独立）。审计链支持原始响应导出与独立离线核验（`clauseweft audit --input FILE`）。
- **升级方式**：停止原控制进程，将新版本进程的 `--state` 指向原状态目录即可保留已有运行、需求版本、候选 SHA 和测试历史。
- **后续交付范围**：SSO、每项目独立角色及逐动作自定义授权、互不信任租户隔离、分布式 worker、通用第三方执行器、MCP 与代码托管自动提交属于后续规划。[需求与验收](docs/02-需求评审.md) · [验证记录](docs/06-构建与运行.md)

---

## 本地测试与核验

```sh
# 单元测试与本机集成回归
PYTHONPATH=src python3.11 -m unittest discover -s tests

# 启用本机固定 Docker 镜像的容器实测
CLAUSEWEFT_TEST_IMAGE="$(docker image inspect --format '{{.Id}}' python:3.11-slim)" \
  PYTHONPATH=src python3.11 -m unittest discover -s tests -v

# 分发入口与发布安全筛查
python3.11 scripts/check_distribution.py source
python3.11 scripts/check_release.py --checkout
```

完整 CI 矩阵（含锁定 Agent 镜像测试、Playwright 浏览器端到端验证及 npm 依赖审计）见 [.github/workflows/ci.yml](.github/workflows/ci.yml)。

---

## 开发文档

| 主题 | 文档 |
|---|---|
| 产品与使用 | [English README](README.md) · [任务与模型用量](docs/11-任务执行与模型用量.md) · [个人工作台与外部 Agent](docs/12-个人工作台与外部Agent.md) · [工作空间与权限](docs/10-工作空间与权限.md) · [产品需求](docs/01-产品需求.md) · [验收基线](docs/02-需求评审.md) · [平台参照](docs/03-竞品调研.md) |
| 系统设计 | [概要设计](docs/04-概要设计.md) · [接口与事务](docs/05-详细设计.md) · [Agent 设计](docs/08-Agent设计.md) · [Harness 能力与审计](docs/09-Harness能力与审计.md) |
| 构建与运行 | [构建与运行](docs/06-构建与运行.md) · [网关协议](docs/07-Gateway详细设计.md) · [平台 API](src/clauseweft/api/README.md) · [独立网关说明](src/clauseweft/gateway/README.md) |
| 参与项目 | [贡献指南](CONTRIBUTING.md) · [编码 Agent 指南](AGENTS.md) · [安全报告指南](SECURITY.md) |

---

## 许可证

代码与随附文档采用 [MIT 许可证](LICENSE)。外部第三方 CLI（Claude Code、DeepSeek Harness）与上游模型供应商服务另须遵守各自的分发和使用条款。
