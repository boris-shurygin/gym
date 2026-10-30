import copy
import hashlib
import unittest
from unittest.mock import patch

from infra import function_tags as tags, staging_target, production_target
from infra.staging_migrations import InterventionRequired


def release(number):
    return f'{number:040x}_{number}_1'


class Cloud:
    def __init__(self, target):
        self.target = target
        self.labels = {'environment': target.NAME, 'owner': 'gym'}
        self.items, self.removed = [], []
        self.current, self.digest = None, 'd' * 64
        self.after_patch = False
        self.before_remove = None

    def json(self, url, payload=None, *, method=None):
        if url.endswith('/functions/' + self.target.FUNCTION):
            if method == 'PATCH':
                assert set(payload) == {'updateMask', 'labels'} and payload['updateMask'] == 'labels'
                self.labels = copy.deepcopy(payload['labels'])
                if self.after_patch:
                    raise TimeoutError('response lost')
                return {'done': True}
            return {'name': 'gym-api', 'folderId': self.target.FOLDER, 'labels': copy.deepcopy(self.labels)}
        if 'versions?functionId=' in url:
            return {'versions': copy.deepcopy(self.items)}
        if url.endswith(':removeTag'):
            identifier = url.split('/')[-1].split(':')[0]
            assert self.current is None or payload['tag'] != tags.tag(self.current)
            if self.before_remove:
                self.before_remove()
            for item in self.items:
                if item['id'] == identifier:
                    item['tags'].remove(payload['tag'])
            self.removed.append((identifier, payload['tag']))
            return {'done': True}
        raise AssertionError(url)

    def wait(self, value):
        assert value['done']


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.target = staging_target
        self.start(self.target)

    def start(self, target):
        self.cloud = Cloud(target)
        self.receipts, self.manifests = {}, {}
        self.controller = tags.TagRetention(self.cloud, 'unused', target)
        self.controller.installed = lambda: (self.cloud.current, self.cloud.digest)
        def read(store, key, **kwargs):
            if key.startswith('deployments/'):
                return copy.deepcopy(self.receipts.get(key.split('/')[1]))
            return copy.deepcopy(self.manifests[key.split('/')[1]])
        self.addCleanup(patch.stopall)
        patch.object(tags, 'read_object', side_effect=read).start()
        patch.object(tags, 'read_manifest', side_effect=lambda token, r, d: copy.deepcopy(self.manifests[r])).start()
        self.add(1)
        self.cloud.current = release(1)
        patch.dict(tags.LEGACY, {target.NAME: (release(1),)}).start()

    def add(self, number, *, success=True):
        r = release(number)
        manifest = {'release_id': r, 'artifacts': [{'path': 'api.zip', 'sha256': 'a' * 64}]}
        self.manifests[r] = manifest
        self.cloud.items.append({'id': f'version{number}', 'functionId': self.cloud.target.FUNCTION,
            'status': 'ACTIVE', 'serviceAccountId': self.cloud.target.API_ACCOUNT, 'tags': [tags.tag(r)],
            'environment': {'GYM_ENVIRONMENT': self.cloud.target.NAME, 'GYM_RELEASE_ID': r, 'GYM_API_ZIP_SHA256': 'a' * 64}})
        receipt = {'format': 1, 'environment': self.cloud.target.NAME, 'release_id': r, 'main_sha': r[:40],
            'function_tag': tags.tag(r), 'function_version': f'version{number}', 'smoke': 'succeeded',
            'purpose': self.cloud.target.NAME + '-release', 'origin': self.cloud.target.ORIGIN,
            'manifest_sha256': hashlib.sha256(tags.canonical_bytes(manifest)).hexdigest(), 'gateway_sha256': self.cloud.digest}
        if success:
            self.receipts[r] = receipt
        return receipt

    def install(self, number):
        self.controller.prepare(release(number))
        receipt = self.add(number)
        self.cloud.current = release(number)
        self.controller.finish(receipt)

    def test_many_releases_never_accumulate_tags_and_keep_unrelated_labels(self):
        for number in range(2, 17):
            self.install(number)
            self.assertLessEqual(sum(len(v['tags']) for v in self.cloud.items), 3)
            self.assertEqual(tags.history(self.cloud.labels), [release(n) for n in range(number, max(0, number-3), -1)])
        self.assertEqual(self.cloud.labels['owner'], 'gym')
        self.assertEqual(self.cloud.labels['environment'], 'staging')

    def test_production_uses_same_policy_on_its_fixed_function(self):
        patch.stopall()
        self.start(production_target)
        for number in range(2, 6): self.install(number)
        self.assertEqual(tags.history(self.cloud.labels), [release(5), release(4), release(3)])
        self.assertEqual(self.controller.function.split('/')[-1], production_target.FUNCTION)

    def test_rollback_is_success_order_and_repeated_deploy_is_idempotent(self):
        for n in range(2, 5): self.install(n)
        self.controller.prepare(release(3))
        self.cloud.current = release(3)
        self.controller.finish(self.receipts[release(3)])
        self.controller.prepare(release(3)); self.controller.finish(self.receipts[release(3)])
        self.assertEqual(tags.history(self.cloud.labels), [release(3), release(4), release(2)])

    def test_failed_candidates_never_displace_successful_predecessors(self):
        for n in range(2, 4): self.install(n)
        for n in range(4, 15):
            self.controller.prepare(release(n)); self.add(n, success=False)
            self.assertLessEqual(sum(len(v['tags']) for v in self.cloud.items), 4)
        self.assertEqual(tags.history(self.cloud.labels), [release(3), release(2), release(1)])

    def test_switch_without_smoke_receipt_keeps_both_current_and_old_window(self):
        for n in range(2, 4): self.install(n)
        self.add(4, success=False); self.cloud.current = release(4)
        self.controller.prepare(release(5))
        self.assertEqual(tags.history(self.cloud.labels), [release(3), release(2), release(1)])
        self.assertTrue(all(tags.tag(release(n)) in sum([v['tags'] for v in self.cloud.items], []) for n in range(1, 5)))

    def test_crash_after_receipt_recovers_actual_success_before_cleaning(self):
        for n in range(2, 4): self.install(n)
        self.add(4); self.cloud.current = release(4)
        self.controller.prepare(release(5))
        self.assertEqual(tags.history(self.cloud.labels), [release(4), release(3), release(2)])
        self.assertNotIn(tags.tag(release(1)), sum([v['tags'] for v in self.cloud.items], []))

    def test_lost_label_response_is_read_back_without_repeating_patch(self):
        self.cloud.after_patch = True
        self.controller.prepare(release(2))
        self.assertEqual(tags.history(self.cloud.labels), [release(1)])

    def test_unknown_tags_and_latest_survive_cleaning(self):
        self.controller.prepare(release(2))
        self.add(2, success=False)
        self.cloud.items[-1]['tags'] += ['$latest', 'operator-tag', 'v-' + '1' * 32]
        self.controller.prepare(release(3))
        self.assertEqual(self.cloud.items[-1]['tags'], ['$latest', 'operator-tag', 'v-' + '1' * 32])

    def test_capacity_due_to_unknown_tags_stops_before_any_mutation(self):
        self.cloud.items[0]['tags'] += [f'operator-{n}' for n in range(9)]
        with self.assertRaises(InterventionRequired): self.controller.prepare(release(2))
        self.assertNotIn('gym-tag-schema', self.cloud.labels)
        self.assertEqual(self.cloud.removed, [])

    def test_rotated_successful_release_cannot_be_recreated_under_old_receipt(self):
        for n in range(2, 5): self.install(n)
        with self.assertRaises(InterventionRequired): self.controller.prepare(release(1))

    def test_bootstrap_mismatch_or_corrupt_history_never_cleans(self):
        self.add(2); self.cloud.current = release(2)
        with self.assertRaises(InterventionRequired): self.controller.prepare(release(3))
        self.cloud.current = release(1)
        self.cloud.labels['gym-tag-schema'] = '2'
        with self.assertRaises(InterventionRequired): self.controller.prepare(release(3))
        self.assertEqual(self.cloud.removed, [])

    def test_partial_history_rejected(self):
        for labels in [{'gym-tag-schema': '1'}, {'gym-tag-schema': '1', 'gym-tag-previous-1': release(1)},
                       {'gym-tag-schema': '1', 'gym-tag-current': release(1), 'gym-tag-previous-1': release(1)}]:
            with self.subTest(labels=labels), self.assertRaises(InterventionRequired): tags.history(labels)

    def test_wrong_receipt_environment_or_archive_stops_before_cleanup(self):
        self.add(2)
        for key, value in [('environment', 'production'), ('manifest_sha256', 'b' * 64), ('function_version', 'other')]:
            original = self.receipts[release(2)][key]; self.receipts[release(2)][key] = value
            with self.subTest(key=key), self.assertRaises(InterventionRequired): self.controller.prepare(release(3))
            self.assertEqual(self.cloud.removed, [])
            self.receipts[release(2)][key] = original
            self.controller.archives.clear()

    def test_archive_missing_stops_without_changing_tags(self):
        self.add(2, success=False)
        with patch.object(tags, 'read_manifest', side_effect=ValueError('missing artifact')):
            with self.assertRaises(ValueError): self.controller.prepare(release(3))
        self.assertEqual(self.cloud.removed, [])

    def test_live_gateway_drift_stops_cleanup(self):
        self.add(2, success=False)
        plan = self.controller.plan(release(3))
        self.controller.save(plan['labels'], plan['history'])
        self.cloud.current = release(2)
        with self.assertRaises(InterventionRequired): self.controller.clean(plan)
        self.assertEqual(self.cloud.removed, [])

    def test_finish_requires_actual_gateway_and_immutable_receipt(self):
        self.controller.prepare(release(2)); receipt = self.add(2)
        with self.assertRaises(InterventionRequired): self.controller.finish(receipt)
        self.assertEqual(tags.history(self.cloud.labels), [release(1)])

    def test_incoming_candidate_is_protected_even_without_receipt(self):
        self.add(2, success=False)
        self.controller.prepare(release(2))
        self.assertEqual(self.cloud.removed, [])

    def test_moved_tag_is_not_removed_from_another_version(self):
        self.add(2, success=False)
        plan = self.controller.plan(release(3))
        self.controller.save(plan['labels'], plan['history'])
        self.cloud.items[-1]['id'] = 'replaced'
        with self.assertRaises(InterventionRequired): self.controller.clean(plan)
        self.assertEqual(self.cloud.removed, [])

    def test_lost_remove_response_is_safe_to_resume(self):
        self.add(2, success=False)
        original = self.cloud.wait
        def fail(value):
            if self.cloud.removed: raise TimeoutError('response lost')
            return original(value)
        with patch.object(self.cloud, 'wait', side_effect=fail):
            with self.assertRaises(TimeoutError): self.controller.prepare(release(3))
        self.controller.prepare(release(3))
        self.assertEqual(len(self.cloud.removed), 1)
        self.assertEqual(tags.history(self.cloud.labels), [release(1)])

    def test_partial_version_list_never_changes_labels_or_tags(self):
        original = self.cloud.json
        def partial(url, *args, **kwargs):
            result = original(url, *args, **kwargs)
            if 'versions?' in url: result['nextPageToken'] = 'more'
            return result
        with patch.object(self.cloud, 'json', side_effect=partial):
            with self.assertRaises(InterventionRequired): self.controller.prepare(release(2))
        self.assertEqual(self.cloud.removed, [])
        self.assertIsNone(tags.history(self.cloud.labels))


if __name__ == '__main__':
    unittest.main()
