# 独立模型网关 Gateway

Gateway 将一个已选定的端点别名和一组文本消息转成一次上游调用。目前支持 Ollama 原生协议与 HTTP Chat Completions 兼容协议。它的运行时只需要 Python 3.11+ 标准库和 JSON 配置，无需平台数据库、路由器或 worker。源码、独立镜像和本机编译程序提供相同的 HTTP 接口。[接口与内部设计](../../../docs/07-Gateway详细设计.md)

## 在本机启动

在仓库根目录先做配置预检，再启动服务。预检输出配置版本与端点数量，不连接上游：

```sh
PYTHONPATH=src python3.11 -m clauseweft.gateway --config src/clauseweft/gateway/example.json --check-config
PYTHONPATH=src python3.11 -m clauseweft.gateway --config src/clauseweft/gateway/example.json
```

也可以把整个 `gateway/` 目录复制到其他位置，在它的**父目录**执行 `python3.11 -m gateway --config gateway/example.json`。默认监听 `127.0.0.1:8765`。通过 `--host` 和 `--port` 修改监听地址。安装整个项目后，也可以使用 `clauseweft-gateway --config FILE`。配置错误会在启动阶段报告；上游可达性在实际调用时检查。

在另一个终端查询：

```sh
curl -sS http://127.0.0.1:8765/healthz
curl -si http://127.0.0.1:8765/v1/models
```

第一条返回 `{"status":"ok"}`，第二条列出已配置的端点别名并返回配置 `ETag`。两条都不触发模型推理。[本机示例](example.json)使用 `local-review` 指向本机 Ollama；先把 `model` 改成已经安装的模型名称，再重启网关。随后可请求一次生成：

```sh
curl -sS http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-review","messages":[{"role":"user","content":"用一句话说明验收条件。"}],"max_tokens":128}'
```

`GET /v1/models` 的 `data[].max_output_tokens` 是该端点的有效限制。工作台默认请求最多 1024 个输出 token，派发时允许网关采用更低的端点上限。直接 HTTP 客户端默认使用严格校验，需要同样行为时发送 `X-ClauseWeft-Token-Limit: clamp`。生成回执的 `gateway_metadata.max_output_tokens` 记录实际传给供应商的上限。对于 JSON 决策请求，`X-ClauseWeft-Output-Format: json` 启用 Ollama 的 `format: "json"` 和 `temperature=0`。Chat Completions 保留通用文本请求，平台在两条路径上都校验决策内容。

若该端点可达，答复在 `choices[0].message.content`。配置入站令牌后，查询模型和生成时都需加 `-H "Authorization: Bearer $CLAUSEWEFT_GATEWAY_TOKEN"`。未设置令牌时只允许回环监听、本机 Host 与本机同源请求。公开健康检查不报告凭据或端点详情。

升级已有鉴权部署时，在网关 JSON 顶层写入 `"token_env": "CLAUSEWEFT_GATEWAY_TOKEN"`，在网关进程和客户端分别设置相应变量后重启。显式指定 `token_env` 时，缺少令牌会导致启动失败。验证时，请求 `GET /v1/models` 应在缺少令牌时返回 401，携带正确令牌时返回 200。

## 添加一个模型

在 `endpoints` 数组里增加一个对象，向配置文件写入环境变量**名称**。将下面的域名、模型标识换成实际服务的值。

```json
{
  "id": "engineering",
  "protocol": "chat_completions",
  "base_url": "https://models.example.com/v1",
  "model": "configured-model-name",
  "api_key_env": "ENGINEERING_PROVIDER_KEY",
  "max_tokens": 1024
}
```

网关追加 `/chat/completions`，原生 Ollama 端点则追加 `/api/chat`。调用方提交 `model: "engineering"`，不会接触供应商密钥。改配置后重启，再读取 `/v1/models` 的 ETag。已绑定旧版本的请求可带 `If-Match: "<旧摘要>"` 验证失效，此时返回 412，供应商不会收到请求。网关的请求 ID 用于排查问题；相同请求重发可能产生第二笔费用。

## 单独用 Docker 运行

从仓库根目录构建。构建上下文只有本目录，镜像使用非 root 用户。默认配置通过 `host.docker.internal` 访问宿主机 Ollama。需确保服务监听容器实际可达的接口，并将容器配置中的模型名改成已安装的名称。连接托管服务时使用 HTTPS 地址。

```sh
docker build -t clauseweft-gateway:dev src/clauseweft/gateway
export CLAUSEWEFT_GATEWAY_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker run --rm --read-only --cap-drop=ALL \
  -p 127.0.0.1:8765:8765 -e CLAUSEWEFT_GATEWAY_TOKEN \
  --add-host=host.docker.internal:host-gateway \
  clauseweft-gateway:dev
```

在保留同一令牌的客户端终端查询 `GET /v1/models`。自定义配置可在镜像名前加 `--mount "type=bind,source=$PWD/gateway.json,target=/etc/clauseweft/gateway.json,readonly"`，并为相应供应商加 `-e ENGINEERING_PROVIDER_KEY`；文件须可由 UID 65532 读取。镜像默认读取[容器示例](container.example.json)。上游是宿主机以外的服务时不需要 `--add-host`。非回环入站应运行在受控网络；跨主机部署需配置 TLS 终止。

## 本机编译并运行

安装目标平台 C 工具链和 CPython 3.11，在仓库根目录执行：

```sh
python3.11 -m venv .venv-gateway-build
.venv-gateway-build/bin/python -m pip install Nuitka==4.3.5 ordered-set==4.1.0 zstandard==0.25.0
.venv-gateway-build/bin/python src/clauseweft/gateway/build_tools/compile.py
./dist/gateway-native/launch_gateway.dist/clauseweft-gateway \
  --config src/clauseweft/gateway/example.json
```

分发整个 `launch_gateway.dist/` 目录，在目标机另行提供配置和凭据。编译脚本只导入 gateway，可在独立副本旁运行；默认输出到当前目录的 `dist/gateway-native/`。Windows 使用虚拟环境的 `Scripts/python.exe`，生成的入口名为 `clauseweft-gateway.exe`；应在目标系统构建和验证。

## 故障定位与验证

| 现象 | 可操作的检查 |
|---|---|
| 健康正常，但生成失败 | 确认上游监听地址、配置的模型名称、凭据环境变量和协议路径；容器内的 127.0.0.1 指向容器自身。 |
| 401 或 412 | 前者检查客户端网关令牌；后者重新核对端点最新配置，再决定是否派发。 |
| 429 | 查看 `error.code`，区分本地并发已满与上游限流。 |
| 502 或 504 | 核对协议、供应商响应或超时；外部执行状态可能未知，重发前核对原尝试。 |

[集成测试](../../../tests/test_gateway.py)在本机启动临时网关和两个协议的模拟上游，校验实际 HTTP 数据流，不调用商业模型。从仓库运行 `PYTHONPATH=src python3.11 -m unittest discover -s tests`。设置 `TEST_GATEWAY_BINARY` 为原生可执行文件绝对路径时，相同测试改为启动该成品。
