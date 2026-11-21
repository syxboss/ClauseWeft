"""Source of the isolated PID 1 entry point, also available in bytecode packages."""

import json
from pathlib import PurePosixPath
import secrets


def review_gate(directory="/tmp"):
    marker = "clauseweft-review-" + secrets.token_hex(24)
    plugin = (
        "export const inject=['tools']; export function apply(ctx) {"
        "ctx.tools.guard(()=> 'This review uses only the attached snapshot');"
        "console.log(" + json.dumps(marker) + "); }"
    )
    patch = (
        "- insert:\n    - id: clauseweft-review\n      name: "
        + json.dumps(str(PurePosixPath(directory) / "review.mjs"))
        + "\n"
    )
    return {"marker": marker, "plugin": plugin, "patch": patch}


# Passed as Python code, never assembled from task data. stdin carries the
# owner's selected files, the prompt and this provider's key. Docker inspect
# and Docker log drivers do not retain those values.
SCRIPT = r"""
import base64, json, os, signal, stat, subprocess, sys
from pathlib import Path

def send(kind, data):
    print(json.dumps([kind, data], separators=(",", ":")), flush=True)

def files_at(root):
    result = {}
    device = root.stat().st_dev
    size = count = 0
    for path in root.rglob("*"):
        count += 1
        if count > 2048: raise ValueError()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode): continue
        size += info.st_size
        if (not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_nlink != 1
                or size > 2*1024*1024 or len(result) >= 512):
            raise ValueError()
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not os.path.samestat(info, os.fstat(stream.fileno())): raise ValueError()
            raw = stream.read(info.st_size + 1)
        if len(raw) != info.st_size: raise ValueError()
        result[str(path.relative_to(root))] = base64.b64encode(raw).decode("ascii")
    return result

def main():
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.alarm(int(sys.argv[1]))
    data = json.load(sys.stdin)
    work = Path("/work")
    tmp = Path("/tmp")
    home = tmp / "home"
    home.mkdir(mode=0o700)
    env = {"HOME":str(home), "PATH":"/usr/local/bin:/usr/bin:/bin", "LANG":"C.UTF-8", "TMPDIR":str(tmp)}
    argv = data["argv"]
    prompt = data["prompt"]
    claude = data["kind"] == "claude"
    if claude:
        env["ANTHROPIC_API_KEY"] = data["key"]
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        for name, content in data["files"].items():
            path = work / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(base64.b64decode(content, validate=True))
    else:
        env["DEEPSEEK_API_KEY"] = data["key"]
        env["DSH_HOME"] = str(home / ".dsh")
        # Load the image's pinned native addon directly; /tmp is noexec.
        env["NARB_DISABLE_NATIVE_CACHE"] = "1"
        # The read-only reviewer receives the exact source text in the turn.
        # Install a monotonic, global tool denial before delivering any task.
        # An incompatible profile never receives the prompt.
        gate = data["review_gate"]
        marker = gate["marker"]
        (tmp / "review.mjs").write_text(gate["plugin"], encoding="utf-8")
        (tmp / "review.yml").write_text(gate["patch"], encoding="utf-8")
        sources = {name:base64.b64decode(content, validate=True).decode("utf-8") for name,content in data["files"].items()}
        prompt = json.dumps({"review_request": prompt, "source_files":sources}, ensure_ascii=False)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=work, env=env, start_new_session=True)
    if claude:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    total = 0
    admitted = claude
    while True:
        row = proc.stdout.readline(1024*1024 + 1)
        if not row: break
        total += len(row)
        if total > 8*1024*1024 or len(row) > 1024*1024: raise ValueError()
        if not admitted:
            if row.rstrip() == marker.encode():
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
                admitted = True
            continue
        if row.strip():
            send("cli", json.loads(row))
    if proc.wait() != 0 or not admitted: sys.exit(1)
    if claude:
        # Reap outstanding helpers before collecting files. PID 1 exiting
        # also tears down every process in this private PID namespace.
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        send("files", files_at(work))

try:
    main()
except BaseException as error:
    if isinstance(error, SystemExit): raise
    sys.exit(2)
"""
