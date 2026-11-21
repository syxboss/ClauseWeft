import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clauseweft.testing import freeze_suite, start, verify
from clauseweft.workspace import Workspace


TEST_IMAGE = os.environ.get("CLAUSEWEFT_TEST_IMAGE")


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=Fixture", "-c", "user.email=test@localhost",
                           "-c", "core.hooksPath=/dev/null", *args], input=data, capture_output=True,
                          check=True).stdout


def change(before, after, name="app.py"):
    return f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-{before}\n+{after}\n"


def config(image="sha256:" + "1" * 64, seconds=15):
    return dict(image=image, timeout_seconds=seconds, commands={"invoke": ["python", "app.py"]},
                cases=[dict(id="regular", clause_id="CSV-01", command_id="invoke",
                            stdin="member\n", stdout="denied\n", required_files=["app.py"])])


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "--template=")
        (self.repo / "app.py").write_text("print('base')\n")
        (self.repo / "other.txt").write_text("old\n")
        (self.repo / ".env").write_text("private\n")
        (self.repo / "key.txt").write_text("-----BEGIN PRIVATE KEY-----\n")
        (self.repo / "link").symlink_to("/etc")
        (self.repo / "request.py").write_text('headers = {"Authorization": "Bearer synthetic-fixture-token"}\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        self.ws = Workspace(self.root / "artifacts", self.repo, ["."])
        self.sha = self.ws.snapshot("HEAD")["base_sha"]


class WorkspaceTest(RepositoryFixture):
    def test_snapshot_and_scope(self):
        self.assertEqual(set(self.ws.files(self.sha)), {"app.py", "other.txt"})
        for hidden in (".env", "key.txt", "link", "request.py"):
            ref = git(self.repo,"rev-parse",f"{self.sha}:{hidden}").decode().strip()
            with self.assertRaises(subprocess.CalledProcessError):
                git(self.ws.git,"cat-file","-e",ref)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.ws.stage_patch(self.sha, change("private", "x", ".env"))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.ws.stage_patch(self.sha, change("old", "new", "other.txt"), paths=["app.py"])

    def test_rejected_link_and_excluded_dir(self):
        link_patch = ("diff --git a/link2 b/link2\nnew file mode 120000\n--- /dev/null\n+++ b/link2\n"
                      "@@ -0,0 +1 @@\n+other.txt\n\\ No newline at end of file\n")
        with self.assertRaisesRegex(ValueError,"outside"):
            self.ws.stage_patch(self.sha,link_patch)
        scoped = Workspace(self.root/"limited",self.repo,["app.py"])
        self.assertEqual(set(scoped.files(self.sha)),{"app.py"})
        oid = git(self.repo,"rev-parse","HEAD:other.txt").decode().strip()
        with self.assertRaises(subprocess.CalledProcessError):
            git(scoped.git,"cat-file","-e",oid)
        stage = scoped.stage_patch(self.sha,change("print('base')","print(42)"))
        candidate = scoped.integrate(self.sha,[stage["patch_id"]])
        with self.assertRaises(subprocess.CalledProcessError):
            git(scoped.git,"cat-file","-e",oid)
        git(self.repo,"apply","--index","-",data=candidate["patch"].encode())
        self.assertEqual(git(self.repo,"write-tree").decode().strip(),candidate["tree_hash"])

    def test_credentials_are_filtered_from_entire_patch(self):
        with self.assertRaisesRegex(ValueError, "restricted content"):
            self.ws.stage_patch(self.sha, change("old", 'api_key = "dummy-for-validation"', "other.txt"))

    def test_independent_patches_conflict_replay_and_reopen(self):
        first = self.ws.stage_patch(self.sha, change("print('base')", "print(1)"))
        second = self.ws.stage_patch(self.sha, change("print('base')", "print(2)"))
        other = self.ws.stage_patch(self.sha, change("old", "new", "other.txt"))
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.ws.integrate(self.sha, [first["patch_id"], second["patch_id"]])
        selected = [second["patch_id"], other["patch_id"]]
        candidate = self.ws.integrate(self.sha, selected)
        self.assertEqual(git(self.repo, "status", "--porcelain"), b"")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").decode().strip(), self.sha)
        checkout = self.root / "replay"
        git(self.root, "clone", "--local", str(self.repo), str(checkout))
        git(checkout, "apply", "--index", "-", data=candidate["patch"].encode())
        self.assertEqual(git(checkout, "write-tree").decode().strip(), candidate["tree_hash"])
        reopened = Workspace(self.ws.root, self.repo, ["."])
        self.assertEqual(candidate, reopened.integrate(self.sha, selected))
        self.assertEqual(reopened.files(candidate["candidate_sha"])["app.py"], b"print(2)\n")


class ReportTest(unittest.TestCase):
    def test_missing_runtime(self):
        suite = freeze_suite("v1", config())
        with patch("clauseweft.testing.subprocess.run", side_effect=FileNotFoundError):
            result = start({"candidate_sha": "c", "tree_hash": "t"}, suite, {})
        self.assertEqual(result["status"], "needs_configuration")
        self.assertFalse(verify(result, {"candidate_sha": "c", "tree_hash": "t"}, suite))

    def test_invalid_plans(self):
        for mutator in (lambda cfg: cfg.update(image="mutable:latest"),
                        lambda cfg: cfg["cases"].append(cfg["cases"][0]),
                        lambda cfg: cfg.update(cases=[])):
            cfg = config()
            mutator(cfg)
            with self.assertRaises(ValueError):
                freeze_suite("v1", cfg)


@unittest.skipUnless(TEST_IMAGE, "set CLAUSEWEFT_TEST_IMAGE to a local Python image ID")
class DockerEvidenceTest(RepositoryFixture):
    def test_red_missing_green_timeout_on_pinned_commits(self):
        source = "print('base')"
        suite = freeze_suite("v1", config(TEST_IMAGE))
        candidates = []
        for program, expected in (("print('leaked')", "failed"), (None, "missing"),
                                  ("print('denied' if input() == 'member' else 'ok')", "passed"),
                                  ("import time; time.sleep(60)", "incomplete")):
            patch_text = (change(source, program) if program else
                          "diff --git a/app.py b/app.py\ndeleted file mode 100644\n--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print('base')\n")
            stage = self.ws.stage_patch(self.sha, patch_text)
            candidate = self.ws.integrate(self.sha, [stage["patch_id"]])
            candidates.append(candidate)
            active = suite if expected != "incomplete" else freeze_suite("v1", config(TEST_IMAGE, 2))
            report = start(candidate, active, self.ws.files(candidate["candidate_sha"]))
            self.assertEqual(report["status"], expected, json.dumps(report, indent=2))
            self.assertEqual(verify(report, candidate, active), expected == "passed")
            if expected == "passed":
                self.assertFalse(verify(report, candidates[0], active))
                self.assertFalse(verify(report, candidate, freeze_suite("v2", config(TEST_IMAGE))))
                for records in ([], report["cases"] * 2, [dict(report["cases"][0], status="skipped")]):
                    tampered = copy.deepcopy(report)
                    tampered["cases"] = records
                    self.assertFalse(verify(tampered, candidate, active))
            if expected == "incomplete":
                self.assertEqual(report["cases"][0]["reason"], "timeout")
