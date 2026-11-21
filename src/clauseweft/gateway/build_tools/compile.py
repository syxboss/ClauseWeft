"""Build only the gateway and its Python runtime for this OS and architecture."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/gateway-native"))
    output = parser.parse_args().output.resolve()
    package = Path(__file__).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=True)
    launcher = output / "launch_gateway.py"
    launcher.write_text("from gateway.server import main\nraise SystemExit(main())\n", encoding="utf-8")
    binary = "clauseweft-gateway.exe" if os.name == "nt" else "clauseweft-gateway"
    env = {**os.environ, "PYTHONPATH": str(package.parent)}
    env.setdefault("NUITKA_CACHE_DIR", str(output / "cache"))
    subprocess.run([sys.executable, "-m", "nuitka", "--mode=standalone", "--jobs=4",
                    f"--include-data-files={package / 'LICENSE'}=LICENSE",
                    f"--output-dir={output}", f"--output-filename={binary}", str(launcher)],
                   cwd=output, env=env, check=True)
    print(output / "launch_gateway.dist" / binary)


if __name__ == "__main__":
    main()
