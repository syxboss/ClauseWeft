"""Inspect the Git release inputs and built Python archives without extracting them."""
import argparse
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import unicodedata
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
PRIVATE = {
    ".git", ".aws", ".ssh", ".gnupg", ".kube", ".docker", ".netrc", "_netrc", ".pypirc",
    ".npmrc", ".git-credentials", ".claude", ".codex", ".venv", "__pycache__", ".ds_store",
    ".env", "console-token", "gateway-token", "controller.lock", "workspace.sqlite3",
    "node_modules", "objects.git", "credentials", "credentials.json", "secrets.json", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
}
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/=]{20,}"
    rb"|(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|glpat-)[A-Za-z0-9_-]{24,}"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    rb"|\b(?:ANTHROPIC_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY|CLAUSEWEFT_(?:CONSOLE|GATEWAY)_TOKEN)"
    rb"""["']?\s*[:=]\s*["']?[A-Za-z0-9_+/=-]{32,}"""
    rb"|\bBearer [A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"
)
PACKAGE_FILES = {"clauseweft/agent_runtime/__init__.py", "clauseweft/api/agent_jobs.py",
                 "clauseweft/api/web/scripts/agent-jobs.js", "clauseweft/api/web/page.html",
                 "clauseweft/api/personal_pairing.py", "clauseweft/gateway/example.json"}
SOURCE_FILES = {"LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", "pyproject.toml",
                "docs/12-个人工作台与外部Agent.md", "examples/agent-workbench.json",
                "examples/agent-image/Dockerfile", "examples/agent-image/.dockerignore",
                "examples/agent-image/package.json", "examples/agent-image/package-lock.json",
                "scripts/check_release.py"} | {"src/" + member for member in PACKAGE_FILES}


def verify_path(name):
    if (not name or "\\" in name or ":" in name or any(ord(c) < 32 for c in name)
            or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in name.split("/"))):
        raise ValueError("unsafe archive or checkout path")
    parts = name.casefold().split("/")
    if len(parts) >= 2 and parts[-2] in {"objects", "patches", "withheld"} and re.fullmatch("[0-9a-f]{40,64}", parts[-1]):
        raise ValueError(f"private runtime artifact: {name!r}")
    for part in parts:
        if (part in PRIVATE or (part.startswith(("workspace.sqlite3-", ".venv-", ".env."))
                and part != ".env.example") or part.endswith((".key", ".pem", ".p12", ".pfx", ".sqlite",
                    ".sqlite3", ".sqlite-wal", ".sqlite-shm", ".log", ".pid", ".pyc"))):
            raise ValueError(f"private state or credential path: {name!r}")


def check_contents(stream, size, name):
    if size < 0 or size > MAX_FILE:
        raise ValueError(f"oversized member: {name!r}")
    data = stream.read(size + 1)
    if len(data) != size:
        raise ValueError(f"member size mismatch: {name!r}")
    if data.startswith(b"SQLite format 3\x00"):
        raise ValueError(f"private database in {name!r}")
    if SECRET.search(data):
        raise ValueError(f"possible credential in {name!r} (value withheld)")


def verify_checkout():
    paths = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                                    cwd=ROOT).split(b"\0")
    count = 0
    for raw in set(paths) - {b""}:
        name = raw.decode("utf-8")
        verify_path(name)
        file = ROOT / name
        if any(p.is_symlink() for p in (file, *file.parents) if p.is_relative_to(ROOT)):
            raise ValueError(f"symlink in release input: {name!r}")
        if not file.exists():  # tracked file removed in the working tree
            continue
        if not file.is_file():
            raise ValueError(f"non-regular release input: {name!r}")
        with file.open("rb") as stream:
            check_contents(stream, file.stat().st_size, name)
        count += 1
    return count


def verify_archive(path, version):
    if not path.is_file() or path.is_symlink():
        raise ValueError("expected a regular distribution file")
    seen = set()
    files = set()
    total = 0

    def member(name, size):
        nonlocal total
        verify_path(name)
        normalized = unicodedata.normalize("NFC", name).casefold()
        if normalized in seen:
            raise ValueError(f"duplicate archive member: {name!r}")
        seen.add(normalized)
        total += size
        if len(seen) > 10000 or total > MAX_TOTAL or size > MAX_FILE:
            raise ValueError("archive exceeds release size limits")

    package = f"clauseweft-{version}"
    if path.name == package + ".tar.gz":
        with tarfile.open(path, "r|gz") as archive:
            for entry in archive:
                member(entry.name, entry.size)
                if entry.name != package and not entry.name.startswith(package + "/"):
                    raise ValueError("unexpected sdist top-level directory")
                if entry.isdir():
                    continue
                if not entry.isreg() or entry.issparse():
                    raise ValueError(f"link or special member: {entry.name!r}")
                with archive.extractfile(entry) as data:
                    check_contents(data, entry.size, entry.name)
                files.add(entry.name.removeprefix(package + "/"))
        required = SOURCE_FILES
    elif path.name == package + "-py3-none-any.whl":
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                name = entry.orig_filename.removesuffix("/") if entry.is_dir() else entry.orig_filename
                member(name, entry.file_size)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG) or entry.flag_bits & 1:
                    raise ValueError(f"link, encrypted or special member: {name!r}")
                if entry.is_dir():
                    continue
                with archive.open(entry) as data:
                    check_contents(data, entry.file_size, name)
                files.add(name)
        required = PACKAGE_FILES | {f"{package}.dist-info/METADATA", f"{package}.dist-info/licenses/LICENSE"}
    else:
        raise ValueError("unexpected package name, version or distribution type")
    missing = required - files
    if missing:
        raise ValueError("missing release assets: " + ", ".join(sorted(missing)))
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", action="store_true", help="scan tracked and non-ignored checkout files")
    parser.add_argument("artifacts", nargs="*", type=Path)
    args = parser.parse_args()
    if not args.checkout and not args.artifacts:
        parser.error("specify --checkout and/or archive paths")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    try:
        if args.checkout:
            print(f"checkout: {verify_checkout()} release inputs checked")
        for path in args.artifacts:
            print(f"{path.name}: {verify_archive(path, version)} files checked")
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
