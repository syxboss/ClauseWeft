"""Command-line entry points shared by source, executable and Docker runs."""

import argparse
import json
import platform
import sys

from . import __version__
from .routing.preview import main_from_file


# Fixed architectural boundaries. Listing a module does not imply its business
# workflow is available in this build.
MODULES = {
    "gateway": "独立 HTTP 模型调用网关",
    "catalog": "模型目录与能力配置",
    "routing": "任务分类、候选筛选与模型排序",
    "workflow": "阶段依赖、并行编排与恢复",
    "requirements": "需求版本与验收条件",
    "reviews": "评审意见、裁决与审批",
    "workspace": "仓库工作区与变更产物",
    "testing": "测试执行与证据",
    "runs": "运行记录与成本",
    "evaluation": "路由效果评测",
    "storage": "持久化与存储迁移",
    "api": "个人工作台、账号与 HTTP 接口",
    "agent_runtime": "Claude Code 与 DeepSeek Harness 隔离执行器",
    "project_tools": "项目资料、需求和经审批的工具操作",
    "harness": "技能、请求上下文与检查点",
    "audit": "审计记录、证据导出和离线核验",
    "usage": "模型用量与费用汇总",
}


def doctor():
    """Report the local runtime; no filesystem, provider or credential checks."""
    supported = sys.version_info >= (3, 11)
    if "__compiled__" in globals():
        distribution = "nuitka"
    elif getattr(sys, "frozen", False):
        distribution = "frozen"
    else:
        distribution = "python"
    print(json.dumps({
        "runtime_ok": supported,
        "python": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "distribution": distribution,
        "registered_modules": len(MODULES),
    }, indent=2))
    return 0 if supported else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clauseweft", description="ClauseWeft · 个人 AI 研发工作台")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}", help="显示 ClauseWeft 版本")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查本地 Python 运行环境")
    commands.add_parser("modules", help="列出功能模块")
    route = commands.add_parser("route", help="按离线任务快照预览路由")
    route.add_argument("--input", required=True, metavar="FILE", help="UTF-8 JSON 快照")
    audit = commands.add_parser("audit", help="离线校验导出的审计链与证据视图")
    audit.add_argument("--input", required=True, metavar="FILE", help="审计导出 JSON")
    serve = commands.add_parser("serve", help="启动本机工作台")
    serve.add_argument("--state", required=True, metavar="DIR", help="单个可信工作区的持久状态目录")
    serve.add_argument("--config", metavar="FILE", help="可信网关、仓库和测试环境的本机 JSON 配置")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if args.command == "audit":
        from .audit.__main__ import verify_file
        return verify_file(args.input)
    if args.command == "serve":
        from .api.server import main as serve_main
        return serve_main(args)
    if args.command == "doctor":
        return doctor()
    if args.command == "modules":
        for name, description in MODULES.items():
            print(f"{name:<15}{description}")
        return 0
    return main_from_file(args.input)
