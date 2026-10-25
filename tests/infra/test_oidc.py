"""Проверка отказа для чужих OIDC claims без облачных запросов."""

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("verify_oidc", ROOT / "infra/verify_oidc.py")
oidc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oidc)


class OidcTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "infra/bootstrap.json").read_text(encoding="utf-8"))
        self.audience = self.config["federation"]["audience"]
        self.claims = {
            "iss": self.config["federation"]["issuer"], "aud": self.audience,
            "sub": next(a["subject"] for a in self.config["accounts"] if a["key"] == "staging_ci"),
            "repository": self.config["repository"], "ref": "refs/heads/main",
            "workflow_ref": f"{self.config['repository']}/.github/workflows/cloud-access.yml@refs/heads/main",
            "event_name": "push",
        }

    def test_expected_staging_claims_pass(self):
        oidc.validate_claims(self.claims, self.config, "staging", self.audience)

    def test_wrong_repository_branch_audience_or_event_is_rejected(self):
        for key, value in [("repository", "other/repo"), ("ref", "refs/heads/feature"),
                           ("aud", "other-audience"), ("iss", "https://attacker.invalid"),
                           ("event_name", "pull_request"), ("workflow_ref", "other-workflow")]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    oidc.validate_claims(dict(self.claims, **{key: value}), self.config, "staging", self.audience)

    def test_staging_token_cannot_select_production(self):
        with self.assertRaises(ValueError):
            oidc.validate_claims(self.claims, self.config, "production", self.audience)

    def test_name_only_subject_is_rejected_for_immutable_repository(self):
        claims = dict(self.claims, sub=f"repo:{self.config['repository']}:environment:staging")
        with self.assertRaises(ValueError):
            oidc.validate_claims(claims, self.config, "staging", self.audience)


if __name__ == "__main__":
    unittest.main()
