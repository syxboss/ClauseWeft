"""Run a repeatable three-scenario acceptance sample against local Ollama."""

import argparse
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from clauseweft.api.application import Application
from clauseweft.api.server import Problem
from clauseweft.gateway.config import load_config
from clauseweft.gateway.server import create_server


SCENARIOS = ("existing", "created", "rejected")
QUERY = "退款对账"
TITLE = "退款对账"
ACCEPTANCE = "验收时核对退款记录中的订单编号与原订单编号是否一致。"
GOAL = (
    f"先按关键词「{QUERY}」检索本项目需求。若已有同名需求，引用其 ID 结束；"
    f"若未命中，申请新建需求，完整标题必须原样使用「{TITLE}」，优先级为 medium。"
    f"需求描述写明验收条件：{ACCEPTANCE} 完成时说明处理结果，并逐字引用回执中的实际需求 ID。"
)


def api(app, method, path, data=None, revision=None, query=None):
    return app.handle(method, tuple(path.split("/")), query or {}, data or {}, uuid4().hex, revision)[1]


@contextmanager
def local_workspace(model):
    with TemporaryDirectory(prefix="clauseweft-agent-goals-") as directory, ExitStack() as cleanup:
        root = Path(directory)
        token_env = "CLAUSEWEFT_ACCEPTANCE_" + uuid4().hex
        os.environ[token_env] = uuid4().hex
        cleanup.callback(os.environ.pop, token_env)
        gateway_path = root / "gateway.json"
        gateway_path.write_text(json.dumps({
            "max_tokens": 1024, "timeout_seconds": 28, "token_env": token_env,
            "endpoints": [{"id": "acceptance", "model": model, "protocol": "ollama",
                           "base_url": "http://127.0.0.1:11434"}],
        }), encoding="utf-8")
        config = load_config(gateway_path)
        with create_server(config, port=0) as gateway:
            thread = Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            try:
                workbench_path = root / "workbench.json"
                workbench_path.write_text(json.dumps({
                    "gateways": {"local": {"url": f"http://127.0.0.1:{gateway.server_port}", "token_env": token_env}}
                }), encoding="utf-8")
                app = Application(root / "state", workbench_path)
                api(app, "POST", "endpoints", {
                    "endpoint_id": "acceptance", "model_identity": model, "gateway_id": "local",
                    "gateway_etag": f'"{config.revision}"', "location": "local",
                    "enabled": True, "allowed": True, "context_tokens": 8192, "max_output_tokens": 1024,
                    "fit": {}, "capabilities": [], "priority": 1, "currency": "CNY",
                    "input_micros_per_million": 0, "output_micros_per_million": 0,
                })
                ws = api(app, "GET", "workspaces/default")
                api(app, "PATCH", "workspaces/default", {"endpoint_ids": ["acceptance"]}, ws["revision"])
                yield app
            finally:
                gateway.shutdown()
                thread.join()


def approval(scenario, proposal, step):
    args = proposal["arguments"]
    if step == 0 and proposal["id"] == "search_requirements" and args["query"] == QUERY:
        return "approve"
    if step == 1 and scenario in ("created", "rejected") and proposal["id"] == "create_requirement":
        if args["title"] == TITLE and args["priority"] == "medium" and "订单编号" in args["description"]:
            return "approve" if scenario == "created" else "reject"
    return None


def sample(app, scenario, repetition, mode, max_retries, progress):
    project = api(app, "POST", "projects", {"name": f"验收 {repetition} · {scenario}"})
    if scenario == "existing":
        api(app, "POST", "requirements", {"project_id": project["id"], "title": TITLE, "description": ACCEPTANCE})
    request = {
        "mode": "tool", "project_id": project["id"],
        "allowed_tools": ["search_requirements", "create_requirement"], "max_tool_steps": 2,
        "budget": {"currency": "CNY", "limit_micros": 0},
        "task": {"text": GOAL, "pinned_endpoint": "acceptance", "max_output_tokens": 1024},
    }
    if mode == "conditional":
        request["requirement_query"] = QUERY
    run = api(app, "POST", "runs", request)
    path = "runs/" + run["run_id"]
    progress["run_id"] = run["run_id"]
    steps = 0
    stop_reason = "step_limit"
    for _ in range(10):
        app.runs.work_once()
        run = api(app, "GET", path)
        if run["status"] in ("completed", "cancelled"):
            stop_reason = run["status"]
            break
        proposal = run.get("tool_proposal")
        if proposal:
            decision = approval(scenario, proposal, steps)
            if not decision:
                stop_reason = "unexpected_proposal"
                progress["unexpected_proposal"] = {
                    "step": steps + 1, "tool_id": proposal["id"], "arguments": proposal["arguments"],
                }
                break
            run = api(app, "POST", path + "/tool-decisions", {
                "action": decision, "proposal_ref": proposal["proposal_ref"],
                "reason": f"合成场景 {scenario} 的第 {steps + 1} 项审批规则",
            }, run["revision"])
            steps += 1
        elif run["status"] == "needs_input":
            reasons = [n["reason"] for n in run["nodes"] if n["state"] in ("needs_input", "uncertain") and n["reason"]]
            stop_reason = ",".join(reasons) or "needs_input"
            if (progress["retries"] >= max_retries or run["budget"]["uncertain_attempts"]
                    or not set(reasons).intersection({"invalid_agent_decision", "tool_step_limit"})):
                break
            api(app, "POST", path + "/resume", {}, run["revision"])
            progress["retries"] += 1
    result = api(app, "GET", path + "/goal-assessment",
                 query={"scenario": scenario, "query": QUERY, "title": TITLE})
    return {"status": result["status"], "passed": result["status"] == "passed",
            "stop_reason": stop_reason,
            "failed_checks": [name for name, ok in result["checks"].items() if not ok]}


def main():
    parser = argparse.ArgumentParser(description="在临时工作空间验收本机 Ollama 模型的多步需求任务")
    parser.add_argument("--model", required=True, help="已安装的 Ollama 模型名称")
    parser.add_argument("--repeats", type=int, default=1, help="每种场景的采样次数")
    parser.add_argument("--mode", choices=("conditional", "auto"), default="conditional",
                        help="conditional 使用显式查重约束，auto 仅依靠模型选择工具顺序")
    parser.add_argument("--max-retries", type=int, choices=(0, 1), default=0,
                        help="每个样本额外重试格式无效或超出步骤的模型决策次数")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats 至少为 1")
    totals = {s: {"passed": 0, "total": args.repeats, "retries": 0} for s in SCENARIOS}
    try:
        with local_workspace(args.model) as app:
            for repetition in range(1, args.repeats + 1):
                for scenario in SCENARIOS:
                    result = {"retries": 0}
                    try:
                        result.update(sample(app, scenario, repetition, args.mode, args.max_retries, result))
                    except (OSError, RuntimeError, ValueError, KeyError, Problem) as exc:
                        result.update(passed=False, status="error", stop_reason=str(exc))
                    totals[scenario]["passed"] += int(result["passed"])
                    totals[scenario]["retries"] += result["retries"]
                    print(json.dumps({"mode": args.mode, "repetition": repetition, "scenario": scenario, **result},
                                     ensure_ascii=False), flush=True)
    except (OSError, ValueError, RuntimeError, Problem) as exc:
        print(f"临时验收环境无法启动：{exc}", file=sys.stderr)
    overall = {key: sum(row[key] for row in totals.values()) for key in ("passed", "total", "retries")}
    print(json.dumps({"mode": args.mode, "model": args.model, "scenarios": totals, "overall": overall},
                     ensure_ascii=False), flush=True)
    return int(overall["passed"] != overall["total"])


if __name__ == "__main__":
    sys.exit(main())
