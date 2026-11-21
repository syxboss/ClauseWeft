"""Run frozen black-box acceptance cases with a host-side oracle and isolated Docker processes."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import subprocess
import time
import uuid
from datetime import datetime, timezone

from clauseweft.workspace import safe_path
from clauseweft.workspace.credentials import redact

ADAPTER = "docker-stdio-v1"
_OUTPUT_LIMIT = 65536
# PID 1 enforces a second deadline if the controller exits unexpectedly.
# The expected output and the test verdict never enter the container.
_BOOTSTRAP = """
import base64,json,os,sys,tempfile,subprocess
data=json.load(sys.stdin)
os.chdir('/work')
for path,encoded in data['files'].items():
    os.makedirs(os.path.dirname(path) or '.',exist_ok=True)
    with open(path,'xb') as f: f.write(base64.b64decode(encoded,validate=True))
with tempfile.TemporaryFile(dir='/work') as stream:
    stream.write(data['stdin'].encode())
    stream.seek(0)
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/work','LANG':'C.UTF-8'}
    try:
        child=subprocess.Popen(data['argv'],stdin=stream,env=env,start_new_session=True)
        code=child.wait(timeout=data['seconds'])
    except subprocess.TimeoutExpired:
        os._exit(124)
    except OSError as exc:
        print(exc,file=sys.stderr)
        sys.exit(127)
sys.exit(code if code >= 0 else 128-code)
"""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 160


def _bound(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid " + name)
    return value


def freeze_suite(spec_version_id, config):
    if not _identifier(spec_version_id) or not isinstance(config, dict):
        raise ValueError("suite must bind a spec version and configuration")
    image = config.get("image")
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("image must be an immutable local Docker image ID")
    timeout = _bound(config.get("timeout_seconds", 30), 1, 600, "total timeout")
    commands = config.get("commands")
    if not isinstance(commands, dict) or not commands:
        raise ValueError("expected registered commands")
    for name, argv in commands.items():
        if (not _identifier(name) or not isinstance(argv, list) or not argv or len(argv) > 64 or
                any(not isinstance(a, str) or "\0" in a or len(a) > 4096 for a in argv) or not argv[0]):
            raise ValueError("invalid command argv")
    cases = config.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 128:
        raise ValueError("empty or oversized required test manifest")
    ids = set()
    normalized = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("invalid required case")
        case_id, clause, command_id = (case.get(k) for k in ("id", "clause_id", "command_id"))
        if (not _identifier(case_id) or case_id in ids or not _identifier(clause)
                or not _identifier(command_id) or command_id not in commands):
            raise ValueError("duplicate case or missing clause/command reference")
        stdin, stdout = case.get("stdin", ""), case.get("stdout")
        if any(not isinstance(v, str) or len(v.encode()) > _OUTPUT_LIMIT for v in (stdin, stdout)):
            raise ValueError("case requires bounded stdin and expected stdout")
        required = case.get("required_files", [])
        if not isinstance(required, list) or not all(safe_path(p) for p in required):
            raise ValueError("invalid case file references")
        ids.add(case_id)
        normalized.append(dict(id=case_id, clause_id=clause, command_id=command_id, stdin=stdin,
                               stdout=stdout, required_files=list(required)))
    env = dict(adapter=ADAPTER, image=image, timeout_seconds=timeout,
               commands={key: list(argv) for key, argv in commands.items()})
    suite = dict(spec_version_id=spec_version_id, cases=normalized, **env, env_digest=_digest(env))
    return dict(**suite, suite_digest=_digest(suite))


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


class Incomplete(Exception):
    pass


def _command(argv, payload, deadline, name):
    """Bound aggregate output while streaming input. Reap the named container on every path."""
    stdout, stderr = bytearray(), bytearray()
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, event in ((proc.stdin, selectors.EVENT_WRITE), (proc.stdout, selectors.EVENT_READ),
                                (proc.stderr, selectors.EVENT_READ)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, event)
            sent = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Incomplete("timeout")
                for key, event in selector.select(remaining):
                    pipe = key.fileobj
                    if event == selectors.EVENT_WRITE:
                        try:
                            sent += os.write(pipe.fileno(), payload[sent:sent + 32768])
                        except BrokenPipeError:
                            sent = len(payload)
                        if sent >= len(payload):
                            selector.unregister(pipe)
                            pipe.close()
                    else:
                        data = os.read(pipe.fileno(), 8192)
                        if not data:
                            selector.unregister(pipe)
                            pipe.close()
                            continue
                        if len(stdout) + len(stderr) + len(data) > _OUTPUT_LIMIT:
                            raise Incomplete("output_limit")
                        (stdout if pipe == proc.stdout else stderr).extend(data)
        code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        return code, bytes(stdout), bytes(stderr)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()
        # The CLI can die before the daemon receives its signal. Remove the container by unique name.
        removed = subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=10)
        if removed.returncode and b"No such container" not in removed.stderr:
            raise Incomplete("container_cleanup_failed")


def _log(raw):
    return redact(raw.decode("utf-8", "replace"))[:8192]


def _case(case, suite, files, encoded, deadline):
    args = suite["commands"][case["command_id"]]
    record = dict(id=case["id"], clause_id=case["clause_id"], command_id=case["command_id"], argv=args,
                  started_at=_timestamp(), status="incomplete", exit_code=None, log="")
    missing = [path for path in case["required_files"] if path not in files]
    if missing:
        record.update(status="missing", reason="required_files", missing=missing)
    elif deadline <= time.monotonic():
        record["reason"] = "timeout"
    else:
        name = "clauseweft-test-" + uuid.uuid4().hex
        argv = ["docker", "run", "--name", name, "--rm", "--pull=never", "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
                "--ipc=none", "--cpus=1", "--memory=128m", "--memory-swap=128m", "--pids-limit=32",
                "--ulimit", "core=0", "--ulimit", "nofile=128:128", "--log-driver=none",
                "--tmpfs", "/work:rw,nosuid,nodev,noexec,size=32m,mode=1777", "--workdir=/work",
                "--entrypoint=/usr/local/bin/python", "-i", suite["image"], "-I", "-S", "-c", _BOOTSTRAP]
        payload = json.dumps(dict(argv=args, stdin=case["stdin"], files=encoded, seconds=max(0.01,deadline-time.monotonic()))).encode()
        try:
            code, stdout, stderr = _command(argv, payload, deadline, name)
            status = "passed" if code == 0 and stdout == case["stdout"].encode() else "failed"
            if code == 124:
                record["reason"] = "timeout"
            if code in (124, 125, 126, 127, 137):
                status = "incomplete"
            record.update(status=status, exit_code=code, log=_log(stderr + b"\n" + stdout),
                          stdout_digest=hashlib.sha256(stdout).hexdigest())
        except (OSError, subprocess.TimeoutExpired, Incomplete) as exc:
            record["reason"] = str(exc) if isinstance(exc, Incomplete) else "runtime_interrupted"
    return dict(**record, ended_at=_timestamp())


def start(candidate, suite, files):
    """Application callers supply files read from the immutable candidate, never a user-supplied report."""
    if suite != freeze_suite(suite.get("spec_version_id"), suite):
        raise ValueError("modified suite or environment")
    if not isinstance(files, dict) or len(files) > 512 or any(
            not safe_path(path) or not isinstance(raw, bytes) for path, raw in files.items()
    ) or sum(map(len, files.values())) > 2 * 1024 * 1024:
        raise ValueError("invalid candidate files")
    started = time.monotonic()
    report = dict(adapter=ADAPTER, execution_id=uuid.uuid4().hex, candidate_sha=candidate["candidate_sha"],
                  tree_hash=candidate["tree_hash"], spec_version_id=suite["spec_version_id"],
                  suite_digest=suite["suite_digest"], env_digest=suite["env_digest"], image=suite["image"],
                  started_at=_timestamp(), status="incomplete", cases=[])
    try:
        probe = subprocess.run(["docker", "image", "inspect", "--format",
                                "{{.Id}} {{json .Config.Volumes}}", suite["image"]],
                               capture_output=True, timeout=min(5, suite["timeout_seconds"]))
        if probe.returncode or probe.stdout.decode().strip() not in (
                suite["image"] + " null", suite["image"] + " {}"):
            raise Incomplete("pinned_image_unavailable_or_declares_volumes")
    except (OSError, subprocess.TimeoutExpired, Incomplete):
        report.update(status="needs_configuration", reason="pinned_image_or_runtime_unavailable")
    else:
        encoded = {path: base64.b64encode(raw).decode("ascii") for path, raw in files.items()}
        for case in suite["cases"]:
            record = _case(case, suite, files, encoded, started + suite["timeout_seconds"])
            report["cases"].append(record)
        states = {case["status"] for case in report["cases"]}
        report["status"] = next((s for s in ("incomplete", "failed", "missing") if s in states), "passed")
    counts = {status: sum(row["status"] == status for row in report["cases"])
              for status in ("passed", "failed", "missing", "incomplete")}
    counts["unreported"] = len(suite["cases"]) - len(report["cases"])
    return dict(**report, counts=counts, ended_at=_timestamp(), elapsed_ms=round((time.monotonic() - started) * 1000))


def verify(report, candidate, suite):
    """Recheck stored, executor-owned evidence against the current approval inputs."""
    if not isinstance(report, dict) or not isinstance(candidate, dict) or not isinstance(suite, dict):
        return False
    try:
        if suite != freeze_suite(suite.get("spec_version_id"), suite):
            return False
    except ValueError:
        return False
    if report.get("status") != "passed" or report.get("adapter") != ADAPTER or not report.get("execution_id"):
        return False
    if any(report.get(k) != suite[k] for k in ("spec_version_id", "env_digest", "suite_digest", "image")):
        return False
    if any(not candidate.get(k) or candidate[k] != report.get(k) for k in ("candidate_sha", "tree_hash")):
        return False
    expected = {case["id"]: case for case in suite["cases"]}
    rows = report.get("cases")
    if not isinstance(rows, list) or len(rows) != len(expected):
        return False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            return False
        case = expected.pop(row["id"], None)
        if not case or type(row.get("exit_code")) is not int or row["exit_code"] != 0 or row.get("status") != "passed":
            return False
        if (row.get("command_id") != case["command_id"] or row.get("clause_id") != case["clause_id"] or
                row.get("argv") != suite["commands"][case["command_id"]] or
                row.get("stdout_digest") != hashlib.sha256(case["stdout"].encode()).hexdigest()):
            return False
    return not expected
