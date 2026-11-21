# 个人 Agent 执行器

工作台从本机可信配置的 `agent_runners` 加载执行器。在任务页选择已登记的执行器、仓库和基准提交。每次运行都会启动新容器。会话 ID 用于核对供应商回执，跨任务评审通过已保存的候选文件和差异交接。

| 执行器 | 任务输入 | 完成条件 |
|---|---|---|
| Claude Code | 目标通过标准输入交给 `claude -p`。筛选过的普通文本文件放在容器内的临时工作目录，Claude 可用 Read、Glob、Grep、Edit、Write。 | 收到成功的 `result` 事件、进程正常退出、输出文件通过校验。工作台据此计算可供检查的 Git 差异。 |
| DeepSeek Harness | 使用 `dsh --profile headless --json` 的只读模式。收到工具守卫的就绪信号后，适配器才提交任务与文件文本。守卫拒绝模型发起的所有工具调用。 | 收到 `final` 事件，其中 `reason.kind` 为 `completed`，且进程正常退出。 |

## 镜像与凭据

镜像需要 Linux、`/usr/local/bin/python3` 和注册路径上的 CLI。参考配置使用 Claude Code 2.1.283、DeepSeek Harness 0.2.2-rc.5。DeepSeek CLI 需要 Node.js 24.9 或更高版本、支持 `tools.guard` 的运行时和 headless JSON 事件接口。实际运行时由固定的本地 Docker 镜像 ID 决定。[Claude Code 接口](https://code.claude.com/docs/en/headless) · [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)

`token_env` 指定工作台进程中保存供应商 API key 的变量名，容器适配器分别以 `ANTHROPIC_API_KEY` 或 `DEEPSEEK_API_KEY` 传给对应 CLI。每次只传这一项供应商凭据。模型可以将用户的任务及其获准的项目材料发送给对应的外部服务。Docker 使用本机套接字和 bridge 网络；需要限制外联地址的部署应在容器网络层配置出口策略。

两个执行器共用只读根文件系统、非特权 UID、能力移除、进程数和内存限制。工作副本及临时目录有容量限制，用户主目录、仓库、Docker 套接字均不挂载。接收任务输出时按 UTF-8 字节计数，最多导出 512 个普通文本文件及 2 MiB 正文。主进程和容器各有时限，失败和取消不会产生可接纳的补丁。原始 stderr、工具参数和思考内容不进入运行事件。用量来自实际回执，CLI 运行时限不代表供应商费用上限。

## 验证

在仓库根目录运行 `PYTHONPATH=src python3.11 -m unittest discover -s tests -p test_agent_runtime.py -v`。要同时运行容器与原生客户端用例，可另外设置以下测试变量。

| 测试变量 | 取值和覆盖范围 |
|---|---|
| `CLAUSEWEFT_TEST_IMAGE` | 本机带 `/usr/local/bin/python3` 的固定 Python 镜像 ID，在真实容器内验证成功、失败、取消、只读评审和输出过滤。 |
| `CLAUSEWEFT_TEST_AGENT_IMAGE` | 带两个固定版 CLI 的 Agent 镜像 ID。全量测试中的 `test_installed_agent_image.py` 在无网络、非 root、只读根文件系统及 `/tmp=noexec` 条件下核对真实版本，并等到 DeepSeek 工具守卫就绪。 |
| `CLAUSEWEFT_TEST_BROWSER` | 使用已安装的 `chrome` 或 Playwright 的 `chromium`，搭配 `CLAUSEWEFT_TEST_IMAGE` 运行个人工作台浏览器端到端测试。需要开发依赖 Playwright，CI 安装和运行命令见[工作流](../../../.github/workflows/ci.yml)。 |
| `CLAUSEWEFT_TEST_CLAUDE` | 本机 Claude Code 的可执行文件绝对路径。脚本在空目录使用回环模拟 Anthropic Messages 服务完成一次真实 CLI 调用。 |
| `CLAUSEWEFT_TEST_DSH` | 本机固定版 dsh 的可执行文件绝对路径，Node.js 须在 PATH 中。回环模拟 DeepSeek Messages 服务发起一次工具调用，验证真实守卫拒绝后再完成评审。 |

测试使用生成的临时资料和虚构的凭据。
