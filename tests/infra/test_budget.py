"""Бюджет ограничен каталогами Gym и не заменяется при расхождении."""

import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("configure_budget", ROOT / "infra/configure_budget.py")
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "infra/bootstrap.json").read_text(encoding="utf-8"))
        self.state = {"cloud_id": self.config["cloud_id"], "folders": {name: name + "-id" for name in ("delivery", "staging", "production")}}
        self.existing = []
        self.writes = []
        self.active = True

    def request(self, path, body=None):
        if body is not None:
            self.writes.append((path, body))
            self.existing.append({"name": body["name"], "costBudget": copy.deepcopy(body["costBudgetSpec"])})
            return {"done": True}
        if path.endswith("/billableObjectBindings"):
            return {"billableObjectBindings": [{"billableObject": {"id": self.config["cloud_id"]}}]}
        if path.startswith("/budgets?"):
            return {"budgets": self.existing}
        return {"id": self.config["billing_account_id"], "currency": "RUB", "active": self.active}

    def run_budget(self, apply=False):
        return budget.configure(self.config, self.state, "operator-id", self.request, apply)

    def test_plan_does_not_write(self):
        self.assertEqual(self.run_budget()["action"], "create")
        self.assertEqual(self.writes, [])

    def test_apply_is_repeatable_and_scoped(self):
        self.run_budget(True)
        self.assertEqual(self.run_budget(True)["action"], "none")
        self.assertEqual(len(self.writes), 1)
        spec = self.writes[0][1]["costBudgetSpec"]
        self.assertEqual(spec["filter"]["cloudFoldersFilters"][0]["folderIds"], ["delivery-id", "production-id", "staging-id"])
        self.assertEqual(spec["notificationUserAccountIds"], ["operator-id"])

    def test_inactive_account_stops_before_write(self):
        self.active = False
        with self.assertRaises(ValueError):
            self.run_budget(True)
        self.assertEqual(self.writes, [])

    def test_existing_budget_drift_is_not_overwritten(self):
        self.run_budget(True)
        self.existing[0]["costBudget"]["filter"]["cloudFoldersFilters"][0]["folderIds"] = []
        with self.assertRaises(ValueError):
            self.run_budget(True)
        self.assertEqual(len(self.writes), 1)

    def test_provider_decimal_precision_is_not_drift(self):
        self.run_budget(True)
        spec = self.existing[0]["costBudget"]
        spec["amount"] = "3000.000000000"
        for rule in spec["thresholdRules"]:
            rule["amount"] += ".000000000"
        self.assertEqual(self.run_budget(True)["action"], "none")
        self.assertEqual(len(self.writes), 1)
