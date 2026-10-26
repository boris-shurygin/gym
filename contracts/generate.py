"""Построй OpenAPI/MCP из общих схем; --check проверяет отсутствие расхождений."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def read(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))


def bundled(root, definitions):
    result = deepcopy(root)
    included = {}

    def visit(value):
        if isinstance(value, dict):
            if '$ref' in value:
                name = value['$ref'].removeprefix('#/$defs/')
                if value['$ref'] != '#/$defs/' + name:
                    raise ValueError('Внешние ссылки в MCP не разрешены')
                if name not in included:
                    included[name] = deepcopy(definitions[name])
                    visit(included[name])
            for key, item in value.items():
                if key != '$defs':
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(result)
    result['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    result['$defs'] = dict(sorted(included.items()))
    return result


def external_refs(value):
    if isinstance(value, dict):
        return {key: ('schema.json' + item if key == '$ref' else external_refs(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [external_refs(item) for item in value]
    return value


def input_fields(schema):
    if 'properties' in schema:
        return schema['properties'], schema.get('required', [])
    branches = schema['oneOf']
    fields = {}
    required = set(branches[0]['required'])
    for branch in branches:
        fields.update(branch['properties'])
        required.intersection_update(branch['required'])
    return fields, sorted(required)


def artifacts():
    definitions = read('schema.json')['$defs']
    bindings = read('bindings.json')['routes']
    errors = read('errors.json')
    paths = {}
    tools = []
    for binding in bindings:
        name = binding['operation_id']
        source = deepcopy(definitions[binding['input']])
        fields, required = input_fields(source)
        path_fields = re.findall(r'{([^}]+)}', binding['path'])
        parameters = []
        for field in path_fields:
            parameters.append({'name': field, 'in': 'path', 'required': True, 'schema': external_refs(fields[field])})
        if binding['method'] == 'get':
            for field, schema in fields.items():
                if field not in path_fields:
                    parameters.append({'name': field, 'in': 'query', 'required': field in required, 'schema': external_refs(schema)})
        cookie = binding['auth'] == 'cookie'
        if cookie and binding['method'] != 'get':
            parameters.append({'name': 'Origin', 'in': 'header', 'required': True, 'schema': {'type': 'string', 'format': 'uri'}})
            parameters.append({'name': 'X-CSRF-Token', 'in': 'header', 'required': name != 'auth_logout', 'schema': {'type': 'string', 'minLength': 32, 'maxLength': 256}})
        success = {'description': binding['summary'], 'headers': {'Cache-Control': {'required': True, 'schema': {'const': 'no-store'}}}}
        if binding['output']:
            success['content'] = {'application/json': {'schema': {'$ref': 'schema.json#/$defs/' + binding['output']}}}
        if binding['success_status'] == 302:
            success['headers']['Location'] = {'required': True, 'schema': {'type': 'string'}}
        responses = {str(binding['success_status']): success}
        for code in binding['errors']:
            status = str(errors[code]['http_status'])
            headers = {'Cache-Control': {'required': True, 'schema': {'const': 'no-store'}}}
            if status == '429':
                headers['Retry-After'] = {'required': True, 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 3600}}
            allowed = [key for key in binding['errors'] if str(errors[key]['http_status']) == status]
            error_schema = {'allOf': [{'$ref': 'schema.json#/$defs/ErrorResponse'}, {'properties': {'error': {'properties': {'code': {'enum': allowed}}}}}]}
            responses[status] = {'description': 'Ошибка Gym; безопасный code и action из errors.json', 'headers': headers, 'content': {'application/json': {'schema': error_schema}}}
        operation = {'operationId': name, 'summary': binding['summary'], 'security': [{'sessionCookie': []}] if cookie else [], 'parameters': parameters, 'responses': responses, 'x-gym-input-schema': 'schema.json#/$defs/' + binding['input'], 'x-gym-error-codes': binding['errors']}
        if name == 'auth_logout':
            operation['security'].append({})
        if binding['method'] != 'get' and source.get('properties'):
            for field in path_fields:
                source['properties'].pop(field)
                source['required'].remove(field)
            operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': external_refs(source)}}}
        paths.setdefault(binding['path'], {})[binding['method']] = operation
        if binding['mcp_tool']:
            output = {'type': 'object', 'oneOf': [
                {'type': 'object', 'properties': {'ok': {'const': True}, 'data': {'$ref': '#/$defs/' + binding['output']}}, 'required': ['ok','data'], 'additionalProperties': False},
                {'type': 'object', 'properties': {'ok': {'const': False}, 'error': {'allOf': [{'$ref': '#/$defs/Error'}, {'properties': {'code': {'enum': binding['errors']}}}]}}, 'required': ['ok','error'], 'additionalProperties': False}]}
            tools.append({'name': binding['mcp_tool'], 'description': binding['summary'], 'inputSchema': bundled(definitions[binding['input']], definitions), 'outputSchema': bundled(output, definitions), 'annotations': {'readOnlyHint': binding['method'] == 'get', 'idempotentHint': True, 'destructiveHint': name == 'publish_approved', 'openWorldHint': False}, '_meta': {'gym.requiredScope': binding['scope'], 'gym.operationId': name}})
    api = {'openapi': '3.1.2', 'jsonSchemaDialect': 'https://json-schema.org/draft/2020-12/schema', 'info': {'title': 'Gym API', 'version': '1.0.0'}, 'servers': [{'url': '/'}], 'paths': paths, 'components': {'securitySchemes': {'sessionCookie': {'type': 'apiKey', 'in': 'cookie', 'name': '__Host-gym_session'}}}}
    return {'openapi.json': api, 'mcp-tools.json': {'tools': sorted(tools, key=lambda t: t['name'])}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for name, value in artifacts().items():
        # Производные артефакты компактны; проверяй исходные schema/bindings.
        output = json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n'
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text(encoding='utf-8') != output:
                raise SystemExit(f'{name}: пересобери python -m contracts.generate')
        else:
            path.write_text(output, encoding='utf-8', newline='\n')
    print('OpenAPI/MCP: ' + ('расхождений нет' if args.check else 'артефакты обновлены'))


if __name__ == '__main__':
    main()
