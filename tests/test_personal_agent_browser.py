"""Exercise personal pairing, editing and candidate review through the shipped UI."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from clauseweft.api.application import Application
from clauseweft.api.server import Handler


# Only the vendor programs are replaced. Both fixtures execute inside real
# containers with the production bootstrap, permissions and snapshot transfer.
EDITOR = r"""
import json, sys
from pathlib import Path
assert sys.stdin.read() == "Make a greeting"
source = Path("hello.txt")
assert source.read_text() == "before\n"
source.write_text("after\n")
print(json.dumps({"type":"assistant", "message":{"content":[{"type":"tool_use","name":"Edit"}]}}))
print(json.dumps({"type":"result", "subtype":"success", "is_error":False,
                  "result":"Patch ready", "usage":{"input_tokens":10}, "session_id":"fixture-claude"}))
"""
REVIEWER = r"""
import json, re, sys
from pathlib import Path
guard = Path("/tmp/review.mjs").read_text()
assert "ctx.tools.guard(" in guard
print(re.search(r"clauseweft-review-[0-9a-f]+", guard).group(), flush=True)
files = json.load(sys.stdin)["source_files"]
assert files["hello.txt"] == "after\n"
assert "+after" in files[".clauseweft-review.patch"]
print(json.dumps({"type":"session", "sessionId":"fixture-dsh"}))
print(json.dumps({"type":"final", "reason":{"kind":"completed"}, "text":"Reviewed the saved candidate"}))
"""


def fixture_cli(runner):
    scripts = {"claude": EDITOR, "deepseek": REVIEWER}
    return ["/usr/local/bin/python3", "-I", "-u", "-c", scripts[runner.kind]]


def git(repo, *args):
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Browser Test",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            f"core.hooksPath={os.devnull}",
            *args,
        ],
        check=True,
        capture_output=True,
        timeout=15,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )


@contextmanager
def personal_instance(root, repo):
    runners = {
        kind: {
            "kind": kind,
            "image": os.environ["CLAUSEWEFT_TEST_IMAGE"],
            "executable": "/opt/bin/browser-test",
            "token_env": "BROWSER_TEST_AGENT_KEY",
        }
        for kind in ("claude", "deepseek")
    }
    config = {
        "data_scope": "public",
        "agent_runners": runners,
        "repositories": {
            "demo": {
                "path": str(repo),
                "allowlist": ["hello.txt"],
                "data_scope": "public",
            }
        },
    }
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    token = secrets.token_urlsafe(32)
    with patch.dict(
        os.environ, {"BROWSER_TEST_AGENT_KEY": "offline-browser-fixture"}
    ), patch("clauseweft.agent_runtime._cli", fixture_cli), ThreadingHTTPServer(
        ("127.0.0.1", 0), Handler
    ) as server:
        server.authorization = ("Bearer " + token).encode()
        server.app = Application(root / "state", config_path)
        stop = threading.Event()
        worker = threading.Thread(target=server.app.work, args=(stop,), daemon=True)
        service = threading.Thread(target=server.serve_forever, daemon=True)
        service.start()
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/", token
        finally:
            stop.set()
            server.shutdown()
            service.join(timeout=3)
            worker.join(timeout=15)
            if worker.is_alive() or service.is_alive():
                raise RuntimeError("Browser fixture server did not stop")


@unittest.skipUnless(
    os.environ.get("CLAUSEWEFT_TEST_BROWSER") in {"chrome", "chromium"}
    and os.environ.get("CLAUSEWEFT_TEST_IMAGE"),
    "set CLAUSEWEFT_TEST_BROWSER to chrome or chromium and supply CLAUSEWEFT_TEST_IMAGE",
)
class PersonalAgentBrowserTest(unittest.TestCase):
    def test_editor_download_pinned_review_refresh_and_mobile_home(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "project"
            repo.mkdir()
            source = repo / "hello.txt"
            source.write_text("before\n", encoding="utf-8")
            git(repo, "init", "--template=")
            git(repo, "add", "hello.txt")
            git(repo, "commit", "-m", "Initial fixture")

            with personal_instance(root, repo) as (
                url,
                token,
            ), sync_playwright() as driver:
                browser = driver.chromium.launch(channel=os.environ["CLAUSEWEFT_TEST_BROWSER"], headless=True)
                try:
                    page = browser.new_page(
                        viewport={"width": 1200, "height": 800}, color_scheme="dark"
                    )
                    browser_errors = []
                    page.on(
                        "pageerror",
                        lambda error: browser_errors.append(type(error).__name__),
                    )
                    page.goto(url)
                    page.locator("#token").fill(token)
                    page.locator("#login button[type=submit]").click()
                    expect(page.locator("#shell")).to_be_visible()
                    page.locator('#nav a[href="#agents"]').click()
                    page.locator("button[data-action=agent-create]").click()
                    page.locator("[name=runner_id]").select_option("claude")
                    page.locator("[name=prompt]").fill("Make a greeting")
                    submit = page.locator("#sheet-form button[type=submit]")
                    page.locator("[name=allow_external]").check()
                    submit.click()
                    expect(page.get_by_text("Patch ready", exact=True)).to_be_visible(
                        timeout=45000
                    )
                    self.assertEqual(source.read_text(), "before\n")

                    with page.expect_download() as downloaded:
                        page.locator("button[data-action=agent-download]").click()
                    candidate = root / "candidate.patch"
                    downloaded.value.save_as(candidate)
                    git(repo, "apply", "--check", str(candidate))
                    self.assertIn("+after", candidate.read_text())

                    # Move the real branch between runs. The reviewer must still
                    # see the editor's candidate, including the original diff.
                    source.write_text("later\n", encoding="utf-8")
                    git(repo, "commit", "-am", "Advance the original working tree")
                    page.locator("button[data-action=agent-review]").click()
                    expect(
                        page.get_by_role("dialog", name="评审候选补丁")
                    ).to_be_visible()
                    page.locator("[name=allow_external]").check()
                    submit.click()
                    result = page.get_by_text(
                        "Reviewed the saved candidate", exact=True
                    )
                    expect(result).to_be_visible(timeout=45000)
                    page.reload()
                    expect(result).to_be_visible(timeout=15000)
                    self.assertEqual(source.read_text(), "later\n")

                    page.set_viewport_size({"width": 390, "height": 844})
                    page.emulate_media(color_scheme="light")
                    expect(page.locator(".agent-record")).to_be_visible()
                    self.assertTrue(
                        page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                    )
                    page.goto(url + "#overview")
                    jobs = page.locator(".desk-section").filter(
                        has=page.get_by_role("heading", name="外部 Agent", exact=True)
                    )
                    links = jobs.locator('li a[href^="#agents/"]')
                    expect(links).to_have_count(2, timeout=15000)
                    self.assertEqual(
                        len(
                            set(
                                links.evaluate_all(
                                    "links => links.map(link => link.hash)"
                                )
                            )
                        ),
                        2,
                    )
                    self.assertTrue(
                        page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                    )
                    self.assertEqual(browser_errors, [])
                finally:
                    browser.close()
