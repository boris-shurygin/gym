from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, ValidationError

from contracts.canonical import canonical_bytes, fingerprint, parse_request
from contracts.generate import artifacts, read
from contracts.validation import FORMATS, SCHEMA, validate


ROOT = Path(__file__).resolve().parents[2]


def walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = read('bindings.json')['routes']
        cls.bindings = {r['operation_id']: r for r in cls.routes}
        cls.fixtures = read('examples.json')
        cls.examples = {e['operation']: e for e in cls.fixtures['examples']}

    def test_schemas_are_valid_and_all_refs_resolve_locally(self):
        Draft202012Validator.check_schema(SCHEMA)
        for node in walk(SCHEMA):
            if isinstance(node, dict) and '$ref' in node:
                ref = node['$ref']
                self.assertTrue(ref.startswith('#/$defs/'))
                self.assertIn(ref.removeprefix('#/$defs/'), SCHEMA['$defs'])

    def test_every_route_has_a_valid_input_and_output_example(self):
        self.assertEqual(set(self.bindings), set(self.examples))
        self.assertEqual(len(self.routes), len({(r['method'], r['path']) for r in self.routes}))
        for name, example in self.examples.items():
            with self.subTest(operation=name):
                route = self.bindings[name]
                validate(route['input'], example['input'])
                if route['output']:
                    validate(route['output'], example['output'])
                else:
                    self.assertIsNone(example['output'])

    def test_extra_commands_and_error_receipt(self):
        for example in self.fixtures['extra']:
            with self.subTest(schema=example['schema'], value=example['value']):
                validate(example['schema'], example['value'])

    def test_negative_examples_are_rejected(self):
        for example in self.fixtures['negative']:
            with self.subTest(case=example['name']):
                with self.assertRaises((ValidationError, ValueError)):
                    validate(example['schema'], example['value'])

    def test_generated_openapi_and_mcp_do_not_drift(self):
        for name, expected in artifacts().items():
            self.assertEqual(read(name), expected, name)
        api = read('openapi.json')
        self.assertEqual(api['openapi'], '3.1.2')
        for route in self.routes:
            operation = api['paths'][route['path']][route['method']]
            self.assertEqual(operation['operationId'], route['operation_id'])
            self.assertIn(str(route['success_status']), operation['responses'])
            if route['auth'] == 'cookie' and route['method'] != 'get':
                origin = next(p for p in operation['parameters'] if p['name'] == 'Origin')
                self.assertTrue(origin['required'])
                csrf = next(p for p in operation['parameters'] if p['name'] == 'X-CSRF-Token')
                self.assertEqual(csrf['required'], route['operation_id'] != 'auth_logout')
            for response in operation['responses'].values():
                self.assertEqual(response['headers']['Cache-Control']['schema']['const'], 'no-store')
            if '429' in operation['responses']:
                self.assertTrue(operation['responses']['429']['headers']['Retry-After']['required'])

    def test_openapi_document_matches_official_schema(self):
        schema = read('vendor/openapi-3.1.schema.json')
        self.assertEqual(schema['$id'], 'https://spec.openapis.org/oas/3.1/schema/2026-08-03')
        Draft202012Validator(schema).validate(read('openapi.json'))

    def test_mcp_uses_identical_inputs_and_typed_success_and_error_outputs(self):
        tools = {t['name']: t for t in read('mcp-tools.json')['tools']}
        error = {'code':'unavailable','message':'Временно недоступно','trace_id':'trace-a','retryable':True,'action':'retry','retry_after_seconds':1,'field_errors':[],'receipt':None}
        for route in self.routes:
            if not route['mcp_tool']:
                continue
            with self.subTest(tool=route['mcp_tool']):
                tool = tools[route['mcp_tool']]
                self.assertEqual(tool['_meta']['gym.requiredScope'], route['scope'])
                self.assertEqual(tool['inputSchema']['type'], 'object')
                for schema in [tool['inputSchema'], tool['outputSchema']]:
                    Draft202012Validator.check_schema(schema)
                    for node in walk(schema):
                        if isinstance(node, dict) and '$ref' in node:
                            self.assertTrue(node['$ref'].startswith('#/$defs/'))
                example = self.examples[route['operation_id']]
                Draft202012Validator(tool['inputSchema'], format_checker=FORMATS).validate(example['input'])
                output = Draft202012Validator(tool['outputSchema'], format_checker=FORMATS)
                output.validate({'ok': True, 'data': example['output']})
                output.validate({'ok': False, 'error': error})
                with self.assertRaises(ValidationError):
                    output.validate({'ok': True, 'error': error})
                unsupported = next(code for code in read('errors.json') if code not in route['errors'])
                policy = read('errors.json')[unsupported]
                wrong = {**error, 'code':unsupported, 'retryable':policy['retryable'], 'action':policy['action']}
                with self.assertRaises(ValidationError):
                    output.validate({'ok':False, 'error':wrong})

    def test_mcp_does_not_expose_owner_approval_or_workout_mutations(self):
        writers = {r['operation_id'] for r in self.routes if r['mcp_tool'] and r['method'] != 'get'}
        self.assertEqual(writers, {'propose_change','replace_change','validate_change','publish_approved'})
        for route in self.routes:
            if route['mcp_tool']:
                self.assertIn(route['scope'], {'program:read','history:read','stats:read','program:draft','program:publish'})
        self.assertIsNone(self.bindings['approve_and_publish']['mcp_tool'])
        self.assertIsNone(self.bindings['submit_operation']['mcp_tool'])

    def test_error_catalog_matches_schemas_and_rest_statuses(self):
        catalog = read('errors.json')
        self.assertEqual(set(catalog), set(SCHEMA['$defs']['Error']['properties']['code']['enum']))
        for code, policy in catalog.items():
            with self.subTest(code=code):
                error = {'code':code,'message':'Безопасное сообщение','trace_id':'trace-a','retryable':policy['retryable'],'action':policy['action'],'retry_after_seconds':1 if policy['retryable'] else None,'field_errors':[],'receipt':None}
                validate('ErrorResponse', {'error':error})
                error['retryable'] = not policy['retryable']
                with self.assertRaises(ValidationError):
                    validate('ErrorResponse', {'error':error})
        for route in self.routes:
            responses = read('openapi.json')['paths'][route['path']][route['method']]['responses']
            for code in route['errors']:
                self.assertIn(str(catalog[code]['http_status']), responses)

    def test_canonical_golden_vectors(self):
        for vector in read('canonical-vectors.json')['vectors']:
            self.assertEqual(canonical_bytes(vector['value']).decode('utf-8'), vector['canonical_text'])
            self.assertEqual(fingerprint(vector['kind'], vector['value']), vector['sha256'])

    def test_strict_json_rejects_ambiguous_or_unsafe_encodings(self):
        invalid = [b'{"a":1,"a":2}', b'{"a":1.0}', b'{"a":1e0}', b'{"a":NaN}', b'{"a":Infinity}',
                   b'{"a":9007199254740992}', b'{"a":"\\ud800"}', b'\xff',
                   b'['*70 + b'0' + b']'*70, b' ' * 1_048_577]
        for raw in invalid:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                parse_request(raw)
        self.assertEqual(parse_request('{"name":"Я","n":1}'), {'name':'Я','n':1})

    def test_fingerprints_bind_full_content_and_preserve_unicode(self):
        operation = self.examples['submit_operation']['input']
        self.assertEqual(fingerprint('operation', operation), fingerprint('operation', dict(reversed(list(operation.items())))))
        changed = deepcopy(operation)
        changed['client_occurred_at'] = '2026-10-26T12:00:01Z'
        self.assertNotEqual(fingerprint('operation', operation), fingerprint('operation', changed))
        proposal = self.examples['propose_change']['input']['proposal']
        for field, value in [('base_program_version_id', None), ('rationale', 'Иное обоснование')]:
            changed = deepcopy(proposal)
            changed[field] = value
            self.assertNotEqual(fingerprint('proposal', proposal), fingerprint('proposal', changed))
        self.assertNotEqual(canonical_bytes({'name':'é'}), canonical_bytes({'name':'e\u0301'}))
        self.assertNotEqual(fingerprint('operation', operation), fingerprint('request', operation))

    def test_documented_flows_have_valid_messages_and_repeat_receipts(self):
        for flow in self.fixtures['flows']:
            for step in flow['steps']:
                route = self.bindings[step['operation']]
                validate(route['input'], step['input'])
                validate(route['output'], step['output'])
        flow = self.fixtures['flows'][0]['steps']
        self.assertEqual(flow[1], flow[3])
        publish = self.fixtures['flows'][1]['steps']
        self.assertEqual(publish[-1]['output'], publish[-2]['output'])
        grant = self.examples['list_grants']['output']['items'][0]
        for step in publish:
            route = self.bindings[step['operation']]
            if route['mcp_tool']:
                self.assertIn(route['scope'], grant['scopes'])
        old = self.examples['get_workout']['output']['plan']['template']['exercises'][0]['targets'][0]
        new = publish[-1]['output']['program_version']['content']['templates'][0]['exercises'][0]['targets'][0]
        self.assertEqual(old['weight_g'], 60000)
        self.assertEqual(new['weight_g'], 62500)

    def test_identity_is_a_runtime_check_not_a_schema_promise(self):
        operation = deepcopy(self.examples['submit_operation']['input'])
        operation['expected_owner_id'] = 'user-b'
        # Структурно допустимо; реальный сервер обязан сверить с личностью.
        validate('SyncOperation', operation)
        self.assertEqual(operation['expected_owner_id'], 'user-b')
        publish = deepcopy(self.examples['publish_approved']['input'])
        publish['approval_id'] = 'unknown-approval'
        # Допустимая форма ссылки также не доказывает существования одобрения.
        validate('PublishInput', publish)


if __name__ == '__main__':
    unittest.main()
