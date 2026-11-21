"""Build a local executable or a portable Python zipapp from this checkout."""

import argparse
import importlib.util
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tempfile
import zipapp

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"


def build_executable(work):
    if importlib.util.find_spec("PyInstaller") is None:
        raise SystemExit("请在当前虚拟环境中安装构建依赖：python -m pip install '.[executable]'")
    web = Path("clauseweft/api/web")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm",
        "--name", "clauseweft", "--paths", str(SRC),
        "--add-data", f"{SRC / web / 'page.html'}:{web.as_posix()}",
        "--add-data", f"{SRC / web / 'scripts'}:{web.as_posix()}/scripts",
        "--add-data", f"{SRC / web / 'styles'}:{web.as_posix()}/styles",
        "--distpath", str(DIST), "--specpath", str(work), "--workpath", str(work / "build"),
        str(SRC / "clauseweft" / "__main__.py"),
    ], env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(work / "pyinstaller")}, check=True)
    shutil.copyfile(ROOT / "LICENSE", DIST / "LICENSE")
    return DIST / ("clauseweft.exe" if os.name == "nt" else "clauseweft")


def build_zipapp(work):
    stage = work / "compiled"
    for source in (SRC / "clauseweft").rglob("*.py"):
        relative = source.relative_to(SRC)
        target = stage / relative.with_suffix(".pyc")
        target.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(
            str(source), cfile=str(target), dfile=str(relative), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
    web = Path("clauseweft/api/web")
    shutil.copytree(SRC / web, stage / web, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    shutil.copyfile(SRC / "clauseweft" / "__main__.py", stage / "__main__.py")
    shutil.copyfile(ROOT / "LICENSE", stage / "LICENSE")
    artifact = work / "clauseweft.pyz"
    zipapp.create_archive(
        stage, target=artifact, interpreter="/usr/bin/env python3", compressed=True,
    )
    return artifact.replace(DIST / artifact.name)


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("构建需要 Python 3.11 或更高版本")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("format", choices=("executable", "zipapp"))
    args = parser.parse_args()
    DIST.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clauseweft-build-", dir=DIST) as temp:
        build = build_executable if args.format == "executable" else build_zipapp
        print(build(Path(temp)))


if __name__ == "__main__":
    main()
