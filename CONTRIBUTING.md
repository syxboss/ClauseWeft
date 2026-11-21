# 参与 ClauseWeft

第一次接触代码时，可以先启动平台，按项目、需求、SOP、Agent、技能、模型资源、运行和审批的业务入口理解用户路径，再选一个感兴趣的模块。离线选模命令适合独立核对执行层规则。[项目概览](README.zh-CN.md) · [English README](README.md) · [架构和模块职责](docs/04-概要设计.md)

## 准备开发环境

在仓库根目录执行以下命令。源码运行使用 Python 3.11 或更高版本的标准库，无需配置模型密钥。

```sh
PYTHONPATH=src python3.11 -m clauseweft doctor
PYTHONPATH=src python3.11 -m clauseweft route --input examples/route-demo.json
```

第二条命令应选中 `local-review`。把示例复制到临时目录，将副本中的 `task.data_scope` 改为 `public` 后重新预览，可以观察费用偏好如何改变选模结果。运行工作台、登记网关或构建可执行文件时可查阅[构建与运行](docs/06-构建与运行.md)。

## 报告问题、提出功能

- 报告缺陷时附上 Python 版本、操作系统、起始状态、最小输入、执行命令或操作、预期与实际结果。能用示例 JSON 或临时 Git 仓库复现时，优先提供这些材料。
- 提出功能时描述谁遇到了什么问题，给出一条可以验证的验收条件。例如“两条并行分支竞争同一笔余额时，两次预留之和应受预算约束”。已有产品条款或验收编号可直接引用。[产品需求](docs/01-产品需求.md) · [验收设计](docs/02-需求评审.md)
- 涉及越权读取、凭据泄露、数据去向或测试隔离的问题，按[安全报告指南](SECURITY.md)私下提供材料。

## 修改与验证

可按[源码导航](docs/04-概要设计.md#从源码定位一次任务)找到表单、HTTP 入口、调度和工具审批的实现。前端源文件位于 `src/clauseweft/api/web/`，新增资源时也要更新脚本或样式清单。修复缺陷时，尽量用一个在旧实现上失败的用例固定预期行为。测试文件按功能划分，可先运行相关用例，再执行整套回归。

```sh
PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_route_preview.py' -v
PYTHONPATH=src python3.11 -m unittest discover -s tests
```

常规测试使用确定性模型替身。HTTP 用例要能监听本机回环端口，交付用例要能调用 Git。实测隔离行为时，还需要本机可用的 Docker 引擎和已有的 Python 镜像。确认下面第一条命令返回完整镜像 ID 后运行第二条，保留结果中的失败、跳过和通过数。

```sh
docker image inspect --format '{{.Id}}' python:3.11-slim
CLAUSEWEFT_TEST_IMAGE="$(docker image inspect --format '{{.Id}}' python:3.11-slim)" PYTHONPATH=src python3.11 -m unittest discover -s tests -v
```

修改 CLI、包名或构建入口时，`python3.11 scripts/check_distribution.py source` 可验证源码命令在临时目录里的行为和退出码。已安装的 wheel 用 `installed` 模式检查工作台、网关两个终端命令及包内示例配置。[分发验证脚本](scripts/check_distribution.py) · [构建和验证 wheel](docs/06-构建与运行.md#python-分发包)

GitHub Actions 在推送和拉取请求上使用 Python 3.11 和 3.14 运行测试。CI 核对外部 Agent 生产依赖公告，按同一份 npm 锁文件安装两个 CLI，并用本机回环服务检验它们的协议。Python 3.11 作业还构建 Agent 镜像，在关闭网络的容器里核验真实 DeepSeek 工具守卫，并用 Chromium 测试配对、修改、下载与评审的完整流程。每个作业最后从源码分发包构建 wheel，在新的虚拟环境中安装和检查。[CI 配置](.github/workflows/ci.yml) · [复现全部外部 Agent 用例的环境变量](src/clauseweft/agent_runtime/README.md#验证)

企业平台改动还应检查账号角色的服务端限制、项目和需求的运行关联、已发布模板与旧执行快照、业务写入和审计的事务一致性。前端操作使用真实 API，提供清晰的加载、空态、错误和权限反馈。

技能与上下文改动须验证旧技能修订和请求工件保持可读，实际外发消息与证据一致，本地数据约束继续生效。检查点及派生改动须核对来源、独立预算与人工审批重置。审计改动须覆盖成功、拒绝、失败、重试、后台事件和旧记录迁移，检查导出文件能按固定链头复验，并使用假凭据证明秘密不会进入审计正文。[Harness 与审计契约](docs/09-Harness能力与审计.md)

## 核对开源发布包

发布前，在仓库根目录扫描 Git 跟踪文件和未忽略的新文件，然后将当前版本构建到空目录。开发环境需要安装 Python 构建工具 `build`。

```sh
python3.11 scripts/check_release.py --checkout
release_dir="$(mktemp -d)"
python3.11 -m build --outdir "$release_dir"
python3.11 scripts/check_release.py "$release_dir"/*.tar.gz "$release_dir"/*.whl
```

检查会拒绝上溯或绝对路径、归档链接、常见本地状态文件和可识别的凭据。源码包需要包含个人工作台指南、Agent 镜像配方和工作台示例，wheel 需要包含执行器代码、配对入口和工作台资源。扫描只报告文件和类别。人工复核发布清单与许可证，并检查未被规则识别的敏感业务资料。[发布包检查](scripts/check_release.py)

## 提交供审查的改动

请说明问题及用户可见的变化，列出验证命令、结果和未覆盖的环境。HTTP 改动附一个脱敏的请求与响应，界面改动可附截图，持久化变更应说明旧状态目录的读取与恢复行为。同步更新受影响的命令示例和模块文档；打包产物保留在本机的 `build/` 和 `dist/`。

项目采用 MIT 许可证。涉及第三方代码或资源时，保留相应的版权和许可说明。[项目许可证](LICENSE)
