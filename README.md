# ClauseWeft

**Local-first, auditable AI Agent orchestration and software engineering workbench.**

English | [简体中文](README.zh-CN.md)

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](#quick-start)
[![Zero Runtime Dependencies](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)](#quick-start)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ClauseWeft is a self-hosted workbench that unifies **multi-model orchestration**, **controlled MCP & project tools**, **isolated external coding CLI agents**, and **verifiable code delivery** on your own machine. Its core runtime runs exclusively on the **Python 3.11+ standard library** with SQLite persistence, content-addressed artifacts, and an independent multi-protocol model gateway—keeping provider keys, files, budgets, and hash-chained audit logs strictly under your control.

![ClauseWeft Workbench — Live Parallel Execution Graph](docs/images/workbench-live.webp)

*Live screenshot of the ClauseWeft workbench showing clean task submission, real-time parallel execution tracking (`plan → branch-1 & branch-2 → summary`), recent runs, and external agent jobs. Also available in [Light Mode](docs/images/workbench-light.webp) and [Mobile Layout](docs/images/workbench-mobile.webp).*

---

## Core Capabilities

| Capability | Mode / Interface | What ClauseWeft Delivers |
|---|---|---|
| **Direct Q&A & Multimodal Input** | `single` (default) | Query configured models with local UTF-8 materials, controlled Base64 image attachments (`task.attachments` with automatic `vision` capability routing), project long-term memory, personal preferences, and auditable multi-turn follow-ups with automatic context compaction. |
| **Parallel Analysis & Synthesis** | `complex` | Set an explicit total budget; a planner proposes bounded sub-tasks, workers execute branches concurrently under one dependency graph, and a synthesizer aggregates outputs once all branches succeed. |
| **Controlled Project & MCP Tools** | `mode=tool` | Combine built-in requirement & SQLite FTS5 BM25 knowledge-base search (with hot reload) and registered `stdio` / Streamable `http` MCP servers. Supports single-turn parallel tool calls (`action="tools"`) and risk-tiered approval (`always` vs `auto_read_only`). |
| **Multi-Protocol Model Gateway** | `clauseweft-gateway` | Independent HTTP gateway natively adapting **OpenAI Chat Completions**, **Ollama**, **Anthropic Messages**, and **Gemini API**—with SSE streaming, native tool calling, `response_format`, Base64 multimodal conversion (SSRF-safe), and `cached_tokens` / `reasoning_tokens` accounting. |
| **Isolated External Coding Agents** | **External Agent** (`#agents`) | Run **Claude Code**, **DeepSeek Harness**, **Codex**, **Gemini CLI**, **Kimi**, or **OpenCode** inside non-root, read-only Docker containers over allowlisted file snapshots. Supports pinned-image acceptance tests, bounded auto-fix loops (`max_fix_rounds`), diff line-level comments, and targeted `continue_from` follow-ups. |
| **Verifiable Code Delivery & SOPs** | `workflow` & `sop` | Built-in code delivery workflow (requirement baseline approval, independent review, path-scoped Git patch integration, pinned Docker testing, and delivery manifest export) plus custom published SOP graphs combining `model`, `agent`, and `gate` nodes. |

### External Agent Execution & Line-Level Candidate Review

![ClauseWeft External Agent — Patch, Line Comments & Candidate Review](docs/images/external-agent.webp)

*Live screenshot of an isolated external agent job: real-time event timeline, execution summary, downloadable Git diff, line-level review comments, candidate testing, and read-only review on the frozen snapshot.*

- **Auditable by design**: Dispatch intents, frozen model prices, actual token receipts, request contexts, checkpoints, project memories, and SHA-256 hash-chained audit logs commit atomically in SQLite and can be exported and verified offline (`clauseweft audit --input ...`).
- **Strict data-scope & budget boundaries**: Local-only tasks, memories, references, and MCP servers never route to cloud endpoints; usage is tracked per endpoint revision and currency with zero fabricated estimates.
- **Zero-dependency Python core**: No third-party Python runtime packages required; provider API keys stay isolated inside the gateway or per-run container environment.

---

## Quick Start

### Run from Source

Requires **Python 3.11+**. Run from the repository root:

```sh
PYTHONPATH=src python3.11 -m clauseweft --version
PYTHONPATH=src python3.11 -m clauseweft doctor
PYTHONPATH=src python3.11 -m clauseweft serve --config examples/workbench.json --state ~/.local/share/clauseweft
```

1. Open the loopback URL printed in your terminal (default: `http://127.0.0.1:8766/`).
2. In **Personal Pairing**, enter the local access token printed at startup (for background runs, pass it via `CLAUSEWEFT_CONSOLE_TOKEN`). Existing team deployments can sign in via the account tab.
3. Configure your model endpoints in the [independent gateway](src/clauseweft/gateway/README.md), register them in **Resources & Admin → Models**, authorize them to your workspace, and submit tasks. ClauseWeft ships no preconfigured commercial models or fabricated pricing.
4. For **External Agent** coding and review, build the locked agent image from `examples/agent-image` and register your runner and allowlisted repository. See [Personal Workbench & External Agents](docs/12-个人工作台与外部Agent.md).

### Install in a Virtual Environment

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install .
clauseweft --version
clauseweft doctor
clauseweft serve --config examples/workbench.json --state ~/.local/share/clauseweft
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`. Keep `--state` on trusted local storage outside any tested Git repository.

### Reproducible Offline Routing Preview

Test the deterministic model router locally without starting any server or calling external APIs:

```sh
PYTHONPATH=src python3.11 -m clauseweft route --input examples/route-demo.json
```

In [examples/route-demo.json](examples/route-demo.json), the task enforces `data_scope: "local_only"`, so `local-review` is selected even though `cloud-general` is cheaper. Changing `task.data_scope` to `"public"` on a copy allows the cloud candidate to compete in ranking.

---

## Architecture & Scope

- **Runtime**: Single-instance Python 3.11+ modular monolith (17 registered modules) backed by SQLite, SQLite FTS5, and content-addressed artifacts.
- **Access Model**: Personal owner mode via local pairing, plus team roles (`admin`, `operator`, `viewer`) with explicit workspace and project allowlists enforced server-side across menus, direct URLs, SSE streams, and APIs.
- **Audit, Memory & Checkpoints**: Project-scoped long-term memories (`convention`/`glossary`/`decision`/`lesson`), versioned skills, stable run checkpoints/forks, and offline-verifiable audit exports (`clauseweft audit --input FILE`).
- **Upgrades**: Stop the running server and start the new version pointing `--state` at the same state directory; runs, requirement revisions, memories, candidate SHAs, and test history are preserved.
- **Planned Next (P2)**: Mid-run pending prompt queue, scheduled Cron runs, container egress allowlist proxy, OpenTelemetry trace export, and controlled remote Git/PR handoff. See [Roadmap](docs/13-后续演进与路线图.md).

---

## Verification & Testing

```sh
# Unit & local integration test suite
PYTHONPATH=src python3.11 -m unittest discover -s tests

# Include real Docker container acceptance tests
CLAUSEWEFT_TEST_IMAGE="$(docker image inspect --format '{{.Id}}' python:3.11-slim)" \
  PYTHONPATH=src python3.11 -m unittest discover -s tests -v

# Distribution & release safety checks
python3.11 scripts/check_distribution.py source
python3.11 scripts/check_release.py --checkout
```

Full CI matrix details (including locked agent image tests, Playwright browser E2E, and npm lockfile auditing) are in [.github/workflows/ci.yml](.github/workflows/ci.yml).

---

## Documentation

Most design and operational docs are written in Simplified Chinese:

| Area | Documents |
|---|---|
| **Product & Guides** | [中文 README](README.zh-CN.md) · [Tasks & Model Usage](docs/11-任务执行与模型用量.md) · [Personal Workbench & External Agents](docs/12-个人工作台与外部Agent.md) · [Workspaces & Permissions](docs/10-工作空间与权限.md) · [Product Requirements](docs/01-产品需求.md) · [Acceptance Baseline](docs/02-需求评审.md) · [Roadmap](docs/13-后续演进与路线图.md) |
| **System Design** | [Architecture & Modules](docs/04-概要设计.md) · [API & Transactions](docs/05-详细设计.md) · [Agent Design](docs/08-Agent设计.md) · [Harness & Audit](docs/09-Harness能力与审计.md) |
| **Build & Operations** | [Build, Run & Verification](docs/06-构建与运行.md) · [Gateway Design](docs/07-Gateway详细设计.md) · [Workbench API Reference](src/clauseweft/api/README.md) · [Gateway Guide](src/clauseweft/gateway/README.md) |
| **Community** | [Contributing](CONTRIBUTING.md) · [Coding Agent Guide](AGENTS.md) · [Security Policy](SECURITY.md) |

---

## License

Code and accompanying documentation are released under the [MIT License](LICENSE). External third-party CLIs and upstream model provider services remain subject to their respective licenses and terms of service.
