"""Materialize bounded text inputs and validate an agent's resulting Git patch."""
import os
from pathlib import Path
import stat
import unicodedata

from clauseweft.workspace import Workspace, _git, portable_path
from clauseweft.workspace.credentials import contains


def visible_path(name):
    return portable_path(name) and all(not part.startswith(".") for part in name.split("/"))


def snapshot(directory, repo, base):
    workspace = Workspace(directory, repo["path"], repo["allowlist"])
    data = workspace.snapshot(base)
    files = {item["path"]: item["text"] for item in data["files"] if visible_path(item["path"])}
    return data["base_sha"], files


def materialize(directory, files):
    directory.mkdir(mode=0o700)
    for name, content in files.items():
        if not visible_path(name):
            raise ValueError("工作副本含不安全路径")
        path = directory / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content.encode())


def remove_review_material(path, material):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(material):
        raise ValueError("只读评审材料已变化")
    with path.open("rb") as stream:
        if stream.read(len(material) + 1) != material:
            raise ValueError("只读评审材料已变化")
    path.unlink()


def collect(directory, allowlist):
    remaining = 2 * 1024 * 1024
    files = {}
    entries = 0
    pending = [directory]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as children:
            for entry in children:
                entries += 1
                path = Path(entry.path)
                name = path.relative_to(directory).as_posix()
                if entries > 2048 or len(name.encode()) > 1024 or not visible_path(name):
                    raise ValueError("工作副本含过多文件或不安全路径")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not any(p == "." or name == p or name.startswith(p + "/") for p in allowlist):
                    raise ValueError("改动超出允许的路径")
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or len(files) >= 512 or info.st_size > remaining):
                    raise ValueError("仅接受有限大小的普通文本文件")
                with path.open("rb") as stream:
                    data = stream.read(remaining + 1)
                if len(data) > remaining or b"\0" in data or contains(data):
                    raise ValueError("文件内容不适合导出")
                files[name] = data.decode("utf-8")
                remaining -= len(data)
    return files


def reviewed_patch(directory, repo, sha, before, after):
    """Build the diff in a trusted Git index; no CLI ever sees this object store."""
    workspace = Workspace(directory, repo["path"], repo["allowlist"])
    sha = workspace._pin(sha)
    paths = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if not paths:
        return "", []
    # A withheld secret, symlink, binary or a hidden directory can already occupy
    # a name outside the materialized view. Never allow an output to replace it.
    original = {}
    leaves = set()
    directories = {}

    def folded(path):
        return unicodedata.normalize("NFC", path).casefold()

    for row in _git(workspace.git, "ls-tree", "-rz", sha).split(b"\0"):
        if row:
            header, raw = row.split(b"\t", 1)
            path = os.fsdecode(raw)
            original[path] = header.split()[0].decode()
            leaves.add(folded(path))
            for parent in Path(path).parents:
                value = str(parent)
                normalized = folded(value)
                if normalized in directories and directories[normalized] != value:
                    directories[normalized] = None
                else:
                    directories[normalized] = value
    with workspace._index(sha) as index:
        for name in paths:
            if name not in before:
                norm = folded(name)
                if norm in leaves or norm in directories:
                    raise ValueError("新增路径与基准文件冲突")
                for parent in Path(name).parents:
                    value = str(parent)
                    norm = folded(value)
                    if norm in leaves or directories.get(norm, value) != value:
                        raise ValueError("父目录与基准路径冲突")
            if name not in after:
                _git(workspace.git, "update-index", "-z", "--index-info", index=index,
                     data=b"0 " + b"0" * len(sha) + b"\t" + name.encode() + b"\0")
            else:
                oid = _git(workspace.git, "hash-object", "-w", "--stdin", data=after[name].encode()).decode().strip()
                mode = original.get(name, "100644")
                _git(workspace.git, "update-index", "--add", "--cacheinfo", mode, oid, name, index=index)
        workspace._changes(sha, index, workspace.scope)
        patch = workspace._diff(sha, index).decode()
    record = workspace.stage_patch(sha, patch)
    return patch, record["paths"]
