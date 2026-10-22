"""Ingest a friction export, anonymize every invocation into an argv shape, dedupe.

Redaction rule: keep command-path words and flag names; replace every other token
with a typed placeholder (<path>, <id>, <n>, <text>, ...). The export itself is
untrusted data: it is copied read-only into the work directory and parsed as text,
never executed or imported. Nothing downstream ever sees a raw value.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import stat
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .inventory import Inventory, Node
from .util import (
    ARG,
    COMMAND_WORD,
    DATE,
    DURATION,
    EMAIL,
    ID,
    LONG_FLAG,
    NUMBER,
    PATH,
    PLACEHOLDERS,
    SHORT_FLAG,
    TEXT,
    URL,
    display,
    is_flag,
    parse_error,
)

ALL_PLACEHOLDERS = PLACEHOLDERS

FIELD_ALIASES = {
    'input': ('input_redacted', 'input', 'pattern', 'argv', 'command_line'),
    'error': ('error_msg', 'error', 'message', 'error_message'),
    'kind': ('kind', 'failure_kind'),
    'actor': ('actor',),
    'agent_type': ('agent_type', 'agent'),
    'version': ('client_version', 'version', 'cli_version', 'latest_version'),
    'timestamp': ('timestamp', 'ts', 'created_at', 'last_seen', 'first_seen'),
    'weight': ('total_count', 'count'),
    'human_count': ('human_count',),
    'agent_count': ('agent_count',),
}

EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
UUID_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
HEX_RE = re.compile(r'\b[0-9a-f]{12,}\b', re.IGNORECASE)
PREFIXED_ID_RE = re.compile(r'\b[a-z]{2,8}_(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{6,}\b')

# privacy detectors run on raw text; only per-category counts leave this module
PRIVACY_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('home-path', re.compile(r'(/Users/|/home/|[A-Za-z]:\\Users\\)[^/\\\s]+')),
    ('absolute-path', re.compile(r'(?:^|\s|=)(?:/[^\s/]+/\S*|[A-Za-z]:\\\S+|~/\S*)')),
    ('email', EMAIL_RE),
    ('url', re.compile(r'\b[a-z][a-z0-9+.-]*://\S+', re.IGNORECASE)),
    ('id', re.compile('|'.join(p.pattern for p in (UUID_RE, HEX_RE, PREFIXED_ID_RE)), re.IGNORECASE)),
    (
        'secret-like',
        re.compile(
            r'gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}'
            r'|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY'
        ),
    ),
)
PRIVACY_LABELS = {
    'home-path': 'Home paths (OS usernames)',
    'absolute-path': 'Absolute file paths',
    'email': 'Email addresses',
    'url': 'URLs',
    'id': 'Account or record IDs',
    'secret-like': 'Secret-like tokens',
    'free-text': 'Free text (prompts, messages)',
    'multi-line': 'Multi-line pasted text',
    'near-limit': 'Inputs near the truncation limit',
    'any-value': 'Any argument value',
}


# ---------------------------------------------------------------------------
# ingest


def copy_export(source: Path, work: Path) -> Path:
    """Work on a read-only copy in its own directory, never on the original."""
    target_dir = work / 'input'
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ('export' + (source.suffix.lower() or '.csv'))
    if target.exists():
        os.chmod(target, stat.S_IWUSR | stat.S_IRUSR)
    shutil.copyfile(source, target)
    os.chmod(target, stat.S_IRUSR)
    return target


def pick(row: dict[str, Any], field: str) -> Any:
    for name in FIELD_ALIASES[field]:
        if name in row and row[name] not in (None, ''):
            return row[name]
    return None


def read_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    if suffix in ('.jsonl', '.ndjson'):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suffix == '.json':
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ('events', 'patterns', 'rows', 'data'):
                if isinstance(data.get(key), list):
                    return data[key]
            raise ValueError('JSON export must be a list or have an events/patterns list')
        return list(data)
    return list(csv.DictReader(text.splitlines(keepends=True)))


def parse_time(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)) or re.fullmatch(r'\d{9,13}(\.\d+)?', str(value)):
        seconds = float(value)
        return datetime.fromtimestamp(seconds / 1000 if seconds > 1e11 else seconds, tz=timezone.utc)
    text = str(value).strip().replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(text.replace(' ', 'T', 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r'\d+', version)[:4]) or (0,)


# ---------------------------------------------------------------------------
# anonymization


def typed_placeholder(token: str, config: Config) -> str:
    if not token:
        return ARG
    if re.match(r'^[a-z][a-z0-9+.-]*://', token, re.IGNORECASE):
        return URL
    if EMAIL_RE.search(token):
        return EMAIL
    if any(pattern.search(token) for pattern in config.id_patterns):
        return ID
    if '/' in token or '\\' in token or token.startswith(('~', '.')) or re.search(r'\.[A-Za-z0-9]{1,5}$', token):
        return PATH
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}([T ][\d:.]+Z?)?', token):
        return DATE
    if re.fullmatch(r'-?\d+(\.\d+)?', token):
        return NUMBER
    if re.fullmatch(r'\d+(ms|s|m|h|d|w|mo|y)', token):
        return DURATION
    if re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)+', token) and not HEX_RE.search(token):
        return ARG  # a slug such as q3-roadmap
    if UUID_RE.search(token) or HEX_RE.search(token) or PREFIXED_ID_RE.search(token):
        return ID
    if re.search(r'\d', token) and re.search(r'[A-Za-z]', token) and len(token) >= 6:
        return ID
    if re.search(r'[A-Z"\'?!,;:()]', token):
        return TEXT
    return ARG


def collapse(tokens: list[str]) -> list[str]:
    """A run of 2+ value placeholders is one free-text argument the join split apart."""
    out: list[str] = []
    for token in tokens:
        if token in ALL_PLACEHOLDERS and out and out[-1] in ALL_PLACEHOLDERS:
            out[-1] = TEXT
            continue
        out.append(token)
    return out


def anonymize(raw: str, cli: str, inventory: Inventory | None, vocabulary: set[str], config: Config) -> list[str]:
    tokens = raw.split()
    if tokens and (tokens[0] == cli or Path(tokens[0]).name == cli):
        tokens = tokens[1:]
    node: Node | None = inventory.root if inventory else None
    command_names = {name for n in inventory.nodes.values() for name in n.children} if inventory else set()
    out: list[str] = []
    walking = True
    vocabulary_run = False  # right after an unknown command, the CLI's own words may follow
    kept_path_words = 0
    expect = ''  # '', 'yes' (flag takes a value), 'maybe' (unknown flag)
    end_of_options = False
    for token in tokens:
        if expect:
            is_child = bool(node and walking and COMMAND_WORD.match(token) and node.child(token))
            if expect == 'yes' or not is_child:
                expect = ''
                if not is_flag(token):
                    out.append(typed_placeholder(token, config))
                    continue
            expect = ''
        if not end_of_options and token == '--':
            out.append('--')
            end_of_options, walking = True, False
            continue
        if not end_of_options and is_flag(token):
            if node is None:
                walking = False  # without an inventory, words after a flag are values or free text
            name, has_value, value = token.partition('=')
            if not (LONG_FLAG.match(name) or SHORT_FLAG.match(name) or re.fullmatch(r'-[A-Za-z]{2,4}', name)):
                out.append(ARG)
                continue
            if has_value:
                out.append(f'{name}={typed_placeholder(value, config)}')
                continue
            out.append(name)
            expect = flag_expectation(name, node, inventory, config)
            continue
        if walking and node is not None:
            child = node.child(token) if COMMAND_WORD.match(token) else None
            if child is not None:
                out.append(token)
                assert inventory is not None
                node = inventory.nodes.get(
                    ' '.join(filter(None, [node.path, child])), Node(path=f'{node.path} {child}'.strip())
                )
                continue
            walking = False
            if node.children and not node.accepts_args and COMMAND_WORD.match(token):
                out.append(token)  # the unknown command word is the friction signal itself
                vocabulary_run = True
                continue
            out.append(typed_placeholder(token, config))
            continue
        if walking and COMMAND_WORD.match(token) and (kept_path_words < 2 or token in vocabulary):
            out.append(token)
            kept_path_words += 1
            continue
        walking = False
        if vocabulary_run and token in command_names:
            out.append(token)  # the CLI's own vocabulary, e.g. `notes sync`: never private
            continue
        vocabulary_run = False
        out.append(typed_placeholder(token, config))
    return collapse(out)


def flag_expectation(name: str, node: Node | None, inventory: Inventory | None, config: Config) -> str:
    if name in config.free_text_flags:
        return 'yes'
    if node is not None and inventory is not None:
        for scope in (node, inventory.root):
            if name in scope.flags or name in scope.shorts:
                return 'yes' if scope.takes_value(name) else ''
        elsewhere = [inventory.nodes[path].takes_value(name) for path in inventory.flag_index().get(name, [])]
        if elsewhere:
            return 'yes' if sum(elsewhere) * 2 >= len(elsewhere) else ''
    # unknown flag: the next token is a value unless it is a real subcommand
    return 'maybe'


def anonymize_token(token: str, config: Config) -> str:
    words = token.split()
    out = []
    for word in words:
        if is_flag(word) and (LONG_FLAG.match(word.split('=', 1)[0]) or SHORT_FLAG.match(word)):
            out.append(word.split('=', 1)[0])
        elif COMMAND_WORD.match(word):
            out.append(word)
        else:
            out.append(typed_placeholder(word, config))
    return ' '.join(out)


def privacy_hits(raw: str, error: str, shape: list[str]) -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}
    for column, text in (('input', raw), ('error', error)):
        for name, pattern in PRIVACY_DETECTORS:
            if pattern.search(text or ''):
                hits.setdefault(name, set()).add(column)
        if '\n' in (text or '').strip():
            hits.setdefault('multi-line', set()).add(column)
    if TEXT in shape or any(token.endswith('=' + TEXT) for token in shape):
        hits.setdefault('free-text', set()).add('input')
    if len(raw) >= 450:
        hits.setdefault('near-limit', set()).add('input')
    if any(token in ALL_PLACEHOLDERS or '=<' in token for token in shape):
        hits.setdefault('any-value', set()).add('input')
    return hits


# ---------------------------------------------------------------------------
# stage


def build_vocabulary(rows: Iterable[dict[str, Any]], catalog: dict[str, Any] | None, config: Config) -> set[str]:
    """Words safe to keep without an inventory: unknown-command tokens and catalog words."""
    words: set[str] = set()
    for row in rows:
        kind, token = parse_error(
            str(pick(row, 'error') or ''), config.unknown_command_patterns, config.unknown_flag_patterns
        )
        if kind == 'unknown-command':
            words.update(word for word in token.split() if COMMAND_WORD.match(word))
    for entry in (catalog or {}).get('commands', []):
        for field in ('pattern', 'target'):
            words.update(word for word in str(entry.get(field, '')).split() if COMMAND_WORD.match(word))
    return words


def run(
    export: Path, work: Path, cli: str, inventory: Inventory | None, catalog: dict[str, Any] | None, config: Config
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    copy = copy_export(export, work)
    rows = read_rows(copy)
    vocabulary = build_vocabulary(rows, catalog, config)
    shapes: dict[str, dict[str, Any]] = {}
    privacy: Counter[str] = Counter()
    privacy_columns: dict[str, set[str]] = {}
    events_with_raw = 0
    totals: dict[str, Counter[str]] = {name: Counter() for name in ('kind', 'actor', 'agent_type', 'error_kind')}
    versions: Counter[str] = Counter()
    times: list[datetime] = []
    word_split_times: list[tuple[datetime, str]] = []
    total = 0
    for row in rows:
        raw = str(pick(row, 'input') or '')
        error = str(pick(row, 'error') or '')
        weight = max(1, int(float(pick(row, 'weight') or 1)))
        total += weight
        argv = anonymize(raw, cli, inventory, vocabulary, config)
        kind, token = parse_error(error, config.unknown_command_patterns, config.unknown_flag_patterns)
        if kind == 'other':
            row_kind = str(pick(row, 'kind') or '')
            kind = row_kind if row_kind in ('unknown-command', 'unknown-flag') else 'other'
        token = anonymize_token(token, config)
        joined, joined_at = 0, 0
        token_words = token.split()
        if kind == 'unknown-command' and len(token_words) > 1:
            for index in range(len(argv)):
                if argv[index : index + len(token_words)] == token_words:
                    joined, joined_at = len(token_words), index
                    break
        hits = privacy_hits(raw, error, argv)
        if hits.keys() - {'any-value'}:
            events_with_raw += weight
        for name, columns in hits.items():
            privacy[name] += weight
            privacy_columns.setdefault(name, set()).update(columns)
        key = display(argv) + (f'  [joined {joined_at}:{joined}]' if joined else '')
        shape = shapes.setdefault(
            key,
            {
                'key': key,
                'argv': argv,
                'joined': joined,
                'joined_at': joined_at,
                'count': 0,
                'error_kind': kind,
                'error_token': token,
                'actors': Counter(),
                'agents': Counter(),
                'versions': Counter(),
                'first_day': '',
                'last_day': '',
                'burst': 0,
            },
        )
        shape['count'] += weight
        actor = str(pick(row, 'actor') or 'unknown')
        human, agent = pick(row, 'human_count'), pick(row, 'agent_count')
        if human is not None or agent is not None:
            shape['actors']['human'] += int(float(human or 0))
            shape['actors']['agent'] += int(float(agent or 0))
            totals['actor']['human'] += int(float(human or 0))
            totals['actor']['agent'] += int(float(agent or 0))
        else:
            shape['actors'][actor] += weight
            totals['actor'][actor] += weight
        agent_type = str(pick(row, 'agent_type') or '')
        if agent_type:
            shape['agents'][agent_type] += weight
            totals['agent_type'][agent_type] += weight
        version = str(pick(row, 'version') or '')
        if version:
            shape['versions'][version] += weight
            versions[version] += weight
        totals['kind'][str(pick(row, 'kind') or kind)] += weight
        totals['error_kind'][kind] += weight
        when = parse_time(pick(row, 'timestamp'))
        if when is not None:
            day = when.date().isoformat()
            times.append(when)
            shape['first_day'] = min(filter(None, [shape['first_day'], day]))
            shape['last_day'] = max(filter(None, [shape['last_day'], day]))
            if joined:
                word_split_times.append((when, key))
    mark_bursts(word_split_times, shapes)
    shape_list = sorted(shapes.values(), key=lambda s: (-s['count'], s['key']))
    for shape in shape_list:
        shape['actors'] = dict(shape['actors'])
        shape['agents'] = dict(shape['agents'])
        shape['versions'] = sorted(shape['versions'], key=version_key)
    ordered_versions = sorted(versions, key=version_key)
    profile = {
        'cli': cli,
        'events': total,
        'rows': len(rows),
        'shapes': len(shape_list),
        'kinds': dict(totals['kind'].most_common()),
        'error_kinds': dict(totals['error_kind'].most_common()),
        'actors': dict(totals['actor'].most_common()),
        'agent_types': dict(totals['agent_type'].most_common()),
        'versions': {
            'oldest': ordered_versions[0] if ordered_versions else '',
            'newest': ordered_versions[-1] if ordered_versions else '',
            'distinct': len(ordered_versions),
        },
        'first_day': min(times).date().isoformat() if times else '',
        'last_day': max(times).date().isoformat() if times else '',
        'privacy': {
            name: {'events': count, 'label': PRIVACY_LABELS[name], 'columns': sorted(privacy_columns[name])}
            for name, count in privacy.most_common()
        },
        'privacy_events': events_with_raw,
        'word_split_events': len(word_split_times),
        'word_split_burst_events': sum(shape['burst'] for shape in shape_list),
        'used_inventory': inventory is not None,
    }
    return shape_list, profile


def mark_bursts(events: list[tuple[datetime, str]], shapes: dict[str, dict[str, Any]]) -> None:
    """A word-split event within one second of another is part of a loop over command names."""
    events.sort()
    for index, (when, key) in enumerate(events):
        neighbors = [events[i][0] for i in (index - 1, index + 1) if 0 <= i < len(events)]
        if any(abs((when - other).total_seconds()) <= 1.0 for other in neighbors):
            shapes[key]['burst'] += 1
