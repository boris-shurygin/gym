"""Проверки повторного provision и отказа от публичных/зарезервированных ресурсов."""

import json
import shutil
import unittest

import test_bootstrap as bootstrap


@unittest.skipUnless(bootstrap.SHELL, "PowerShell is required")
class ResourceTests(bootstrap.BootstrapFixture):
    def setUp(self):
        super().setUp()
        for name in ("Provision-Resources.ps1", "resources.json", "gateway-bootstrap.json"):
            shutil.copyfile(bootstrap.ROOT / "infra" / name, self.root / "infra" / name)
        result = self.invoke(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def provision(self, apply=False):
        return self.invoke(
            apply=apply, script=self.root / "infra/Provision-Resources.ps1",
            extra_args=("-ResourcesPath", str(self.root / "infra/resources.json"),
                        "-StatePath", str(self.root / ".local/bootstrap-state.json")),
        )

    def test_resource_plan_does_not_create_paid_resources(self):
        previous = list(self.mutations())
        result = self.provision()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mutations(), previous)
        self.assertFalse((self.root / ".local/resources-state.json").exists())
        self.assertEqual(len(json.loads(result.stdout)["actions"]), 12)

    def test_resource_apply_is_repeatable(self):
        first = self.provision(apply=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(sum(map(len, self.state["resources"].values())), 12)
        previous = list(self.mutations())
        second = self.provision(apply=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.mutations(), previous)
        self.assertEqual(json.loads(second.stdout)["created"], [])

    def test_public_bucket_drift_is_not_silently_adopted(self):
        self.assertEqual(self.provision(apply=True).returncode, 0)
        self.state["resources"]["bucket"][0]["anonymous_access_flags"]["read"] = True
        previous = list(self.mutations())
        result = self.provision(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), previous)

    def test_reserved_capacity_is_rejected(self):
        path = self.root / "infra/resources.json"
        resources = json.loads(path.read_text(encoding="utf-8"))
        resources["ydb_provisioned_ru"] = 10
        path.write_text(json.dumps(resources), encoding="utf-8")
        previous = list(self.mutations())
        result = self.provision(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), previous)

    def test_database_storage_drift_is_rejected(self):
        self.assertEqual(self.provision(apply=True).returncode, 0)
        self.state["resources"]["database"][0]["serverless_database"]["storage_size_limit"] *= 2
        previous = list(self.mutations())
        result = self.provision(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), previous)


if __name__ == "__main__":
    unittest.main()
