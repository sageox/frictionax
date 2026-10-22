#!/usr/bin/env python3
"""A fictional cobra-style CLI named `acme`, used only by the smoke test.

It prints cobra-shaped help and errors, suggests corrections the way frictionax
does (a tiny catalog, then Levenshtein over command paths), hides a feature-gated
`cart` group unless ACME_FEATURE_CART=1, and refuses to run anything outside the
replay sandbox, which is how the smoke test proves the sandbox is applied.

Two commands exist to test the replay guards: `gc` refuses to run (exit 97), so
any mutating command that slips past the deny check is visible, and `follow`
never returns and leaves a detached helper holding stdout open (it exits on its
own after a short sleep), so timeout cleanup that waits for pipe EOF would hang.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

GLOBAL_FLAGS = {'--verbose': False}
GLOBAL_SHORTS = {'-v': '--verbose'}
TREE: dict[str, dict] = {
    '': {'short': 'acme manages plans and sessions', 'flags': {}},
    'status': {'short': 'Show status', 'flags': {'--json': False}},
    'sync': {'short': 'Sync local state', 'flags': {}},
    'login': {'short': 'Log in', 'flags': {'--token': True}},
    'upgrade': {'short': 'Upgrade acme', 'flags': {}},
    'gc': {'short': 'Reclaim disk space (deletes caches)', 'flags': {'--force': False}},
    'follow': {'short': 'Stream new events until interrupted', 'flags': {}},
    'log': {'short': 'Record a note', 'flags': {'--content': True, '--json': False}},
    'plan': {'short': 'Manage plans', 'flags': {}},
    'plan list': {
        'short': 'List plans',
        'flags': {'--json': False, '--all': False},
        'examples': ['acme plan list --limit 5', 'acme plan list --json'],
    },
    'plan view': {'short': 'View a plan', 'flags': {'--json': False}, 'args': '<slug>'},
    'plan lint': {'short': 'Lint a plan file', 'flags': {'--file': True}},
    'session': {'short': 'Manage sessions', 'flags': {}},
    'session list': {'short': 'List sessions', 'flags': {'--limit': True, '--json': False, '--since': True}},
    'session status': {'short': 'Show the current session', 'flags': {}},
    'skills': {'short': 'Manage skills', 'flags': {}},
    'skills list': {'short': 'List skills', 'flags': {}},
    'cart': {'short': 'Shopping cart (preview)', 'flags': {}, 'feature': 'ACME_FEATURE_CART'},
    'cart list': {'short': 'List cart items', 'flags': {}, 'feature': 'ACME_FEATURE_CART'},
}
CATALOG = {'stats': ('status', True), 'plans': ('plan-list', True), 'tidy': ('gc', True)}


def enabled(path: str) -> bool:
    if path not in TREE:
        return False
    feature = TREE[path].get('feature')
    return not feature or os.environ.get(feature) == '1'


def children(path: str) -> list[str]:
    prefix = f'{path} ' if path else ''
    names = []
    for key in TREE:
        if key.startswith(prefix) and key != path and ' ' not in key[len(prefix) :] and enabled(key):
            names.append(key[len(prefix) :])
    return sorted(names)


def help_text(path: str) -> str:
    node = TREE[path]
    kids = children(path)
    full = ' '.join(['acme'] + path.split())
    lines = [node['short'], '', 'Usage:']
    if kids:
        lines.append(f'  {full} [command]')
    else:
        lines.append(f'  {full} {node.get("args", "")} [flags]'.replace('  [', ' [').rstrip())
    if node.get('examples'):
        lines += ['', 'Examples:'] + [f'  {example}' for example in node['examples']]
    if kids:
        lines += ['', 'Available Commands:']
        width = max(len(k) for k in kids) + 4
        for kid in kids:
            lines.append(f'  {kid.ljust(width)}{TREE[(path + " " + kid).strip()]["short"]}')
    lines += ['', 'Flags:', f'  -h, --help   help for {path.split()[-1] if path else "acme"}']
    for name, takes in node['flags'].items():
        lines.append(f'      {name}{" string" if takes else ""}   {name.strip("-")} option')
    lines += ['', 'Global Flags:', '  -v, --verbose   verbose output', '']
    if kids:
        lines.append(f'Use "{full} [command] --help" for more information about a command.')
    return '\n'.join(lines) + '\n'


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[len(b)]


def suggest(word: str) -> str | None:
    best, best_distance = None, 3
    for path in sorted(p for p in TREE if p and enabled(p)):
        distance = levenshtein(word, path)
        if distance < best_distance:
            best, best_distance = path, distance
    return best


def fail(message: str, suggestion: str | None = None) -> int:
    print(f'Error: {message}', file=sys.stderr)
    if suggestion:
        print(f'Did you mean?\n    acme {suggestion}', file=sys.stderr)
    return 1


def assert_sandboxed(help_only: bool) -> None:
    problems = []
    if 'friction-sandbox-' not in os.environ.get('HOME', ''):
        problems.append('HOME')
    if any(Path.cwd().iterdir()):
        problems.append('cwd not empty')
    if not help_only and os.environ.get('ACME_TELEMETRY') != 'off':
        problems.append('ACME_TELEMETRY')
    if sys.stdin is not None and sys.stdin.isatty():
        problems.append('stdin')
    if problems:
        print('sandbox violated: ' + ', '.join(problems), file=sys.stderr)
        sys.exit(99)


def main(args: list[str]) -> int:
    help_requested = '--help' in args or '-h' in args
    assert_sandboxed(help_requested or '--version' in args or not args)
    if args == ['--version']:
        print('acme version 2.4.0')
        return 0
    joined = ' '.join(args)
    if joined in CATALOG:
        target, auto = CATALOG[joined]
        if auto:
            print(f'-> Correcting to: acme {target}', file=sys.stderr)
            return main(target.split())
    path = ''
    skip = False
    for token in args:
        if skip:
            skip = False
            continue
        if token.startswith('-'):
            name = token.split('=', 1)[0]
            takes = TREE[path]['flags'].get(name, False)
            skip = takes and '=' not in token
            continue
        kids = children(path)
        if not kids:
            continue  # leaf commands take positional arguments
        if token in kids:
            path = f'{path} {token}'.strip()
            continue
        parent = ' '.join(['acme'] + path.split())
        return fail(f'unknown command "{token}" for "{parent}"', suggest(token))
    if help_requested or (not args):
        print(help_text(path), end='')
        return 0
    for token in args:
        if token.startswith('-'):
            name = token.split('=', 1)[0]
            if name in ('--help', '-h'):
                continue
            if name not in TREE[path]['flags'] and name not in GLOBAL_FLAGS and name not in GLOBAL_SHORTS:
                return fail(f'unknown flag: {name}')
    if children(path):
        print(help_text(path), end='')
        return 0
    if path == 'gc':
        print('Error: gc executed during a replay; the deny check let a mutating command through', file=sys.stderr)
        return 97
    if path == 'follow':
        # a detached helper inherits stdout, so the pipe stays open after this process is killed
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'], start_new_session=True)
        time.sleep(120)
    print('ok')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
