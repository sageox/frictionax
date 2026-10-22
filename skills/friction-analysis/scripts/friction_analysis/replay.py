"""Exec replay: run every anonymized shape against the current binary in the sandbox.

This is the only stage that executes real commands, so it is opt-in (`--exec`)
and requires an env file of side-effect switches the user approved. A shape is
never executed when its resolved command, its unknown word, or the target an
auto-executing catalog entry would rewrite it to contains a mutating verb; it
keeps its static outcome.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import Config
from .frictionsim import Catalog
from .inventory import Inventory, resolve
from .sandbox import Sandbox, warn
from .util import ARG, DATE, DURATION, EMAIL, ID, NUMBER, PATH, TEXT, URL, parse_error

FILL = {
    PATH: 'x.txt',
    URL: 'http://127.0.0.1:9/x',
    EMAIL: 'person@example.com',
    ID: 'id0000000000',
    NUMBER: '1',
    DURATION: '1d',
    TEXT: 'x',
    ARG: 'x',
    DATE: '2026-01-01',
}
AUTO_TEXT = re.compile(r'^->\s*Correcting to:\s*(?P<target>.+)$', re.MULTILINE)
SUGGEST_TEXT = re.compile(r'Did you mean(?: this)?\?\s*\n\s+(?P<target>\S.*)$', re.MULTILINE)


def concrete_argv(shape: dict[str, Any]) -> list[str]:
    """Fill placeholders with inert values; re-join a word-split path into one argv element."""
    argv = []
    for token in shape['argv']:
        if '=' in token and token.startswith('-'):
            name, _, value = token.partition('=')
            argv.append(f'{name}={FILL.get(value, value)}')
        else:
            argv.append(FILL.get(token, token))
    if shape.get('joined'):
        start, length = shape['joined_at'], shape['joined']
        argv[start : start + length] = [' '.join(argv[start : start + length])]
    return argv


def parse_outcome(code: int | None, stdout: str, stderr: str, cli: str, config: Config) -> dict[str, Any]:
    text = stderr + '\n' + stdout
    outcome: dict[str, Any] = {'exit': code}
    if code is None:
        outcome['result'] = 'timeout'
        return outcome
    corrected = None
    suggested = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith('{') and ('"_corrected"' in line or '"_suggestion"' in line):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            corrected = corrected or (payload.get('_corrected') or {}).get('now')
            suggested = suggested or (payload.get('_suggestion') or {}).get('try')
    match = AUTO_TEXT.search(text)
    corrected = corrected or (match.group('target').strip() if match else None)
    match = SUGGEST_TEXT.search(text)
    suggested = suggested or (match.group('target').strip() if match else None)
    error_line = next((line.strip() for line in stderr.splitlines() if line.strip()), '')
    kind, token = parse_error(
        error_line.removeprefix('Error: '), config.unknown_command_patterns, config.unknown_flag_patterns
    )
    if kind == 'other':
        for line in text.splitlines():
            kind, token = parse_error(
                line.strip().removeprefix('Error: '), config.unknown_command_patterns, config.unknown_flag_patterns
            )
            if kind != 'other':
                break
    for name, value in (('corrected', corrected), ('suggested', suggested)):
        if value:
            outcome[name] = strip_cli(value, cli)
    if code == 0 and kind == 'other':
        outcome['result'] = 'corrected' if corrected else 'works'
    elif kind in ('unknown-command', 'unknown-flag'):
        outcome['result'] = kind
        outcome['token'] = token
    else:
        # parsed, then failed for another reason (no auth, no network, missing arg)
        outcome['result'] = 'other-error'
    outcome['error_line'] = error_line[:160]
    return outcome


def strip_cli(command: str, cli: str) -> str:
    words = command.split()
    if words and words[0] == cli:
        words = words[1:]
    return ' '.join(words)


def denied(shape: dict[str, Any], inventory: Inventory | None, config: Config, catalog: Catalog | None) -> bool:
    """Skip a shape whose command, unknown word, or catalog auto-correct target mutates anything."""
    words = [token for token in shape['argv'] if not token.startswith('-')]
    if inventory is not None:
        resolution = resolve(inventory, shape['argv'], shape.get('joined', 0), shape.get('joined_at', 0))
        words = resolution.path + (resolution.unknown_word or '').split()
    if config.is_destructive(words):
        return True
    if catalog is not None:
        text = ' '.join(shape['argv'])
        entry = catalog.lookup(text)
        # any auto-executing entry counts, whatever its confidence: the CLI decides, not us
        if entry is not None and entry.get('auto_execute'):
            return config.is_destructive(catalog.apply(entry, text).split())
    return False


def run(
    shapes: list[dict[str, Any]],
    sandbox: Sandbox,
    cli: str,
    inventory: Inventory | None,
    config: Config,
    catalog: Catalog | None,
    dry_run: bool = False,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    executed = 0
    for shape in shapes:
        argv = concrete_argv(shape)
        if denied(shape, inventory, config, catalog):
            results[shape['key']] = {'result': 'skipped', 'reason': 'mutating verb'}
            continue
        if dry_run:
            print(' '.join(json.dumps(arg) if ' ' in arg else arg for arg in [cli] + argv))
            continue
        run_result = sandbox.run(argv)
        executed += 1
        results[shape['key']] = parse_outcome(run_result.exit_code, run_result.stdout, run_result.stderr, cli, config)
    if not dry_run:
        warn(
            f'replayed {executed} shapes in the sandbox; {sum(1 for r in results.values() if r["result"] == "skipped")} skipped'
        )
    return results
