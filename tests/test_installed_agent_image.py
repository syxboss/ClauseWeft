"""Verify the release CLIs and review handshake inside an offline, read-only Linux image."""
import json
import os
from pathlib import Path
import subprocess
import unittest

from clauseweft.agent_runtime import Runner, _cli, _docker_context
from clauseweft.agent_runtime.container import SCRIPT, review_gate


IMAGE = os.environ.get("CLAUSEWEFT_TEST_AGENT_IMAGE")

# Observe the marker emitted by the real CLI plugin. Stop it before forwarding
# the synthetic review input, so the test never needs a model server.
HANDSHAKE = """
import json, subprocess, sys
marker = sys.argv[1]
child = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
try:
    for row in child.stdout:
        if row.rstrip() == marker.encode():
            break
    else:
        sys.exit(1)
finally:
    child.kill()
    child.wait()
print(marker, flush=True)
request = json.load(sys.stdin)
if request != {"review_request": "offline check", "source_files": {}}:
    sys.exit(2)
print(json.dumps({"type":"final","reason":{"kind":"completed"},"text":"guard ready"}))
"""


@unittest.skipUnless(IMAGE, "set CLAUSEWEFT_TEST_AGENT_IMAGE to the release agent image ID")
class InstalledAgentImage(unittest.TestCase):
    def test_versions_and_real_review_guard_in_production_sandbox(self):
        runner = Runner("deepseek", IMAGE, "/usr/local/bin/dsh", "TEST_DSH_IMAGE_KEY", 45)
        client, env = _docker_context(runner)
        command = [
            *client, "run", "--rm", "--pull=never", "--network=none",
            "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=96", "--cpus=1", "--memory=1g", "--memory-swap=1g",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
            "--workdir=/work", "-i", "--entrypoint=/usr/local/bin/python3", IMAGE, "-I", "-u", "-c",
        ]
        script = """import json, subprocess
print(json.dumps([subprocess.check_output([p, "--version"], text=True).strip()
                  for p in ("/usr/local/bin/claude", "/usr/local/bin/dsh")]))"""
        versions = subprocess.run([*command, script], env=env, capture_output=True, text=True, timeout=60, check=True)
        claude, deepseek = json.loads(versions.stdout)
        manifest = Path(__file__).resolve().parents[1] / "examples/agent-image/package.json"
        pinned = json.loads(manifest.read_text())["dependencies"]
        self.assertIn(pinned["@anthropic-ai/claude-code"], claude)
        self.assertIn(pinned["@deepseek-ai/dsh"], deepseek)

        gate = review_gate()
        packet = {
            "kind": "deepseek", "prompt": "offline check", "key": "fake-image-check", "files": {},
            "review_gate": gate, "argv": ["/usr/local/bin/python3", "-I", "-u", "-c",
                                          HANDSHAKE, gate["marker"], *_cli(runner)],
        }
        check = subprocess.run([*command, SCRIPT, "45"], input=json.dumps(packet), text=True,
                               env=env, capture_output=True, timeout=60, check=True)
        events = [json.loads(line) for line in check.stdout.splitlines()]
        self.assertEqual(events, [["cli", {"type": "final", "reason": {"kind": "completed"}, "text": "guard ready"}]])
