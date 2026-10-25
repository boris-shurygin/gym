"""Проверки границ доступа bootstrap без подключения к облаку."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(SHELL, "PowerShell is required")
class BootstrapFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="gym-bootstrap-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "infra").mkdir()
        self.script = self.root / "infra/Bootstrap-Cloud.ps1"
        shutil.copyfile(ROOT / "infra/Bootstrap-Cloud.ps1", self.script)
        shutil.copyfile(ROOT / "infra/Yc-Common.ps1", self.root / "infra/Yc-Common.ps1")
        self.config = json.loads((ROOT / "infra/bootstrap.json").read_text(encoding="utf-8"))
        self.config_path = self.root / "infra/bootstrap.json"
        self.state_path = self.root / "fake-state.json"
        self.state = {
            "cloud": {"id": self.config["cloud_id"], "organization_id": self.config["organization_id"]},
            "folders": [], "accounts": [], "federations": [],
            "credentials": [], "bindings": [], "calls": [], "fail_prefix": "", "progress": False,
            "resources": {},
        }

    def invoke(self, apply=False, script=None, extra_args=()):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        env = dict(os.environ, GYM_FAKE_YC_STATE=str(self.state_path))
        result = subprocess.run(
            [SHELL, "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File", str(script or self.script),
             "-ConfigPath", str(self.config_path), "-YcPath", str(ROOT / "tests/infra/Fake-Yc.ps1"),
             *extra_args,
             *(["-Apply"] if apply else [])],
            env=env, capture_output=True, text=True, errors="replace", timeout=60,
        )
        self.state = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
        return result

    def mutations(self):
        return [s for s in self.state["calls"] if " create " in s or " add-access-binding " in s]


class BootstrapTests(BootstrapFixture):
    def test_plan_does_not_mutate_cloud_or_write_state(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.root / ".local/bootstrap-state.json").exists())
        self.assertGreater(len(json.loads(result.stdout)["actions"]), 0)

    def test_apply_is_repeatable_and_only_grants_metadata_access(self):
        first = self.invoke(apply=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.state["folders"]), 3)
        self.assertEqual(len(self.state["accounts"]), 9)
        self.assertEqual(len(self.state["credentials"]), 3)
        self.assertEqual([b["role_id"] for b in self.state["bindings"]], ["auditor"] * 3)
        mutations_before = list(self.mutations())
        second = self.invoke(apply=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.mutations(), mutations_before)
        self.assertEqual(json.loads(second.stdout)["planned_actions"], [])

    def test_wrong_cloud_stops_before_mutation(self):
        self.state["cloud"]["id"] = "another-cloud"
        result = self.invoke(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_progress_on_stderr_does_not_corrupt_json(self):
        self.state["progress"] = True
        result = self.invoke(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.state["folders"]), 3)

    def test_existing_unowned_folder_is_not_adopted(self):
        self.state["folders"] = [{"id": "other", "cloud_id": self.config["cloud_id"],
                                  "name": "gym-production", "status": "ACTIVE"}]
        result = self.invoke(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_changed_federation_does_not_expand_trust(self):
        self.assertEqual(self.invoke(apply=True).returncode, 0)
        self.state["federations"][0]["audiences"] = ["https://attacker.invalid"]
        previous = list(self.mutations())
        result = self.invoke(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), previous)

    def test_unexpected_subject_does_not_gain_access(self):
        self.assertEqual(self.invoke(apply=True).returncode, 0)
        self.state["credentials"][0]["external_subject_id"] = "repo:other/repo:environment:production"
        previous = list(self.mutations())
        result = self.invoke(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), previous)

    def test_provider_errors_do_not_echo_credentials(self):
        self.state["fail_prefix"] = "resource-manager cloud get"
        result = self.invoke(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SECRET-SENTINEL", result.stdout + result.stderr)
        self.assertEqual(self.mutations(), [])


if __name__ == "__main__":
    unittest.main()
