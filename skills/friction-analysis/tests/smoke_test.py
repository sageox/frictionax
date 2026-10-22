#!/usr/bin/env python3
"""End-to-end smoke test for the friction-analysis skill. Stdlib only.

    python3 -I skills/friction-analysis/tests/smoke_test.py

Runs the whole pipeline twice on a synthetic export for a fictional CLI named
`acme`: once against a fake acme binary (inventory from --help, sandboxed exec
replay, feature-gated inventory, source scan, judgment decisions), and once with
--skip-replay and no binary at all. Asserts the bucket counts, the AUTO rule,
that no fixture PII reaches any output, that the sandbox was applied, and that
the report is self-contained with at most 100 visible words per view.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
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
    'fixed': 6,
    'noise': 3,
}
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


def analyze(*args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([sys.executable, '-I', str(ANALYZE), *args], capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
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
    check(cleanup == {('plans', 'retarget'), ('skills', 'delete')}, f'full run: catalog cleanup {cleanup}')
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
    corpus = json.loads((work / 'corpus.json').read_text())
    check(
        len(corpus) == len(classified['shapes']) and all(row['expected'] != 'needs-judgment' for row in corpus),
        'full run: corpus has an expected outcome for every shape',
    )
    check_report(work, 'full run')


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
    check(total == 51, f'skip-replay run: all 51 events accounted for ({total})')
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
        if '--keep' in sys.argv:  # keep the work directories, e.g. to open the reports
            shutil.copytree(root, sys.argv[sys.argv.index('--keep') + 1], dirs_exist_ok=True)
    print(f'\n{"FAILED" if failures else "PASSED"}: {len(failures)} failing checks')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
