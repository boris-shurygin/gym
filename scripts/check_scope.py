"""Выбери проверки по изменённым файлам; неизвестный диапазон проверяй полностью."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

JOBS = ('contracts', 'api', 'ydb', 'infra', 'web', 'browser')
UI_FILES = {'web/src/ui/FinishExerciseButton.tsx', 'web/src/style.css', 'web/src/demo-program.ts'}


def documentation(path):
    return path.endswith('.md') or path.startswith(('.agents/', '.codex/', 'docs/'))


def classify(paths, *, full=False):
    plan = {name: False for name in (*JOBS, 'smoke', 'deploy')}
    unknown = paths is None
    for path in paths or []:
        if path.startswith('docs/examples/') and path.endswith('.json'):
            plan['contracts'] = True
            continue
        if documentation(path):
            continue
        if path.startswith(('api/', 'db/', 'contracts/', 'infra/')) or path.startswith('web/') and not path.startswith('web/tests/'):
            plan['deploy'] = True
        if path.startswith(('contracts/', 'tests/contracts/')):
            for name in ('contracts', 'api', 'ydb', 'infra', 'web', 'browser'):
                plan[name] = True
        elif path.startswith(('api/', 'db/', 'tests/api/', 'tests/ydb/')):
            for name in ('contracts', 'api', 'ydb', 'infra', 'web', 'browser'):
                plan[name] = True
        elif path.startswith(('infra/', 'tests/infra/', 'tests/release/', 'tests/build/')):
            plan['infra'] = True
        elif path.startswith('web/'):
            plan['web'] = True
            if path in UI_FILES or path.startswith(('web/public/assets/', 'web/public/icons/')):
                plan['smoke'] = True
            else:
                plan['browser'] = True
        else:
            unknown = True
            plan['deploy'] = True
    if full or unknown:
        plan.update({name: True for name in JOBS})
        if unknown:
            plan['deploy'] = True
    if plan['browser']:
        plan['smoke'] = False
    plan['profile'] = 'full' if full or unknown else 'integration' if plan['browser'] else 'ui' if plan['web'] else 'scoped' if any(plan[name] for name in JOBS) else 'docs'
    return plan


def paths_between(base, head, *, worktree=False):
    def git(*args):
        return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL).decode('utf-8').split('\0')
    try:
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', base or '') or not re.fullmatch(r'[A-Za-z0-9_./-]+', head or ''):
            return None
        if set(base) == {'0'}:
            return None
        paths = git('diff', '--name-only', '--no-renames', '-z', base, head, '--')
        if worktree:
            paths += git('diff', '--name-only', '--no-renames', '-z', head, '--')
            paths += git('ls-files', '--others', '--exclude-standard', '-z')
        return sorted(set(filter(None, paths)))
    except (subprocess.CalledProcessError, UnicodeError):
        return None


def verify_results(needs):
    if needs.get('changes', {}).get('result') != 'success':
        raise ValueError('Область изменений не подтверждена')
    plan = json.loads(needs['changes']['outputs']['plan'])
    for name in JOBS:
        if type(plan.get(name)) is not bool:
            raise ValueError('Неполный план проверок')
        result = needs.get(name, {}).get('result')
        if result != 'success' and (plan[name] or result != 'skipped'):
            raise ValueError('Не выполнена необходимая проверка: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['detect', 'verify'])
    parser.add_argument('--base')
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--worktree', action='store_true')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--plan-file')
    args = parser.parse_args()
    if args.operation == 'verify':
        verify_results(json.loads(os.environ['GYM_CHECK_NEEDS']))
        print('Required checks passed')
        return
    paths = [] if args.full and not args.base else paths_between(args.base, args.head, worktree=args.worktree)
    plan = classify(paths, full=args.full)
    encoded = json.dumps(plan, separators=(',', ':'))
    print(encoded)
    if args.plan_file:
        source_sha = os.environ['GITHUB_SHA']
        if not re.fullmatch(r'[a-f0-9]{40}', source_sha):
            raise ValueError('Неизвестный source SHA')
        path = Path(args.plan_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'format': 1, 'source_sha': source_sha, 'plan': plan}) + '\n', encoding='utf-8')
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
            output.write('plan=' + encoded + '\n')
            for key, value in plan.items():
                output.write(f'{key}={str(value).lower()}\n')


if __name__ == '__main__':
    main()
