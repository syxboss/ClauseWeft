"""Pinned Git objects, private work-package indexes and reproducible patch integration."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import subprocess
import tempfile

from .credentials import contains

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_MAX_PATCH = 1024 * 1024
_MAX_FILES = 512
_MAX_CONTENT = 2 * 1024 * 1024
_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9¹²³])(?:\..*)?", re.I)


def safe_path(path):
    if not isinstance(path, str) or not path or "\\" in path or any(ord(c) < 32 for c in path):
        return False
    parts = path.casefold().split("/")
    denied = {".git", ".hg", ".svn", ".ssh", ".aws", ".config", ".gnupg", ".gitmodules",
              ".gitattributes", ".git-credentials", ".docker", ".kube", ".env", ".netrc", ".npmrc", ".pypirc", "credentials",
              "credentials.json", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
    return all(p not in ("", ".", "..") and p not in denied and not p.startswith(".env.")
               and not p.endswith((".pem", ".key", ".p12", ".pfx", ".keystore", ".env")) for p in parts)


def _scope(paths):
    if not isinstance(paths, (tuple, list)) or not paths or any(p != "." and not safe_path(p) for p in paths):
        raise ValueError("invalid work-package path scope")
    return tuple(paths)


def portable_path(path):
    """Accept bounded Git names that can be materialized on supported host filesystems."""
    if not safe_path(path) or len(path.encode()) > 1024 or ":" in path:
        return False
    parts = path.split("/")
    return len(parts) <= 32 and all(
        len(part.encode()) <= 255 and not part.endswith((" ", ".")) and not _DEVICE.fullmatch(part)
        for part in parts
    )


def _allowed(path, scope):
    return safe_path(path) and any(p == "." or p == path or path.startswith(p + "/") for p in scope)


def _git(directory, *args, data=None, index=None, extra=None, stdout=None):
    env = {"PATH": os.defpath + os.pathsep + os.environ.get("PATH", ""), "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "",
           "GIT_ATTR_NOSYSTEM": "1"}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    env.update(extra or {})
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
                             "-c", "protocol.allow=never", "-C", str(directory), *args],
                            input=data, stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=90)
    if result.returncode:
        raise ValueError("Git " + args[0] + " failed: " + result.stderr[:1200].decode("utf-8", "replace"))
    return result.stdout


class Workspace:
    def __init__(self, root, repo, allowlist):
        self.root = Path(root).resolve()
        self.repo = Path(repo).resolve(strict=True)
        if self.root == self.repo or self.repo in self.root.parents:
            raise ValueError("private object store must be outside source repository")
        self.scope = _scope(allowlist)
        self.git = self.root / "objects.git"
        self.patches = self.root / "patches"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.patches.mkdir(mode=0o700, exist_ok=True)
        if not (self.git / "HEAD").exists():
            format_name = _git(self.repo, "rev-parse", "--show-object-format").decode().strip()
            _git(self.root, "init", "--bare", "--template=", "--object-format=" + format_name, str(self.git))

    def _pin(self, base):
        if not isinstance(base, str) or not base or len(base) > 256 or "\x00" in base:
            raise ValueError("invalid base")
        # The private database can serve an already pinned revision after source refs move.
        if _SHA.fullmatch(base):
            try:
                return _git(self.git, "rev-parse", "--verify", base + "^{commit}").decode().strip()
            except ValueError:
                pass
        sha = _git(self.repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode().strip()
        objects = _git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
        alt = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": objects}
        tree = _git(self.git, "rev-parse", sha + "^{tree}", extra=alt).strip()
        ids = {sha.encode(), tree}
        withheld = self.root / "withheld"
        withheld.mkdir(mode=0o700, exist_ok=True)
        count = size = 0
        for line in _git(self.git, "ls-tree", "-rtzl", tree, extra=alt).split(b"\0"):
            if not line:
                continue
            metadata, name = line.split(b"\t", 1)
            mode, kind, oid, length = metadata.split()
            if kind == b"tree":
                ids.add(oid)
                continue
            path = name.decode("utf-8", "replace")
            if name != path.encode() or mode not in (b"100644", b"100755") or not _allowed(path, self.scope):
                continue
            count += 1
            size += int(length)
            if size > _MAX_CONTENT or count > _MAX_FILES:
                raise ValueError("selected files exceed snapshot limits")
            raw = _git(self.git, "cat-file", "blob", oid, extra=alt)
            if contains(raw):
                (withheld / oid.decode()).touch()
            else:
                ids.add(oid)
        # Preserve the complete tree identity without copying private or out-of-scope file contents.
        with tempfile.TemporaryFile(dir=self.root) as pack:
            _git(self.git, "pack-objects", "--stdout", data=b"\n".join(sorted(ids)) + b"\n", extra=alt, stdout=pack)
            pack.seek(0)
            result = subprocess.run(["git", "-C", str(self.git), "index-pack", "--stdin"], stdin=pack,
                                    capture_output=True, timeout=90,
                                    env={"PATH": os.defpath + ":" + os.environ.get("PATH", ""),
                                         "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
            if result.returncode:
                raise ValueError("failed to store pinned Git objects")
        return sha

    def files(self, revision):
        sha = self._pin(revision)
        files = {}
        size = 0
        for row in _git(self.git, "ls-tree", "-rz", sha).split(b"\0"):
            if not row:
                continue
            entry, name = row.split(b"\t", 1)
            mode, kind, oid = entry.split()
            path = name.decode("utf-8", "replace")
            if name != path.encode() or mode not in (b"100644", b"100755") or not _allowed(path, self.scope):
                continue
            if (self.root / "withheld" / oid.decode()).is_file():
                continue
            if int(_git(self.git, "cat-file", "-s", oid)) > _MAX_CONTENT:
                raise ValueError("selected file exceeds snapshot size limit: " + path)
            raw = _git(self.git, "cat-file", "blob", oid)
            if contains(raw):
                continue
            size += len(raw)
            if size > _MAX_CONTENT or len(files) >= _MAX_FILES:
                raise ValueError("selected files exceed snapshot limits")
            files[path] = raw
        return files

    def snapshot(self, base):
        sha = self._pin(base)
        files = []
        for path, raw in self.files(sha).items():
            try:
                content = raw.decode()
            except UnicodeDecodeError:
                continue
            if "\0" not in content:
                files.append({"path": path, "text": content, "sha256": hashlib.sha256(raw).hexdigest()})
        return {"base_sha": sha, "files": files}

    @contextmanager
    def _index(self, sha):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            index = Path(directory) / "index"
            _git(self.git, "read-tree", sha, index=index)
            yield index

    def _diff(self, sha, index):
        return _git(self.git, "diff", "--cached", "--full-index", "--binary", "--no-renames",
                    "--no-ext-diff", "--no-textconv", sha, "--", index=index)

    def _changes(self, sha, index, scope):
        raw = _git(self.git, "diff", "--cached", "--raw", "--no-renames", "-z", sha, "--", index=index)
        items = raw.split(b"\0")[:-1]
        paths = []
        for entry, name in zip(items[::2], items[1::2]):
            path = name.decode()
            before, after = entry.lstrip(b":").split()[:2]
            if (not _allowed(path, self.scope) or not _allowed(path, scope) or
                    {before, after} - {b"000000", b"100644", b"100755"}):
                raise ValueError("patch outside writable regular files: " + path)
            paths.append(path)
        if not paths:
            raise ValueError("empty patch")
        return paths

    def stage_patch(self, base, patch, paths=None):
        sha = self._pin(base)
        scope = self.scope if paths is None else _scope(paths)
        if not isinstance(patch, str) or len(patch.encode()) > _MAX_PATCH or "\0" in patch or "GIT binary patch" in patch:
            raise ValueError("expected a bounded text patch")
        raw = patch.encode()
        stats = _git(self.git, "apply", "--numstat", "-z", "-", data=raw)
        for row in stats.rstrip(b"\0").split(b"\0"):
            if not row:
                continue
            name = row.split(b"\t", 2)[-1].decode()
            if not _allowed(name, self.scope) or not _allowed(name, scope):
                raise ValueError("patch outside writable regular files: " + name)
        with self._index(sha) as index:
            _git(self.git, "apply", "--cached", "--whitespace=nowarn", "-", data=raw, index=index)
            changed = self._changes(sha, index, scope)
            diff = self._diff(sha, index)
            if (len(diff) > _MAX_PATCH or b"GIT binary patch" in diff or
                    any(contains(line[1:]) for line in diff.splitlines() if line[:1] in (b"+", b"-", b" "))):
                raise ValueError("patch contains restricted content or exceeds size limit")
        record = {"base_sha": sha, "paths": changed, "patch": diff.decode()}
        raw = json.dumps(record, sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.NamedTemporaryFile(dir=self.patches, delete=False) as staged:
            staged.write(raw)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged.name, self.patches / digest)
        return {"patch_id": digest, "base_sha": sha, "paths": changed}

    def integrate(self, base, patch_ids):
        sha = self._pin(base)
        if not isinstance(patch_ids, (list, tuple)) or not patch_ids or any(
                not isinstance(p, str) or not re.fullmatch(r"[0-9a-f]{64}", p) for p in patch_ids
        ) or len(set(patch_ids)) != len(patch_ids):
            raise ValueError("expected distinct staged patch IDs")
        with self._index(sha) as index:
            for patch_id in patch_ids:
                file = self.patches / patch_id
                if not file.is_file():
                    raise ValueError("unknown staged patch")
                raw = file.read_bytes()
                if hashlib.sha256(raw).hexdigest() != patch_id:
                    raise ValueError("patch digest mismatch")
                record = json.loads(raw)
                if record["base_sha"] != sha:
                    raise ValueError("patch base mismatch")
                try:
                    _git(self.git, "apply", "--cached", "--3way", "--whitespace=nowarn", "-",
                         index=index, data=record["patch"].encode())
                except ValueError as exc:
                    raise ValueError(f"integration conflict at {patch_id} ({', '.join(record['paths'])}): {exc}") from exc
            changed = self._changes(sha, index, self.scope)
            tree = _git(self.git, "write-tree", "--missing-ok", index=index).decode().strip()
            diff = self._diff(sha, index)
        with self._index(sha) as replay:
            _git(self.git, "apply", "--cached", "-", data=diff, index=replay)
            if _git(self.git, "write-tree", "--missing-ok", index=replay).decode().strip() != tree:
                raise ValueError("export did not reproduce candidate tree")
        env = {f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER")
               for key, value in (("NAME", "ClauseWeft"), ("EMAIL", "clauseweft@localhost"),
                                  ("DATE", "2000-01-01T00:00:00Z"))}
        candidate = _git(self.git, "commit-tree", tree, "-p", sha, extra=env,
                         data=("\n".join(patch_ids) + "\n").encode()).decode().strip()
        _git(self.git, "update-ref", "refs/clauseweft/" + candidate, candidate)
        return {"base_sha": sha, "candidate_sha": candidate, "tree_hash": tree, "patch": diff.decode(),
                "patch_ids": list(patch_ids), "paths": changed}
