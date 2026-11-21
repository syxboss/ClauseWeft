"""Compile the ClauseWeft CLI with Nuitka on the target operating system."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "dist" / "native"
    output.mkdir(parents=True, exist_ok=True)
    entry = output / "launch_clauseweft.py"
    entry.write_bytes((root / "src/clauseweft/__main__.py").read_bytes())
    filename = "clauseweft.exe" if os.name == "nt" else "clauseweft"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env.setdefault("NUITKA_CACHE_DIR", str(output / "cache"))
    subprocess.run([
        sys.executable, "-m", "nuitka", "--mode=standalone", "--include-package=clauseweft",
        "--include-package-data=clauseweft.api.web",
        "--jobs=4", f"--include-data-files={root / 'LICENSE'}=LICENSE", f"--output-dir={output}", f"--output-filename={filename}", str(entry),
    ], env=env, cwd=root, check=True)
    print(output / "launch_clauseweft.dist" / filename)


if __name__ == "__main__":
    main()
