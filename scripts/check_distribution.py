"""Exercise the same CLI contract on source or a locally built artifact."""

import argparse
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]

# Isolate the import from the shell's working directory and PYTHONPATH.
# An explicit root selects the source checkout or the bytecode archive.
WORKBENCH_CHECK = """
import hashlib
import json
import sys
import tempfile
from pathlib import Path

root = sys.argv[1]
if root:
    sys.path.insert(0, root)
from clauseweft.api import console
from clauseweft.api.application import Application
if root and not Path(console.__file__).is_relative_to(root):
    raise SystemExit("workbench was imported from outside the requested artifact")
if not console.SCRIPT.strip() or not console.STYLE.strip():
    raise SystemExit("empty workbench script or stylesheet")
if console.SCRIPT not in console.PAGE or console.STYLE not in console.PAGE:
    raise SystemExit("workbench HTML is missing its script or stylesheet")
with tempfile.TemporaryDirectory() as state:
    app = Application(state)
    with app.store.transaction() as db:
        manifest = json.loads(db.execute("SELECT after_snapshot FROM deployment_snapshot WHERE id=1").fetchone()[0])["manifest"]
web_assets = {key: value for key, value in manifest.items()
              if key.startswith("clauseweft/api/web/") and key.endswith((".html", ".js", ".css"))}
if "clauseweft/api/web/page.html" not in web_assets:
    raise SystemExit("deployment audit is missing workbench assets")
if root.endswith(".pyz") and any(key.endswith(".py") for key in manifest):
    raise SystemExit("bytecode archive recorded external Python source files")
print(hashlib.sha256(console.PAGE.encode("utf-8")).hexdigest(), json.dumps(web_assets, sort_keys=True))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("format", choices=("source", "installed", "native", "zipapp", "executable"))
    args = parser.parse_args()
    scripts = Path(sysconfig.get_path("scripts"))
    suffix = ".exe" if os.name == "nt" else ""
    commands = {
        "source": [sys.executable, "-m", "clauseweft"],
        "installed": [str(scripts / f"clauseweft{suffix}")],
        "native": [str(ROOT / "dist/native/launch_clauseweft.dist" /
                       ("clauseweft.exe" if os.name == "nt" else "clauseweft"))],
        "zipapp": [sys.executable, str(ROOT / "dist/clauseweft.pyz")],
        "executable": [str(ROOT / "dist" / ("clauseweft.exe" if os.name == "nt" else "clauseweft"))],
    }
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if args.format == "source":
        env["PYTHONPATH"] = str(ROOT / "src")
    with tempfile.TemporaryDirectory() as temp:
        def run(arguments, expected, command=commands[args.format]):
            result = subprocess.run(
                [*command, *arguments], capture_output=True, text=True,
                cwd=temp, env=env,
            )
            if result.returncode != expected:
                raise SystemExit(f"{arguments[0]} returned {result.returncode}: {result.stderr}")
            return result.stdout

        with (ROOT / "pyproject.toml").open("rb") as metadata:
            expected_version = tomllib.load(metadata)["project"]["version"]
        if run(["--version"], 0).strip() != f"clauseweft {expected_version}":
            raise SystemExit("CLI version differs from pyproject.toml")
        runtime = json.loads(run(["doctor"], 0))
        if runtime["runtime_ok"] is not True:
            raise SystemExit("runtime check failed")
        names = [line.split()[0] for line in run(["modules"], 0).splitlines()]
        expected_modules = {p.name for p in (ROOT / "src/clauseweft").iterdir() if (p / "__init__.py").is_file()}
        if set(names) != expected_modules or len(names) != len(expected_modules) or runtime["registered_modules"] != len(names):
            raise SystemExit("incomplete module registry")
        sample = ROOT / "examples/route-demo.json"
        selected = json.loads(run(["route", "--input", str(sample)], 0))
        if selected["selected_endpoint"] != "local-review":
            raise SystemExit("sample routed to an unexpected model")
        blocked = json.loads(sample.read_text(encoding="utf-8"))
        blocked["models"] = []
        local_input = Path(temp) / "input.json"
        local_input.write_text(json.dumps(blocked), encoding="utf-8")
        if json.loads(run(["route", "--input", str(local_input)], 1))["status"] != "blocked":
            raise SystemExit("an empty model pool must block routing")
        run(["route", "--input", str(Path(temp) / "missing.json")], 2)
        if args.format in ("source", "installed", "zipapp"):
            interpreter = [sys.executable, "-I", "-c", WORKBENCH_CHECK]
            source_digest = run([str(ROOT / "src")], 0, interpreter)
            if args.format != "source":
                archive = str(ROOT / "dist/clauseweft.pyz") if args.format == "zipapp" else ""
                if run([archive], 0, interpreter) != source_digest:
                    raise SystemExit("packaged workbench HTML or audited asset hashes differ from source")
        if args.format == "installed":
            package = distribution("clauseweft")
            if package.version != expected_version:
                raise SystemExit("installed package version differs from pyproject.toml")
            config = package.locate_file("clauseweft/gateway/example.json")
            summary = json.loads(run(["--config", str(config), "--check-config"], 0,
                                     [str(scripts / f"clauseweft-gateway{suffix}")]))
            if summary["status"] != "valid" or summary["endpoint_count"] != 1:
                raise SystemExit("installed gateway failed to load its sample configuration")
            print("installed: gateway entry point and bundled example passed")
    print(f"{args.format}: ClauseWeft {expected_version}, doctor, modules and route exit codes 0/1/2 passed")


if __name__ == "__main__":
    main()
