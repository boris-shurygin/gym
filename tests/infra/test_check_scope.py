import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('check_scope', Path(__file__).parents[2] / 'scripts/check_scope.py')
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class ScopeTests(unittest.TestCase):
    def test_ui_does_not_start_backend_or_full_browser(self):
        plan = scope.classify(['web/src/ui/FinishExerciseButton.tsx', 'docs/development-log.md'])
        self.assertTrue(plan['web'] and plan['smoke'] and plan['deploy'])
        self.assertFalse(any(plan[k] for k in ('api', 'ydb', 'infra', 'browser', 'contracts')))

    def test_data_auth_worker_and_contract_changes_keep_integration_checks(self):
        for path in ('web/src/App.tsx', 'web/src/model.ts', 'web/src/sync.ts', 'web/src/account.ts',
                'web/public/service-worker.js', 'api/handler.py', 'contracts/schema.json', 'db/migrations/example.py'):
            with self.subTest(path=path):
                self.assertTrue(scope.classify([path])['browser'])
        for path in ('api/workouts.py', 'contracts/schema.json', 'db/migrations/example.py'):
            self.assertTrue(scope.classify([path])['ydb'])

    def test_docs_and_skills_do_not_deploy_or_test_app(self):
        plan = scope.classify(['docs/adr/0020.md', '.agents/skills/gym-merge-pr/SKILL.md', 'web/README.md'])
        self.assertFalse(any(plan[k] for k in (*scope.JOBS, 'smoke', 'deploy')))

    def test_unknown_paths_missing_range_and_ci_changes_fail_to_full_checks(self):
        for paths in (None, ['.github/workflows/ci.yml'], ['scripts/check_scope.py'], ['new-runtime/file.py']):
            plan = scope.classify(paths)
            self.assertTrue(all(plan[k] for k in scope.JOBS))
            self.assertTrue(plan['deploy'])

    def test_test_only_changes_do_not_release_app_and_combined_changes_expand_checks(self):
        self.assertFalse(scope.classify(['tests/api/test_workouts.py'])['deploy'])
        plan = scope.classify(['web/src/App.tsx', 'web/src/repository.ts'])
        self.assertTrue(plan['browser'])
        self.assertFalse(plan['smoke'])
        self.assertTrue(scope.classify(['infra/staging_app.py'])['infra'])

    def test_aggregate_rejects_failed_cancelled_missing_or_skipped_required_checks(self):
        plan = scope.classify(['web/src/style.css'])
        needs = {name: {'result': 'success' if plan[name] else 'skipped'} for name in scope.JOBS}
        needs['changes'] = {'result': 'success', 'outputs': {'plan': json.dumps(plan)}}
        scope.verify_results(needs)
        for result in ('failure', 'cancelled', 'skipped', None):
            changed = needs | {'web': {'result': result}}
            with self.assertRaises(ValueError):
                scope.verify_results(changed)
