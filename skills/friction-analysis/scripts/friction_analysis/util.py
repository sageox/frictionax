"""Shared helpers: JSON IO, placeholders, verb vocabulary, error parsing, edit distance."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# placeholders stand in for every value an export might carry; the report and every
# intermediate file only ever contain these, never the raw token
PATH = '<path>'
URL = '<url>'
EMAIL = '<email>'
ID = '<id>'
NUMBER = '<n>'
DURATION = '<duration>'
TEXT = '<text>'
ARG = '<arg>'
DATE = '<date>'
PLACEHOLDERS = frozenset({PATH, URL, EMAIL, ID, NUMBER, DURATION, TEXT, ARG, DATE})

# a command-path word: lowercase, short, no digits-heavy noise
COMMAND_WORD = re.compile(r'^[a-z][a-z0-9-]{0,31}$')
LONG_FLAG = re.compile(r'^--[A-Za-z0-9][A-Za-z0-9_.-]*$')
SHORT_FLAG = re.compile(r'^-[A-Za-z]$')

# verbs that mean the same action; mapping between members preserves the verb (AUTO-eligible)
DEFAULT_VERB_GROUPS: tuple[tuple[str, ...], ...] = (
    ('show', 'view', 'get', 'display', 'describe', 'cat', 'print', 'read', 'open'),
    ('list', 'ls', 'all', 'index', 'enumerate'),
    ('remove', 'rm', 'delete', 'del', 'destroy'),
    ('create', 'new', 'add', 'make'),
    ('status', 'stat', 'state', 'health'),
    ('run', 'exec', 'execute', 'start', 'launch'),
    ('stop', 'kill', 'halt', 'end'),
    ('edit', 'modify', 'change', 'set'),
    ('search', 'find', 'query', 'grep', 'lookup'),
    ('log', 'logs', 'history'),
    ('login', 'signin', 'sign-in', 'auth', 'authenticate'),
    ('logout', 'signout', 'sign-out'),
    ('config', 'configure', 'settings', 'conf', 'cfg'),
    ('version', 'ver'),
    ('help', 'usage'),
    ('info', 'about', 'whoami'),
)

# a command containing any of these creates, changes or destroys something: never AUTO,
# never replayed (creating and editing count: replay only proves parsing, not effects)
DEFAULT_DESTRUCTIVE_VERBS = frozenset(
    {
        'create',
        'add',
        'new',
        'make',
        'edit',
        'modify',
        'change',
        'set',
        'write',
        'rename',
        'move',
        'mv',
        'delete',
        'remove',
        'rm',
        'del',
        'destroy',
        'purge',
        'drop',
        'reset',
        'wipe',
        'erase',
        'prune',
        'gc',
        'clean',
        'cleanup',
        'clear',
        'uninstall',
        'upgrade',
        'update',
        'self-update',
        'install',
        'push',
        'publish',
        'deploy',
        'release',
        'kill',
        'stop',
        'abort',
        'revoke',
        'logout',
        'login',
        'migrate',
        'apply',
        'rollback',
        'restore',
        'format',
        'force',
        'overwrite',
        'sync',
        'import',
        'merge',
        'rebase',
        'commit',
        'send',
        'submit',
        'approve',
        'reject',
        'close',
    }
)

# synonyms between flag names: an unknown flag whose synonym exists becomes a token fix
DEFAULT_FLAG_GROUPS: tuple[tuple[str, ...], ...] = (
    ('--limit', '--last', '--max', '--count', '--top', '--max-results', '--num'),
    ('--since', '--after', '--from', '--window'),
    ('--until', '--before', '--to'),
    ('--json', '--output-json', '--format-json'),
    ('--quiet', '--silent'),
    ('--verbose', '--debug'),
    ('--dry-run', '--dryrun', '--noop'),
)

# deliberate probes of error handling: kept in the corpus as "stays an error", excluded from fixes
DEFAULT_NOISE_PATTERNS = (
    r'(^|\s)(bogus|nonexistent\S*|not-?a-?command|does-?not-?exist|fake-?\S*|asdf\S*|qwerty|xyzzy|foobar)(\s|$)',
    r'--(bogus|nonexistent|not-a-flag|invalid-flag|fake)\S*',
    r'--helpo\b',
)

# unknown-command / unknown-flag messages across common CLI frameworks; each captures the bad token
UNKNOWN_COMMAND_PATTERNS = (
    r'unknown command "(?P<tok>[^"]+)"',  # cobra
    r'unknown command[: ]+\'(?P<tok>[^\']+)\'',  # commander.js
    r'unexpected argument[: ]+"?(?P<tok>[^"\s]+)"?',  # kong
    r'[Nn]o help topic for \'(?P<tok>[^\']+)\'',  # urfave/cli
    r'command "(?P<tok>[^"]+)" not found',  # urfave/cli
    r'unrecognized subcommand \'(?P<tok>[^\']+)\'',  # clap
    r'invalid choice: \'(?P<tok>[^\']+)\'',  # argparse
)
UNKNOWN_FLAG_PATTERNS = (
    r'unknown flag:? (?P<tok>--?[\w-]+)',  # cobra, kong
    r'unknown shorthand flag: \'(?P<tok>\w)\'',  # cobra shorthand
    r'flag provided but not defined: (?P<tok>-{1,2}[\w-]+)',  # go flag, urfave/cli
    r'unknown option[: ]+\'?(?P<tok>--?[\w-]+)\'?',  # commander.js
    r'unexpected argument \'(?P<tok>--?[\w-]+)\' found',  # clap
    r'unrecognized arguments?: (?P<tok>--?[\w-]+)',  # argparse
)


def read_json(path: Path) -> Any:
    with path.open(encoding='utf-8') as handle:
        return json.load(handle)


def write_json(path: Path, data: Any, indent: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=indent, sort_keys=False, ensure_ascii=False)
        handle.write('\n')
    os.replace(tmp, path)


def read_json_or(path: Path, default: Any) -> Any:
    return read_json(path) if path.exists() else default


def is_flag(token: str) -> bool:
    return token.startswith('-') and len(token) > 1 and not re.match(r'^-\d', token)


def flag_name(token: str) -> str:
    """'--limit=5' -> '--limit'; shorthand clusters are kept as written."""
    return token.split('=', 1)[0]


def display(argv: list[str]) -> str:
    """Stable, human-readable form of an argv shape; also the shape's identity key."""
    return ' '.join(argv)


def parse_error(message: str, extra_command: tuple[str, ...] = (), extra_flag: tuple[str, ...] = ()) -> tuple[str, str]:
    """Return (kind, bad_token) from the first line of an error message, or ('other', '')."""
    first = (message or '').strip().splitlines()[0] if (message or '').strip() else ''
    for pattern in UNKNOWN_FLAG_PATTERNS + tuple(extra_flag):
        match = re.search(pattern, first)
        if match:
            token = match.group('tok')
            if not token.startswith('-'):
                token = '-' + token  # cobra reports shorthand letters without the dash
            return 'unknown-flag', token
    for pattern in UNKNOWN_COMMAND_PATTERNS + tuple(extra_command):
        match = re.search(pattern, first)
        if match:
            return 'unknown-command', match.group('tok')
    return 'other', ''


def osa_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: Levenshtein plus adjacent transpositions."""
    if a == b:
        return 0
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[len(a)][len(b)]


def levenshtein(a: str, b: str) -> int:
    """Plain Levenshtein, matching frictionax's internal/levenshtein (no transpositions)."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        previous = current
    return previous[len(b)]


def is_close_typo(typed: str, real: str) -> bool:
    """Length-scaled typo test: <=1 edit for short words, <=2 otherwise, never half the word."""
    distance = osa_distance(typed, real)
    if distance == 0:
        return False
    limit = 1 if len(typed) <= 4 else 2
    return distance <= limit and distance * 2 < len(typed)


def verb_group(word: str, groups: tuple[tuple[str, ...], ...]) -> int | None:
    for index, group in enumerate(groups):
        if word in group:
            return index
    return None


def singular_plural(a: str, b: str) -> bool:
    return a != b and (a + 's' == b or b + 's' == a or a + 'es' == b or b + 'es' == a)


def verb_preserved(typed: str, target: str, groups: tuple[tuple[str, ...], ...]) -> bool:
    """True when typed and target name the same action: equal, synonyms, plural, or a typo."""
    if typed == target or singular_plural(typed, target) or is_close_typo(typed, target):
        return True
    group = verb_group(typed, groups)
    return group is not None and group == verb_group(target, groups)


def plural(count: int, word: str, plural_form: str | None = None) -> str:
    return f'{count} {word if count == 1 else (plural_form or word + "s")}'
