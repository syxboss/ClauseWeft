# 个人工作台与外部 Agent

ClauseWeft 0.0.1 有两个执行入口。工作台的文本任务经过模型网关，支持材料、后续追问和并行汇总。「外部 Agent」让 Claude Code 修改选定仓库的授权文件，或让 DeepSeek Harness 只读评审。外部任务有独立记录，产物保存在本机状态目录。

## 从个人项目开始

首次启动使用 [README 中的命令](../README.zh-CN.md)，在「个人配对」输入本机访问令牌。配对会创建「我的工作空间」「我的项目」，并签发 12 小时的具名所有者会话。原管理令牌留在本机；到期可重新配对，恢复同一个身份。已经有成员或业务数据的实例可继续从原账号入口登录。个人所有者及管理员可以使用自己的外部 Agent 任务记录。[权限和升级](10-工作空间与权限.md)

## 准备运行镜像

外部任务在 Linux 或 macOS 上运行工作台时使用本机 Git、Docker CLI 和 Linux 容器引擎。镜像内需要 Python 3 和所登记的 Agent CLI。仓库示例在 Debian 基础镜像安装系统 Python 3，用 npm 的锁文件确定两种 CLI 及其传递依赖的版本。镜像构建上下文只开放 Dockerfile、package.json 和 package-lock.json。构建时需要能访问镜像仓库、Debian 软件源和 npm。使用者自行取得供应商服务及凭据。[构建文件](../examples/agent-image/Dockerfile) · [Claude Code 非交互模式](https://code.claude.com/docs/en/headless) · [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)

```sh
docker build -t clauseweft-personal-agents:0.0.1 examples/agent-image
docker image inspect --format '{{.Id}}' clauseweft-personal-agents:0.0.1
```

构建和 CI 用 `npm ci --ignore-scripts` 安装锁定依赖，随后显式运行 Claude Code 包内的安装程序，以启用对应平台的原生二进制。镜像内 `/usr/local/bin/claude` 与 `/usr/local/bin/dsh` 指向 `/opt/agent-clis` 内的锁定安装，`/usr/local/bin/python3` 指向 Debian 的解释器。CI 用同一份锁运行 `npm audit --omit=dev --audit-level=high` 和两种真实 CLI 的回环协议测试。更新 CLI 或依赖覆盖后，一并更新锁、构建镜像、运行协议测试，再在可信配置中登记新的镜像 ID。[依赖声明](../examples/agent-image/package.json) · [依赖锁](../examples/agent-image/package-lock.json)

## 连接自己的仓库

1. 把 [agent-workbench.json](../examples/agent-workbench.json) 复制到源码仓库以外。在两个 `image` 字段填写上一步返回的完整 `sha256:` 镜像 ID。只使用一个执行器时可删去另一条配置。
2. 将 `repositories.my-repo.path` 换成已有提交的本机 Git 仓库绝对路径。在 `allowlist` 中逐项登记任务所需文件或子目录。实例及该仓库都须明确使用 `data_scope: "public"` 才能外发。普通本地模型可用原有 `local_only` 配置。
3. 在启动工作台的进程环境设置示例引用的 `PERSONAL_CLAUDE_KEY` 或 `PERSONAL_DEEPSEEK_KEY`。JSON 中只填写环境变量名。代码交付、模型网关等现有配置可与 `agent_runners` 保存在同一文件。
4. 使用这份配置启动工作台。在「外部 Agent → 仓库授权」确认当前工作空间可用的仓库。已运行的实例应使用原来的状态目录和管理令牌重启以保留个人项目及历史。

```sh
PYTHONPATH=src python3.11 -m clauseweft serve --config /absolute/path/to/agent-workbench.json --state ~/.local/share/clauseweft
```

## 运行、评审与采用补丁

选项目后从「外部 Agent」发起任务，确认执行器、仓库、基准提交和外发范围。默认 `HEAD` 在提交任务时解析为具体提交，未提交的本地改动不在输入中。任务详情记录排队、运行和结局。执行中可以取消。控制进程中断时，已开始的任务保留中断状态；重新提交会启动另一项任务，可能产生新的供应商用量。

Claude Code 在私有副本内工作。成功且确有改动时，可以查看或下载 Git 补丁，或选「用 DeepSeek 评审这份改动」。工作台把前一次成功任务的固定候选和补丁交给只读评审器。输入与补丁合计最多 512 个 UTF-8 文件、2 MiB。首页列出当前范围内自己的外部任务，运行中的任务优先。每次评审使用新会话。文件范围校验与模型评语各自记录；评语不能作为测试已通过的证明。需要执行登记的容器验收流程时，从工作台选择「代码交付」。供应商账户可能产生独立费用，外部任务的超时按秒计算，模型网关的费用上限与账本只适用于网关任务。

在与任务相同的基准上核对补丁，例如在自己的工作树中先运行以下命令。检查结果和测试证据后，再决定是否应用、提交或推送。

```sh
git apply --stat /path/to/downloaded.patch
git apply --check /path/to/downloaded.patch
```

## API

接口均在 `/api/v1` 下，使用当前所有者或管理员的 Bearer 会话。写操作沿用幂等键，取消操作还需携带详情返回的修订号。

| 方法与路径 | 用途 |
|---|---|
| `GET agent-jobs/options?project_id=ID` | 查看当前项目可选执行器与可授权外发的仓库 |
| `POST agent-jobs` | 传入 `project_id, runner_id, repo_id, prompt`，可选 `base_sha`；对前次候选评审时改传 `review_of` |
| `GET agent-jobs?project_id=ID` | 按 `limit, cursor` 分页读取自己的任务 |
| `GET agent-jobs/ID` | 读取状态、实际 SHA、事件、文本结果和补丁 |
| `POST agent-jobs/ID/cancel` | 使用 `If-Match` 和空 JSON 对象取消该任务 |

普通团队成员和其他管理员各自的外部任务受服务端访问控制。提供 Docker 服务和 CLI 的宿主、固定镜像、供应商，以及本机状态目录均属于部署的信任边界。[安全说明](../SECURITY.md)
