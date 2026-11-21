# testing · 受限执行与验收证据

本地工作台使用已登记的 Docker 镜像和负责人配置的黑盒用例。每条用例向候选程序提供输入，宿主适配器比较标准输出字节并记录容器的实际退出码。期望输出保存在受信套件中，候选程序只能看到本次用例的输入。该适配器适合用标准输入输出验证的命令行程序。

下列配置使用本机已有的 Python 镜像。通过 `docker image inspect --format '{{.Id}}' python:3.11-slim` 查到不可变镜像 ID 后，将其填入配置。镜像需要包含 `/usr/local/bin/python`，且不能声明数据卷。

```json
{
  "image": "sha256:<64位镜像ID>",
  "timeout_seconds": 30,
  "commands": {"run_export": ["python", "app.py"]},
  "cases": [{"id": "member-denied", "clause_id": "CSV-01", "command_id": "run_export",
             "stdin": "member\n", "stdout": "denied\n", "required_files": ["app.py"]}]
}
```

- `freeze_suite(spec_version_id, config)` 固定必测 ID、条款、断言、映射、镜像和时间上限，生成套件与环境摘要。
- `start(candidate, suite, files)` 在每个用例的一次性容器中运行登记的 argv。候选内容通过标准输入传入内存文件系统。根文件系统只读，工作目录限 32 MiB，容器使用普通 UID、单 CPU、128 MiB 内存、32 个进程上限、无网络、无宿主目录挂载。程序由镜像内登记的解释器执行。全套用例共享超时上限，单用例输出最多 64 KiB。
- `verify(report, candidate, suite)` 只用于服务端读取执行器产生的报告。核对当前基线、提交、树与环境，每项必测 ID 须出现一次、实际返回零且输出摘要满足冻结的断言。缺文件为 missing，超时、输出超限或中断为 incomplete，缺镜像或无运行时为 needs_configuration。工作台另行检查条款覆盖、代码评审和负责人接纳。

实际隔离验收需要启动 Docker。运行 `CLAUSEWEFT_TEST_IMAGE="$(docker image inspect --format '{{.Id}}' python:3.11-slim)" PYTHONPATH=src python3.11 -m unittest discover -s tests -p test_delivery.py -v`。测试会生成四个候选，分别检查失败、缺项、成功与超时，并验证旧提交的报告无法批准新候选。[接口与验收设计](../../../docs/05-详细设计.md)
