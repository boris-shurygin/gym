import copy
import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from infra.migration_container import CONTAINER, deploy, environment, plan, recovery_evidence
from infra.ydb_access import load_config


class ContainerPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.env = environment(self.config, 'validation', 'gym_worker_probe_123456789abc_', 'worker')
        self.image = 'cr.yandex/crpe91o8ka9e03n3n90c/gym-migrations@sha256:' + '1' * 64
        self.container = {'id': CONTAINER, 'name': 'gym-migrations', 'folder_id': self.config['folder_id'],
                          'labels': {'gym-project': 'gym'}}
        self.access, self.registry, self.revisions = [], [], []
        self.yc = Mock()
        self.yc.run.side_effect = self.call

    def call(self, *args):
        if args[:3] == ('serverless', 'container', 'get'):
            return self.container
        if args[:3] == ('serverless', 'container', 'list-access-bindings'):
            return self.access
        if args[:3] == ('serverless', 'container', 'revision'):
            return self.revisions
        if args[:3] == ('container', 'registry', 'list-access-bindings'):
            return self.registry
        raise AssertionError('Unexpected mutation or request')

    def test_plan_is_read_only_and_specific(self):
        result = plan(self.yc, self.image, self.env)
        self.assertTrue(result['add_registry_puller'])
        self.assertTrue(result['deploy'])
        self.assertEqual(result['container_id'], CONTAINER)

    def test_public_container_or_foreign_owner_is_rejected(self):
        self.access.append({'subject': {'type': 'system', 'id': 'allUsers'}})
        with self.assertRaises(ValueError):
            plan(self.yc, self.image, self.env)
        self.access.clear()
        self.container['folder_id'] = 'production'
        with self.assertRaises(ValueError):
            plan(self.yc, self.image, self.env)

    def test_tag_other_registry_or_database_is_rejected(self):
        for image in (self.image.replace('@sha256:' + '1' * 64, ':latest'), self.image.replace('crpe91o8', 'other')):
            with self.assertRaises(ValueError):
                plan(self.yc, image, self.env)
        for key, value in (('GYM_YDB_DATABASE', '/production'), ('GYM_YDB_AUTH', 'anonymous'), ('SECRET', 'x')):
            with self.assertRaises(ValueError):
                plan(self.yc, self.image, self.env | {key: value})

    def test_excess_registry_role_does_not_get_overwritten(self):
        self.registry.append({'role_id': 'container-registry.images.pusher',
                              'subject': {'id': self.config['accounts']['migrations']}})
        with self.assertRaises(ValueError):
            plan(self.yc, self.image, self.env)

    def test_matching_revision_is_reused_and_budget_change_is_detected(self):
        self.registry.append({'role_id': 'container-registry.images.puller',
                              'subject': {'id': self.config['accounts']['migrations']}})
        active = {'id': 'revision', 'status': 'ACTIVE', 'image': {'image_digest': 'sha256:' + '1' * 64,
                  'environment': self.env}, 'service_account_id': self.config['accounts']['migrations'],
                  'execution_timeout': '120s', 'concurrency': '1', 'resources': {'memory': '536870912', 'cores': '1', 'core_fraction': '100'},
                  'metadata_options': {'gce_http_endpoint': 'ENABLED', 'aws_v1_http_endpoint': 'DISABLED'}, 'runtime': {'http': {}},
                  'log_options': {'disabled': True}, 'scaling_policy': {'zone_instances_limit': '1', 'zone_requests_limit': '1'}}
        self.revisions.append(active)
        self.assertFalse(plan(self.yc, self.image, self.env)['deploy'])
        self.assertFalse(plan(self.yc, self.image, self.env)['add_registry_puller'])
        active['resources']['cores'] = '2'
        self.assertTrue(plan(self.yc, self.image, self.env)['deploy'])

    def test_modes_and_fault_are_only_operator_configuration(self):
        for kwargs in ({'prefix': 'gym_production_'}, {'crash': True, 'prefix': 'gym_'},
                       {'mode': 'recovery'}, {'mode': 'worker', 'ticket': {'x': 1}},
                       {'mode': 'recovery', 'ticket': {}}, {'mode': 'recovery', 'ticket': {'x': 1}}):
            values = dict(release='validation', prefix='gym_', mode='worker') | kwargs
            with self.assertRaises(ValueError):
                environment(self.config, **values)

    def test_operation_poll_uses_positional_id_and_verifies_actual_revision(self):
        initial = {'add_registry_puller': False, 'deploy': True}
        confirmed = {'add_registry_puller': False, 'deploy': False, 'active_revision': 'revision'}
        self.yc.run.side_effect = None
        self.yc.run.return_value = {'id': 'operation', 'done': True}
        with patch('infra.migration_container.plan', side_effect=[initial, confirmed]), \
                patch('infra.migration_container.api', return_value={'id': 'operation', 'done': False}), \
                patch('infra.migration_container.time.sleep'):
            self.assertEqual(deploy(self.yc, self.image, self.env, apply=True), confirmed)
        self.yc.run.assert_called_once_with('operation', 'get', 'operation')

    def test_recovery_requires_consistent_observation_and_obsolete_same_image(self):
        evidence = {'old_revision': 'old', 'observed_obsolete_at': 100,
                    'execution_timeout_seconds': 120, 'quiescence_seconds': 180,
                    'owner': 'a' * 32, 'generation': 1, 'job_id': 'job'}
        ticket = {'job_id': 'job', 'owner': 'a' * 32, 'generation': 1, 'not_before': 280,
                  'evidence_sha256': hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()}
        old = {'id': 'old', 'container_id': CONTAINER, 'status': 'OBSOLETE', 'execution_timeout': '120s',
               'image': {'image_digest': 'sha256:' + '1' * 64},
               'service_account_id': self.config['accounts']['migrations'], 'runtime': {'http': {}}}
        recovery_evidence(evidence, ticket, [old], self.image)
        for changed in ({'not_before': 279}, {'evidence_sha256': '0' * 64}, {'generation': 2}):
            with self.assertRaises(ValueError):
                recovery_evidence(evidence, ticket | changed, [old], self.image)
        with self.assertRaises(ValueError):
            recovery_evidence(evidence, ticket, [old | {'status': 'ACTIVE'}], self.image)
        with self.assertRaises(ValueError):
            recovery_evidence(evidence, ticket, [], self.image)


if __name__ == '__main__':
    unittest.main()
