#!/usr/bin/env python3
"""End-to-end smoke test for the friction-analysis skill. Stdlib only.

    python3 -I skills/friction-analysis/tests/smoke_test.py

Runs the whole pipeline on a synthetic export for a fictional CLI named `acme`:
against a fake acme binary (inventory from --help, sandboxed exec replay,
feature-gated inventory, source scan, judgment decisions, catalog apply), with
--skip-replay and no binary at all, and with a hand-written inventory. Asserts the
bucket counts, the AUTO rule, the replay guards (mutating commands and catalog
auto-correct targets never run, proxy variables stay reserved, a hung CLI cannot
stall the run), that no fixture PII reaches any output, and that the report is
self-contained with at most 100 visible words per view.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
DATA = SKILL / 'tests' / 'testdata'
ANALYZE = SKILL / 'scripts' / 'analyze.py'

EXPECTED_BUCKETS = {
    'cli-taught': 10,
    'word-split': 5,
    'catalog-miss': 4,
    'bad-fuzzy': 2,
    'alias': 11,
    'flag-drift': 6,
    'missing-feature': 2,
    'gated': 2,
    'fixed': 8,
    'noise': 3,
}
TOTAL_EVENTS = 53
# `fake_acme.py follow` hangs and leaves a detached helper holding stdout for 40s; a
# 2s replay timeout plus bounded cleanup must return long before the helper exits
HANG_TIMEOUT_SECONDS = 2.0
HANG_BUDGET_SECONDS = 20.0
EXPECTED_ENTRIES = {
    ('plan show', 'plan view'): 'AUTO',
    ('skill', 'skills'): 'AUTO',
    ('stats', 'status'): 'AUTO',
    ('plans', 'plan'): 'AUTO',
    ('plan --list', 'plan list'): 'AUTO',
    ('session statu', 'session status'): 'AUTO',
    ('update', 'upgrade'): 'ASK',
}
# every raw value planted in the fixture export; none may appear in any output
PLANTED = (
    'person-a',
    'person-b',
    'example.com',
    'tm_8f3k2j9x01',
    'AKIAIOSFODNN7EXAMPLE',
    'billing page',
    'release checklist',
    'vacation-2023',
    'q3-roadmap',
    'my-plan',
    'notes.md',
    'draft.md',
    '/Users/person',
)
VIEWS = ('v1', 'v2', 'v3', 'v4', 'v5')
MAX_VIEW_WORDS = 100

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(('ok    ' if condition else 'FAIL  ') + message)
    if not condition:
        failures.append(message)


def analyze(*args: str, expect_failure: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([sys.executable, '-I', str(ANALYZE), *args], capture_output=True, text=True, timeout=600)
    if result.returncode != 0 and not expect_failure:
        print(result.stdout, result.stderr, sep='\n')
    return result


class ViewWords(HTMLParser):
    """Counts visible words per tab view: skips screen-reader-only text and hidden inspector entries."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.view: str | None = None
        self.view_depth = 0
        self.words: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get('class') or '').split()
        hidden = (
            bool(self.stack and self.stack[-1][1])
            or 'sr' in classes
            or tag in ('style', 'title')
            or ('e' in classes and 'e0' not in classes)
        )
        if tag in ('br', 'meta', 'input', 'path', 'line', 'rect', 'circle'):
            return
        self.stack.append((tag, hidden))
        if tag == 'section' and attributes.get('id') in VIEWS:
            self.view, self.view_depth = attributes['id'], len(self.stack)

    def handle_endtag(self, tag: str) -> None:
        if tag in ('br', 'meta', 'input', 'path', 'line', 'rect', 'circle'):
            return
        if self.stack:
            if self.view and len(self.stack) == self.view_depth:
                self.view = None
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.view and not (self.stack and self.stack[-1][1]):
            self.words[self.view] = self.words.get(self.view, 0) + sum(
                1 for w in data.split() if re.search(r'[A-Za-z]', w)
            )


def outputs(work: Path) -> list[Path]:
    return [
        p for p in work.rglob('*') if p.is_file() and 'input' not in p.relative_to(work).parts and p.name != 'run.json'
    ]


def check_report(work: Path, label: str) -> None:
    report = (work / 'report.html').read_text(encoding='utf-8')
    check(report.count('<section class="view"') == 5, f'{label}: report has five views')
    external_url = re.search(r'url\((?!#)', report)  # url(#id) points inside the document
    check(
        '<script' not in report and '://' not in report and '@import' not in report and not external_url,
        f'{label}: report is self-contained (no scripts, URLs, imports)',
    )
    check('<details class="notes">' in report, f'{label}: implementation notes are collapsed')
    check(str(Path.home()) not in report, f'{label}: report carries no local home path')
    counter = ViewWords()
    counter.feed(report)
    for view in VIEWS:
        words = counter.words.get(view, 0)
        check(words <= MAX_VIEW_WORDS, f'{label}: view {view} has {words} visible words (<= {MAX_VIEW_WORDS})')
    leaked = sorted(
        {
            value
            for path in outputs(work)
            for value in PLANTED
            if value in path.read_text(encoding='utf-8', errors='replace')
        }
    )
    check(not leaked, f'{label}: no planted raw value reaches any output' + (f' (found {leaked})' if leaked else ''))


def full_run(work: Path) -> None:
    binary = f'{sys.executable} -I {DATA / "fake_acme.py"}'
    result = analyze(
        '--work',
        str(work),
        '--export',
        str(DATA / 'acme-export.csv'),
        '--cli',
        'acme',
        '--binary',
        binary,
        '--catalog',
        str(DATA / 'acme-catalog.json'),
        '--source',
        str(DATA / 'acme-src'),
        '--config',
        str(DATA / 'acme-config.json'),
        '--exec',
        '--env-file',
        str(DATA / 'acme-replay.env'),
        '--feature-env-file',
        str(DATA / 'acme-features.env'),
    )
    check(result.returncode == 0, 'full run: first pass exits 0')
    classified = json.loads((work / 'classified.json').read_text())
    judgment = sorted(s['key'] for s in classified['shapes'] if s['needs_judgment'])
    check(judgment == ['<path>', 'repo', 'repo list', 'update'], f'full run: residue left for judgment is {judgment}')

    # second pass: the AI coworker's decisions, then only recommend + render
    (work / 'decisions.json').write_text((DATA / 'acme-decisions.json').read_text())
    result = analyze('--work', str(work), '--stages', 'recommend,render')
    check(result.returncode == 0, 'full run: decisions pass exits 0')
    print('      ' + '\n      '.join(result.stdout.strip().splitlines()))
    rec = json.loads((work / 'recommendations.json').read_text())
    buckets = {name: values['events'] for name, values in rec['buckets'].items()}
    check(buckets == EXPECTED_BUCKETS, f'full run: buckets {buckets}')
    check(not rec['decision_problems'] and not rec['judgment'], 'full run: every decision applied, no residue')
    entries = {(e['typed'], e['target']): e['mode'] for e in rec['catalog_entries']}
    check(entries == EXPECTED_ENTRIES, f'full run: catalog entries and AUTO/ASK modes {entries}')
    check(
        all(e['verified'] for e in rec['catalog_entries']),
        'full run: every proposed entry lands on its target in the frictionax simulation',
    )
    cleanup = {(row['pattern'], row['action']) for row in rec['catalog_cleanup']}
    check(
        cleanup == {('plans', 'retarget'), ('skills', 'delete'), ('tidy', 'suggest-only')},
        f'full run: catalog cleanup {cleanup}',
    )
    check(
        [t['pattern'] + '->' + t['target'] for t in rec['vocabulary']['tokens']] == ['--last->--limit'],
        'full run: --last becomes a token fix',
    )
    check(
        rec['vocabulary']['add'].get('plan list') == ['--limit', '--since'],
        'full run: plan list gains the shared list flags',
    )
    hints = {row['ref']: row['hint'] for row in rec['strings_to_fix']}
    check(
        hints.get('acme notes sync') == 'acme sync' and len(rec['strings_to_fix']) == 4,
        f'full run: strings to fix {hints}',
    )
    replay = json.loads((work / 'replay.json').read_text())
    check(
        not any(r.get('exit') == 99 for r in replay.values()),
        'full run: every replay ran inside the sandbox (fake CLI saw a throwaway HOME)',
    )
    check(
        replay.get('login --token <id>', {}).get('result') == 'skipped',
        'full run: destructive command was not executed',
    )
    check(
        not any(r.get('exit') == 97 for r in replay.values()) and replay.get('tidy', {}).get('result') == 'skipped',
        'full run: a catalog auto-correct to a mutating command (tidy -> gc) was not executed',
    )
    check(replay.get('update', {}).get('result') == 'skipped', 'full run: a mutating unknown word was not executed')
    check(replay.get('follow', {}).get('result') == 'timeout', 'full run: the hung command was recorded as a timeout')
    corpus = json.loads((work / 'corpus.json').read_text())
    check(
        len(corpus) == len(classified['shapes']) and all(row['expected'] != 'needs-judgment' for row in corpus),
        'full run: corpus has an expected outcome for every shape',
    )
    check_report(work, 'full run')
    apply_run(work)


def apply_run(work: Path) -> None:
    """The reviewed apply step merges the patch into a copy of the catalog, never the fixture."""
    target = work / 'catalog-copy.json'
    shutil.copyfile(DATA / 'acme-catalog.json', target)
    refused = analyze('--work', str(work), '--stages', 'apply', expect_failure=True)
    check(refused.returncode != 0, 'apply: refuses to run without --apply-catalog')
    result = analyze('--work', str(work), '--stages', 'apply', '--apply-catalog', str(target))
    check(result.returncode == 0, 'apply: exits 0')
    merged = json.loads(target.read_text())
    by_pattern = {entry['pattern']: entry for entry in merged['commands']}
    check(
        'skills' not in by_pattern
        and by_pattern['plans']['target'] == 'plan'
        and by_pattern['tidy']['auto_execute'] is False
        and by_pattern[r'^plan show(\s.*)?$']['target'] == r'plan view$1',
        'apply: entries added, stale entry deleted, dead target retargeted, mutating auto entry demoted',
    )
    check(
        json.loads((DATA / 'acme-catalog.json').read_text()) != merged,
        'apply: wrote the copy it was given, fixture untouched',
    )


def proxy_run(work: Path) -> None:
    result = analyze(
        '--work', str(work), '--export', str(DATA / 'acme-export.csv'), '--cli', 'acme',
        '--binary', f'{sys.executable} -I {DATA / "fake_acme.py"}', '--exec',
        '--env-file', str(DATA / 'acme-replay.env'), '--env', 'HTTPS_PROXY=http://proxy.invalid:8080',
        expect_failure=True,
    )  # fmt: skip
    check(
        result.returncode != 0 and 'owned by the sandbox' in result.stderr and not (work / 'replay.json').exists(),
        'proxy: an env file cannot reroute the sandbox proxy',
    )


def inventory_run(work: Path, inventory: Path) -> None:
    """A hand-written inventory never runs anything, so --skip-replay must keep using it on later passes."""
    result = analyze(
        '--work', str(work), '--export', str(DATA / 'acme-export.csv'), '--cli', 'acme', '--skip-replay',
        '--inventory', str(inventory), '--catalog', str(DATA / 'acme-catalog.json'),
    )  # fmt: skip
    check(result.returncode == 0, 'hand-written inventory: first pass exits 0')
    first = json.loads((work / 'recommendations.json').read_text())
    result = analyze('--work', str(work), '--stages', 'recommend,render')
    second = json.loads((work / 'recommendations.json').read_text())
    run = json.loads((work / 'run.json').read_text())
    check(
        result.returncode == 0
        and first['catalog_cleanup'] == second['catalog_cleanup'] != []
        and 'supplied command inventory' in run['method_line'],
        'hand-written inventory: the recommend,render pass still uses it',
    )


def unit_checks() -> None:
    """Small direct checks of behavior that has no natural end-to-end trigger in the fixture."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SKILL / 'scripts'))  # trusted: the skill's own modules
    from friction_analysis.classify import Context
    from friction_analysis.config import Config
    from friction_analysis.frictionsim import Catalog
    from friction_analysis.inventory import Inventory, resolve
    from friction_analysis.recommend import verify_entries
    from friction_analysis.sandbox import Sandbox, resolve_binary

    binary = resolve_binary(f'{sys.executable} -I {DATA / "fake_acme.py"}')
    with Sandbox(binary, {'ACME_TELEMETRY': 'off'}, HANG_TIMEOUT_SECONDS) as sandbox:
        started = time.monotonic()
        hung = sandbox.run(['follow'])
        elapsed = time.monotonic() - started
    check(
        hung.exit_code is None and elapsed < HANG_BUDGET_SECONDS,
        f'unit: a hung CLI whose helper holds stdout returns after the timeout ({elapsed:.1f}s < {HANG_BUDGET_SECONDS:g}s)',
    )

    inventory = Inventory.from_json(
        {'cli': 'acme', 'commands': {'': {'children': ['secret']}, 'secret': {'hidden_from_help': True}}}
    )
    check(
        resolve(inventory, ['secret', '--anything']).unknown_flags == [],
        'unit: a command with unreadable help does not reject its flags',
    )
    check(Config().is_destructive(['plan', 'create']), 'unit: create counts as a mutating verb')
    # frictionax matches regexes on the normalized input but substitutes from the raw input;
    # an entry that only matches the normalized form leaves a literal $1 and must not verify
    entry = {'pattern': r'^show (.*) --json$', 'target': 'view $1', 'has_regex': True, 'auto_execute': True,
             'confidence': 0.9}  # fmt: skip
    proposals = [{'entry': entry, 'target': 'view', 'typed': 'show', 'shapes': ['k']}]
    records = [{'key': 'k', 'argv': ['show', '--json', 'list'], 'unknown_word': 'show'}]
    verify_entries(proposals, records, Context(cli='acme', config=Config(), catalog=Catalog(None, 'acme')))
    check(proposals[0]['verified'] is False, 'unit: an entry that would run a literal $1 is not verified')


def static_run(work: Path) -> None:
    result = analyze(
        '--work',
        str(work),
        '--export',
        str(DATA / 'acme-export.csv'),
        '--cli',
        'acme',
        '--skip-replay',
        '--catalog',
        str(DATA / 'acme-catalog.json'),
        '--config',
        str(DATA / 'acme-config.json'),
    )
    check(result.returncode == 0, 'skip-replay run: exits 0 without any binary')
    rec = json.loads((work / 'recommendations.json').read_text())
    total = sum(values['events'] for values in rec['buckets'].values())
    check(total == TOTAL_EVENTS, f'skip-replay run: all {TOTAL_EVENTS} events accounted for ({total})')
    check(
        rec['buckets'].get('word-split', {}).get('events') == 5,
        'skip-replay run: word-split found from error text alone',
    )
    check(
        not (work / 'replay.json').exists() and not (work / 'inventory.json').exists(),
        'skip-replay run: nothing executed',
    )
    check_report(work, 'skip-replay run')


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='friction-smoke-') as root:
        full_run(Path(root) / 'full')
        static_run(Path(root) / 'static')
        proxy_run(Path(root) / 'proxy')
        inventory_run(Path(root) / 'inventory', Path(root) / 'full' / 'inventory.json')
        unit_checks()
        if '--keep' in sys.argv:  # keep the work directories, e.g. to open the reports
            shutil.copytree(root, sys.argv[sys.argv.index('--keep') + 1], dirs_exist_ok=True)
    print(f'\n{"FAILED" if failures else "PASSED"}: {len(failures)} failing checks')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
