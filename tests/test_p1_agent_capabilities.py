"""Tests for P1 capabilities:
- P1-1: Controlled MCP client (stdio & HTTP), extensible tool registry, and parallel tool calls
- P1-2: Generic external agent adapters, explicit capability matrix, and SOP `agent` nodes
- P1-3: Diff line-level review comments and targeted `continue_from` continuation
- P1-4: SQLite FTS5 BM25 + hot reload, auditable conversation compaction, and project long-term memory
- P1-5: Multimodal image attachments with automatic `vision` capability routing
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from clauseweft import agent_runtime, audit, project_tools
from clauseweft.api.application import Application
from clauseweft.api.http_contracts import Problem
from platform_fixture import prepare_team


PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _git(repo, *args):
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "-c", f"core.hooksPath={os.devnull}",
            *args,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )


class _McpHttpHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        auth = self.headers.get("Authorization")
        self.server.received.append((auth, payload))
        method = payload.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}
        elif method == "tools/call":
            params = payload.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "create_ticket":
                result = {
                    "content": [{"type": "text", "text": json.dumps({
                        "ticket_id": "TKT-900",
                        "summary": args.get("summary"),
                    })}],
                    "isError": False,
                }
            else:
                result = {"content": [{"type": "text", "text": "unknown"}], "isError": True}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class P1AgentCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("def greet():\n    return 'v0'\n", encoding="utf-8")
        _git(self.repo, "init")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "initial")
        self.base_sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

        # Local stdio MCP script
        self.mcp_script = self.root / "stdio_mcp.py"
        self.mcp_script.write_text(
            "import json, sys\n"
            "for raw in sys.stdin:\n"
            "    raw = raw.strip()\n"
            "    if not raw:\n"
            "        continue\n"
            "    msg = json.loads(raw)\n"
            "    mid = msg.get('id')\n"
            "    if mid is None:\n"
            "        continue\n"
            "    if msg.get('method') == 'initialize':\n"
            "        res = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}}\n"
            "    elif msg.get('method') == 'tools/call':\n"
            "        args = msg.get('params', {}).get('arguments', {})\n"
            "        res = {'content': [{'type': 'text', 'text': 'metric:' + str(args.get('service', ''))}], 'isError': False}\n"
            "    else:\n"
            "        res = {}\n"
            "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': res}) + '\\n')\n"
            "    sys.stdout.flush()\n",
            encoding="utf-8",
        )

        # Local Streamable HTTP MCP server
        self.mcp_http = ThreadingHTTPServer(("127.0.0.1", 0), _McpHttpHandler)
        self.mcp_http.received = []
        threading.Thread(target=self.mcp_http.serve_forever, daemon=True).start()
        self.addCleanup(self.mcp_http.shutdown)
        self.addCleanup(self.mcp_http.server_close)

        # Local project reference file
        self.ref_path = self.root / "kb.json"
        self.ref_path.write_text(json.dumps([
            {"id": "doc-body", "title": "通用运维指南", "text": "故障排查时先核对网关延迟与错误率，再检查退款队列。"},
            {"id": "doc-title", "title": "退款队列排障手册", "text": "对账延迟超过 5 分钟时触发告警。"},
            {"id": "doc-en", "title": "Circuit Breaker Policy", "text": "Open breaker after five consecutive upstream timeouts."},
        ], ensure_ascii=False), encoding="utf-8")

        self.image = "sha256:" + "2" * 64
        os.environ["P1_MCP_HTTP_TOKEN"] = "mcp-secret-token-xyz"
        self.addCleanup(os.environ.pop, "P1_MCP_HTTP_TOKEN", None)

        self.config_path = self.root / "workbench.json"
        self.config_path.write_text(json.dumps({
            "data_scope": "public",
            "gateways": {"g": {"url": "http://127.0.0.1:8765"}},
            "repositories": {
                "repo": {
                    "path": str(self.repo),
                    "allowlist": ["app.py"],
                    "paths": {"code": ["app.py"], "test-design": ["app.py"]},
                    "data_scope": "public",
                }
            },
            "project_references": {
                "default": {
                    "name": "排障知识库",
                    "path": str(self.ref_path),
                    "data_scope": "public",
                }
            },
            "mcp_servers": {
                "metrics": {
                    "transport": "stdio",
                    "command": [sys.executable, str(self.mcp_script)],
                    "data_scope": "local_only",
                    "tools": {
                        "query_slo": {
                            "description": "读取服务实时 SLO",
                            "risk": "read_only",
                            "parameters": {"type": "object", "required": ["service"]},
                        }
                    },
                },
                "tickets": {
                    "transport": "http",
                    "url": f"http://127.0.0.1:{self.mcp_http.server_port}/mcp",
                    "token_env": "P1_MCP_HTTP_TOKEN",
                    "data_scope": "public",
                    "tools": {
                        "create_ticket": {
                            "description": "创建外部工单",
                            "risk": "external_effect",
                            "parameters": {"type": "object", "required": ["summary"]},
                        }
                    },
                },
            },
            "agent_runners": {
                kind: {
                    "kind": kind,
                    "image": self.image,
                    "executable": f"/usr/local/bin/{kind}",
                    "token_env": f"P1_{kind.upper()}_KEY",
                }
                for kind in ("claude", "deepseek", "codex", "gemini", "kimi", "opencode")
            },
        }), encoding="utf-8")
        self.app = Application(self.root / "state", self.config_path)
        for eid, identity, location, caps in (
            ("local-text", "model-local-text", "local", ["structured_output"]),
            ("local-vision", "model-local-vision", "local", ["structured_output", "vision"]),
            ("cloud-vision", "model-cloud-vision", "cloud", ["structured_output", "vision"]),
        ):
            self.app.catalog.save({
                "endpoint_id": eid,
                "model_identity": identity,
                "gateway_id": "g",
                "gateway_etag": '"v1"',
                "location": location,
                "fit": {"general": 4, "requirements": 4, "review": 4, "code": 4, "test": 4},
                "currency": "CNY",
                "context_tokens": 64000,
                "enabled": True,
                "allowed": True,
                "identity_verified": True,
                "capabilities": caps,
                "priority": 1,
                "input_micros_per_million": 1000,
                "output_micros_per_million": 2000,
            })
        prepare_team(self.app)
        with self.app.store.transaction() as db:
            ws = self.app.platform.get(db, "workspaces", "default")
            self.app.platform.save(
                db, "workspaces",
                {"endpoint_ids": ["local-text", "local-vision", "cloud-vision"], "repository_ids": ["repo"]},
                "default", ws["revision"],
            )
            proj = self.app.platform.get(db, "projects", "default")
            self.app.platform.save(
                db, "projects",
                {"repository_id": "repo"},
                "default", proj["revision"],
            )

    def api(self, method, path, body=None, *, query=None, expected=None, principal=None):
        parts = tuple(seg for seg in path.strip("/").split("/") if seg)
        return self.app.handle(
            method, parts, query or {}, body or {},
            key=uuid4().hex if method != "GET" else None,
            expected=expected,
            principal=principal,
        )

    def test_mcp_stdio_and_http_tools_risk_tiering_and_parallel_calls(self):
        # Create one project requirement so parallel read batch returns both requirement & MCP data
        self.api("POST", "requirements", {
            "project_id": "default", "title": "支付网关告警治理", "description": "核对 SLO", "priority": "high",
        })
        replies = iter([
            # Step 1: parallel read_only batch (MCP stdio + local search_requirements) -> auto-approved!
            json.dumps({
                "action": "tools",
                "calls": [
                    {"tool_id": "mcp:metrics/query_slo", "arguments": {"service": "payment"}},
                    {"tool_id": "search_requirements", "arguments": {"query": "支付网关"}},
                ],
            }, ensure_ascii=False),
            # Step 2: external_effect HTTP MCP tool -> MUST pause for human approval!
            json.dumps({
                "action": "tool",
                "tool_id": "mcp:tickets/create_ticket",
                "arguments": {"summary": "跟进 payment SLO 指标"},
            }, ensure_ascii=False),
            # Step 3: final answer
            json.dumps({"action": "finish", "answer": "已核对 metric:payment 并创建工单 TKT-900。"}, ensure_ascii=False),
        ])

        def fake_generate(endpoint, messages, max_tokens):
            # Because mcp:metrics is local_only, router MUST choose a local endpoint!
            self.assertIn(endpoint["endpoint_id"], ("local-text", "local-vision"))
            return next(replies), {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}, f"req-mcp-{uuid4().hex}", "stop", max_tokens

        with patch.object(self.app.catalog, "generate", side_effect=fake_generate):
            status, run = self.api("POST", "runs", {
                "mode": "tool",
                "project_id": "default",
                "allowed_tools": [
                    "mcp:metrics/query_slo",
                    "search_requirements",
                    "mcp:tickets/create_ticket",
                ],
                "tool_approval_policy": "auto_read_only",
                "max_tool_steps": 4,
                "task": {"text": "查询 payment 的 SLO 与相关需求，并创建跟进工单", "data_scope": "public"},
                "budget": {"currency": "CNY", "limit_micros": 500_000},
            })
            self.assertEqual(status, 201)
            rid = run["run_id"]
            # Drain worker: decide-1 runs, auto-approves parallel read batch, decide-2 runs and pauses on external_effect!
            while self.app.runs.work_once():
                pass
            _, paused = self.api("GET", f"runs/{rid}")
            self.assertEqual(paused["status"], "needs_input")
            self.assertEqual(paused["tool_proposal"]["id"], "mcp:tickets/create_ticket")
            self.assertEqual(paused["tool_proposal"]["risk"], "external_effect")

            # Verify mid-run MCP digest tampering blocks execution, then restore and approve
            orig_digest = self.app.mcp_servers["tickets"]["digest"]
            self.app.mcp_servers["tickets"]["digest"] = "0" * 64
            with self.assertRaises(Problem) as ctx:
                self.api("POST", f"runs/{rid}/tool-decisions", {
                    "action": "approve",
                    "proposal_ref": paused["tool_proposal"]["proposal_ref"],
                    "reason": "尝试在配置变更后批准",
                }, expected=paused["revision"])
            self.assertEqual(ctx.exception.status, 409)
            self.app.mcp_servers["tickets"]["digest"] = orig_digest

            # Approve external_effect HTTP MCP call
            status, approved = self.api("POST", f"runs/{rid}/tool-decisions", {
                "action": "approve",
                "proposal_ref": paused["tool_proposal"]["proposal_ref"],
                "reason": "确认创建外部工单",
            }, expected=paused["revision"])
            self.assertEqual(status, 200)
            self.assertEqual(self.mcp_http.received[-1][0], "Bearer mcp-secret-token-xyz")

            while self.app.runs.work_once():
                pass
            _, done = self.api("GET", f"runs/{rid}")
            self.assertEqual(done["status"], "completed")
            final_node = [n for n in done["nodes"] if n.get("is_agent_answer")][0]
            _, art = self.api("GET", f"runs/{rid}/artifacts/{final_node['artifact']}")
            self.assertIn("TKT-900", art["content"])

        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])

    def test_generic_agent_capabilities_and_sop_agent_nodes(self):
        # 1. Verify all 6 CLI kinds and explicit capability matrix
        _, options = self.api("GET", "agent-jobs/options", query={"project_id": "default"})
        kinds = {r["kind"] for r in options["runners"]}
        self.assertEqual(kinds, {"claude", "deepseek", "codex", "gemini", "kimi", "opencode"})
        self.assertIn("capabilities", options)
        self.assertTrue(options["capabilities"]["codex"]["editable"])
        self.assertFalse(options["capabilities"]["deepseek"]["editable"])
        self.assertTrue(options["capabilities"]["deepseek"]["read_only_guard"])

        # 2. Verify generic JSON stream parser for codex / gemini
        events = []
        resp = agent_runtime.protocol.Response("codex", "secret-key-val", events.append)
        resp.consume({"type": "tool_use", "name": "edit_file"})
        resp.consume({"type": "item.completed", "item": {"type": "agent_message", "text": "Implemented feature"}})
        resp.consume({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 8}})
        parsed = resp.result()
        self.assertEqual(parsed["text"], "Implemented feature")
        self.assertEqual(parsed["usage"], {"input_tokens": 12, "output_tokens": 8})
        self.assertEqual(events[0], {"type": "tool_use", "tool": "tool"})

        # 3. Publish and execute a multi-stage SOP containing model -> agent (codex) -> gate
        _, tpl = self.api("POST", "templates", {
            "workspace_id": "default",
            "name": "混合模型与外部CLI研发SOP",
            "description": "模型拆解方案后交由 Codex CLI 修改代码，再经人工关卡放行",
            "execution_mode": "sop",
            "nodes": [
                {"node_id": "design", "label": "设计方案", "kind": "model", "stage": "code", "dependencies": []},
                {"node_id": "implement", "label": "CLI改码", "kind": "agent", "stage": "code",
                 "runner_id": "codex", "repo_id": "repo", "base_sha": self.base_sha, "dependencies": ["design"]},
                {"node_id": "signoff", "label": "人工验收", "kind": "gate", "stage": "review",
                 "dependencies": ["implement"]},
            ],
        })
        self.api("POST", f"templates/{tpl['id']}/publish", {}, expected=tpl["revision"])

        def fake_model(endpoint, messages, max_tokens):
            return "请将 greet 返回值改为 sop-v1", {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16}, f"r1-{uuid4().hex}", "stop", max_tokens

        seen_prompts = []

        def fake_agent(runner, work_dir, prompt, on_event=None, cancelled=None):
            seen_prompts.append((runner.kind, prompt))
            (Path(work_dir) / "app.py").write_text("def greet():\n    return 'sop-v1'\n", encoding="utf-8")
            return {"text": "Updated greet to sop-v1", "usage": {"input_tokens": 15, "output_tokens": 5}, "session_id": "s-codex"}

        with patch.object(self.app.catalog, "generate", side_effect=fake_model), \
             patch("clauseweft.agent_runtime.run_agent", side_effect=fake_agent):
            status, run = self.api("POST", "runs", {
                "project_id": "default",
                "template_id": tpl["id"],
                "task": {"text": "实现 sop-v1 问候语"},
                "budget": {"currency": "CNY", "limit_micros": 200_000},
            })
            self.assertEqual(status, 201)
            rid = run["run_id"]
            self.assertTrue(self.app.runs.work_once())
            # Model worker must NOT claim `kind='agent'` node
            self.assertFalse(self.app.runs.work_once())
            # SOP agent worker claims and executes `implement`
            self.assertTrue(self.app.agent_jobs.work_sop_agent_once())
            self.assertEqual(seen_prompts[0][0], "codex")
            self.assertIn("请将 greet 返回值改为 sop-v1", seen_prompts[0][1])

            _, mid = self.api("GET", f"runs/{rid}")
            self.assertEqual(mid["status"], "needs_input")
            impl_node = next(n for n in mid["nodes"] if n["node_id"] == "implement")
            self.assertEqual(impl_node["state"], "succeeded")
            _, impl_art = self.api("GET", f"runs/{rid}/artifacts/{impl_node['artifact']}")
            impl_payload = json.loads(impl_art["content"])
            self.assertIn("+    return 'sop-v1'", impl_payload["patch"])
            self.assertEqual(impl_payload["paths"], ["app.py"])

            # Approve final gate
            self.api("POST", f"runs/{rid}/gates/signoff", {
                "action": "approve", "reason": "补丁核验通过",
            }, expected=mid["revision"])
            _, finished = self.api("GET", f"runs/{rid}")
            self.assertEqual(finished["status"], "completed")

        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])

    def test_agent_job_line_comments_and_targeted_continuation(self):
        prompts = []

        def fake_run_agent(runner, work_dir, prompt, on_event=None, cancelled=None):
            prompts.append(prompt)
            target = Path(work_dir) / "app.py"
            if len(prompts) == 1:
                target.write_text("def greet():\n    return 'draft'\n", encoding="utf-8")
                return {"text": "First draft", "usage": {"input_tokens": 8, "output_tokens": 4}, "session_id": "s1"}
            target.write_text("def greet(name='world'):\n    return f'hello {name}'\n", encoding="utf-8")
            return {"text": "Addressed line comment", "usage": {"input_tokens": 10, "output_tokens": 5}, "session_id": "s2"}

        with patch("clauseweft.agent_runtime.run_agent", side_effect=fake_run_agent):
            status, job1 = self.api("POST", "agent-jobs", {
                "runner_id": "codex",
                "project_id": "default",
                "repo_id": "repo",
                "base_sha": self.base_sha,
                "prompt": "起草 greet 实现",
            })
            self.assertEqual(status, 201)
            self.assertTrue(self.app.agent_jobs.work_once())

            # Untouched path must be rejected
            with self.assertRaises(ValueError):
                self.api("POST", f"agent-jobs/{job1['id']}/comments", {
                    "path": "other.py", "line": 1, "side": "new", "body": "不在此候选内",
                })
            # Secret in comment body must be rejected
            with self.assertRaises(ValueError):
                self.api("POST", f"agent-jobs/{job1['id']}/comments", {
                    "path": "app.py", "line": 2, "side": "new",
                    "body": "泄漏密钥 sk-ant-" + "x" * 28,
                })

            # Valid line comment
            status, c1 = self.api("POST", f"agent-jobs/{job1['id']}/comments", {
                "path": "app.py", "line": 2, "side": "new",
                "body": "请支持可选参数 name 并返回 f'hello {name}'",
            })
            self.assertEqual(status, 201)
            _, detail1 = self.api("GET", f"agent-jobs/{job1['id']}")
            self.assertEqual(len(detail1["comments"]), 1)
            self.assertEqual(detail1["comments"][0]["id"], c1["id"])

            # Targeted continuation carrying selected comment_ids
            status, job2 = self.api("POST", "agent-jobs", {
                "runner_id": "codex",
                "project_id": "default",
                "repo_id": "repo",
                "continue_from": job1["id"],
                "comment_ids": [c1["id"]],
                "prompt": "按评审意见修复",
            })
            self.assertEqual(status, 201)
            self.assertTrue(self.app.agent_jobs.work_once())
            self.assertIn("- app.py:2 (new): 请支持可选参数 name", prompts[1])

            _, detail2 = self.api("GET", f"agent-jobs/{job2['id']}")
            self.assertEqual(detail2["status"], "succeeded")
            patch_text = detail2["patch"]
            self.assertIn("+def greet(name='world'):", patch_text)

            # Verify cumulative patch applies cleanly onto a fresh checkout of base_sha
            verify_dir = self.root / "verify_apply"
            subprocess.run(["git", "clone", "-q", str(self.repo), str(verify_dir)], check=True)
            _git(verify_dir, "checkout", "-q", self.base_sha)
            patch_file = self.root / "cumulative.patch"
            patch_file.write_text(patch_text, encoding="utf-8")
            subprocess.run(["git", "-C", str(verify_dir), "apply", "--check", str(patch_file)], check=True)

    def test_fts5_search_hot_reload_compaction_and_project_memory(self):
        # 1. SQLite FTS5 BM25: title hit ('doc-title') ranks ahead of body-only hit ('doc-body')
        docs = list(self.app.references["default"]["docs"].values())
        hits = project_tools.search(docs, "退款队列")
        self.assertGreaterEqual(len(hits), 2)
        self.assertEqual(hits[0]["id"], "doc-title")
        en_hits = project_tools.search(docs, "upstream timeouts breaker")
        self.assertEqual(en_hits[0]["id"], "doc-en")

        # Hot reload on new run when reference file mtime changes
        old_digest = self.app.references["default"]["digest"]
        time.sleep(0.01)
        self.ref_path.write_text(json.dumps([
            {"id": "doc-hot", "title": "热更新条目", "text": "无需重启即可在新任务检索到本条内容。"},
        ], ensure_ascii=False), encoding="utf-8")
        os.utime(self.ref_path, None)
        _, preview = self.api("POST", "routing/previews", {
            "mode": "tool",
            "project_id": "default",
            "tool_id": "search_reference",
            "task": {"text": "查询热更新条目"},
            "budget": {"currency": "CNY", "limit_micros": 100_000},
        })
        self.assertEqual(preview["status"], "selected")
        self.assertNotEqual(self.app.references["default"]["digest"], old_digest)
        self.assertEqual(project_tools.search(self.app.references["default"]["docs"].values(), "热更新")[0]["id"], "doc-hot")

        # 2. Project-scoped structured long-term memory CRUD + If-Match + cross-project isolation + scope tightening
        status, mem = self.api("POST", "projects/default/memories", {
            "category": "convention",
            "title": "金额单位约定",
            "content": "所有账本金额均以整数微单位（micros）存储，禁止浮点数结算。",
            "data_scope": "local_only",
        })
        self.assertEqual(status, 201)
        self.assertEqual(mem["revision"], 1)

        # Stale If-Match rejected with 412
        with self.assertRaises(Problem) as stale_err:
            self.api("PATCH", f"projects/default/memories/{mem['id']}", {
                "title": "更新标题",
            }, expected=99)
        self.assertEqual(stale_err.exception.status, 412)

        status, mem_v2 = self.api("PATCH", f"projects/default/memories/{mem['id']}", {
            "content": "所有金额一律使用 64 位整数微单位（micros）保存。",
        }, expected=1)
        self.assertEqual(status, 200)
        self.assertEqual(mem_v2["revision"], 2)

        # Cross-project isolation: another project cannot freeze or read `default`'s memory ID
        _, other_proj = self.api("POST", "projects", {
            "workspace_id": "default", "name": "Other", "description": "isolated",
        })
        with self.assertRaises(Problem) as iso_err:
            self.api("GET", f"projects/{other_proj['id']}/memories/{mem['id']}")
        self.assertEqual(iso_err.exception.status, 404)

        # Viewer role cannot create/modify project memory
        with self.app.store.transaction() as db:
            viewer = self.app.platform.add_member(db, {
                "username": "mem_viewer", "display_name": "Viewer", "role": "viewer",
                "password": "viewer-password-1234",
                "workspace_ids": ["default"], "project_ids": ["default"],
            })
        with self.assertRaises(Problem) as viewer_err:
            self.api("POST", "projects/default/memories", {
                "category": "glossary", "title": "T", "content": "C",
            }, principal={"id": viewer["id"]})
        self.assertEqual(viewer_err.exception.status, 403)

        # Using a local_only project memory tightens task data_scope and injects into system prompt
        captured_messages = []

        def fake_gen(endpoint, messages, max_tokens):
            captured_messages.append((endpoint["endpoint_id"], messages))
            return f"答复-{len(captured_messages)}", {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20}, f"r-mem-{uuid4().hex}", "stop", max_tokens

        with patch.object(self.app.catalog, "generate", side_effect=fake_gen):
            # Pinning cloud-vision must block when local_only memory is attached!
            _, blocked_preview = self.api("POST", "routing/previews", {
                "project_id": "default",
                "use_project_memory": True,
                "task": {"text": "解释账本结算规则", "data_scope": "public", "pinned_endpoint": "cloud-vision"},
                "budget": {"currency": "CNY", "limit_micros": 100_000},
            })
            self.assertEqual(blocked_preview["status"], "blocked")

            # Unpinned run routes to local model and receives memory snapshot in system prompt
            status, current = self.api("POST", "runs", {
                "project_id": "default",
                "memory_ids": [mem["id"]],
                "task": {"text": "第 1 轮问题：解释账本结算规则", "data_scope": "public"},
                "budget": {"currency": "CNY", "limit_micros": 500_000},
            })
            self.assertEqual(status, 201)
            self.assertTrue(self.app.runs.work_once())
            self.assertIn(captured_messages[-1][0], ("local-text", "local-vision"))
            sys_msgs = [m["content"] for m in captured_messages[-1][1] if m["role"] == "system"]
            self.assertTrue(any("金额单位约定" in s and "micros" in s for s in sys_msgs))

            # 3. Auditable Conversation Compaction across > MAX_HISTORY_TURNS (10 turns total)
            for idx in range(2, 11):
                _, snap = self.api("GET", f"runs/{current['run_id']}")
                _, current = self.api("POST", f"runs/{current['run_id']}/continuations", {
                    "action": "followup",
                    "text": f"第 {idx} 轮追问：补充说明细节 {idx}",
                    "budget": {"currency": "CNY", "limit_micros": 500_000},
                }, expected=snap["revision"])
                self.assertTrue(self.app.runs.work_once())

            _, tenth = self.api("GET", f"runs/{current['run_id']}")
            self.assertIn("conversation_compaction", tenth)
            comp = tenth["conversation_compaction"]
            self.assertEqual(comp["compacted_turns"], 1)
            self.assertEqual(len(comp["source_run_ids"]), 1)
            self.assertEqual(len(comp["source_answer_refs"]), 1)
            self.assertIn("第 1 轮问题", comp["summary"])
            # Verify compacted summary was injected into the 10th turn's model messages
            tenth_sys = [m["content"] for m in captured_messages[-1][1] if m["role"] == "system"]
            self.assertTrue(any("较早对话摘要" in s and "第 1 轮问题" in s for s in tenth_sys))

        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])

    def test_multimodal_attachments_vision_routing_and_end_to_end_dispatch(self):
        # Reject unsupported MIME or corrupt base64
        for bad_att in (
            [{"name": "doc.pdf", "media_type": "application/pdf", "data_base64": PNG_1X1}],
            [{"name": "bad.png", "media_type": "image/png", "data_base64": "not-valid-base64!!!"}],
        ):
            with self.assertRaises(ValueError):
                self.api("POST", "routing/previews", {
                    "project_id": "default",
                    "task": {"text": "分析截图", "attachments": bad_att},
                    "budget": {"currency": "CNY", "limit_micros": 100_000},
                })

        # Valid attachment automatically adds `vision` capability and filters out `local-text`
        _, preview = self.api("POST", "routing/previews", {
            "project_id": "default",
            "task": {
                "text": "请检查这张 UI 截图的按钮对齐问题",
                "data_scope": "local_only",
                "attachments": [{"name": "ui.png", "media_type": "image/png", "data_base64": PNG_1X1}],
            },
            "budget": {"currency": "CNY", "limit_micros": 200_000},
        })
        self.assertEqual(preview["status"], "selected")
        self.assertEqual(preview["selected_endpoint"], "local-vision")
        by_id = {c["endpoint_id"]: c for c in preview["candidates"]}
        self.assertIn("missing_capability", by_id["local-text"]["reasons"])

        dispatched = []

        def fake_vision(endpoint, messages, max_tokens):
            dispatched.append((endpoint["endpoint_id"], messages))
            return "按钮向右下偏移了 4px。", {"prompt_tokens": 1100, "completion_tokens": 12, "total_tokens": 1112}, "r-vis", "stop", max_tokens

        with patch.object(self.app.catalog, "generate", side_effect=fake_vision):
            status, run = self.api("POST", "runs", {
                "project_id": "default",
                "task": {
                    "text": "请检查这张 UI 截图的按钮对齐问题",
                    "data_scope": "local_only",
                    "attachments": [{"name": "ui.png", "media_type": "image/png", "data_base64": PNG_1X1}],
                },
                "budget": {"currency": "CNY", "limit_micros": 200_000},
            })
            self.assertEqual(status, 201)
            self.assertTrue(self.app.runs.work_once())
            self.assertEqual(dispatched[0][0], "local-vision")
            user_content = dispatched[0][1][-1]["content"]
            self.assertIsInstance(user_content, list)
            self.assertEqual(user_content[0], {"type": "text", "text": "请检查这张 UI 截图的按钮对齐问题"})
            self.assertTrue(user_content[1]["image_url"]["url"].startswith("data:image/png;base64,"))


if __name__ == "__main__":
    unittest.main()
