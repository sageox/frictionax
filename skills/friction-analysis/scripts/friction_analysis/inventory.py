"""Command x flag inventory, built by parsing `<cli> <path> --help` in the sandbox.

The parser is framework-agnostic: it looks for section headers that mention
commands, flags or options (cobra, kong, urfave/cli, clap, argparse, commander all
qualify) and reads the first column of the indented lines beneath them. The
result is a flat map from space-joined command path to its children, aliases and
flags. A hand-written inventory JSON with the same shape can be supplied instead.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import Sandbox, warn
from .util import COMMAND_WORD, PLACEHOLDERS, is_flag

HEADER = re.compile(r'^(?P<title>[A-Za-z][A-Za-z /&()-]{1,40}):\s*$|^(?P<caps>[A-Z][A-Z /&-]{2,40})$')
USAGE_INLINE = re.compile(r'^\s*usage:\s*(?P<rest>.+)$', re.IGNORECASE)
LONG_IN_COLUMN = re.compile(r'(?<![\w-])--[A-Za-z0-9][\w.-]*')
SHORT_IN_COLUMN = re.compile(r'(?<![\w-])-[A-Za-z0-9](?![\w-])')
USAGE_NOISE = re.compile(
    r'^(\[?(flags|options|global options|command|commands|subcommand)\]?|<command>|COMMAND|\[COMMAND\]|\.\.\.|\|)$',
    re.IGNORECASE,
)
SKIP_RECURSE = frozenset({'help', 'completion', '__complete', '__completenoDesc'})
# words that follow a CLI's name in prose, never in an invocation
PROSE_WORDS = frozenset(
    {
        'is',
        'was',
        'are',
        'be',
        'will',
        'can',
        'could',
        'should',
        'would',
        'may',
        'might',
        'must',
        'has',
        'have',
        'had',
        'does',
        'did',
        'do',
        'not',
        'and',
        'or',
        'but',
        'the',
        'a',
        'an',
        'to',
        'for',
        'in',
        'on',
        'of',
        'with',
        'by',
        'from',
        'at',
        'as',
        'that',
        'this',
        'it',
        'its',
        'you',
        'your',
        'we',
        'our',
        'cli',
        'binary',
        'itself',
        'needs',
        'uses',
        'runs',
        'reads',
        'writes',
        'failed',
        'exited',
    }
)


@dataclass
class Node:
    path: str
    children: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> child name
    flags: dict[str, bool] = field(default_factory=dict)  # '--name' -> takes a value
    shorts: dict[str, str] = field(default_factory=dict)  # '-n' -> '--name' (or itself)
    accepts_args: bool = False
    hidden_from_help: bool = False

    def child(self, word: str) -> str | None:
        if word in self.children:
            return word
        return self.aliases.get(word)

    def has_flag(self, token: str) -> bool:
        if self.hidden_from_help:
            return True  # its help was unreadable, so its flags are unknown, not absent
        name = token.split('=', 1)[0]
        if name in ('--help', '-h'):
            return True
        if name.startswith('--'):
            return name in self.flags
        return name in self.shorts or name in self.flags

    def takes_value(self, token: str) -> bool:
        name = token.split('=', 1)[0]
        if name.startswith('--'):
            return self.flags.get(name, False)
        long = self.shorts.get(name)
        return bool(long and self.flags.get(long, False))

    def to_json(self) -> dict[str, Any]:
        return {
            'children': self.children,
            'aliases': self.aliases,
            'flags': self.flags,
            'shorts': self.shorts,
            'accepts_args': self.accepts_args,
            'hidden_from_help': self.hidden_from_help,
        }


@dataclass
class Inventory:
    cli: str
    nodes: dict[str, Node]
    source: str = 'help'
    version: str = ''
    help_refs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def root(self) -> Node:
        return self.nodes['']

    def node(self, path: list[str]) -> Node | None:
        return self.nodes.get(' '.join(path))

    def all_paths(self) -> list[str]:
        return [path for path in self.nodes if path]

    def flag_index(self) -> dict[str, list[str]]:
        """flag -> command paths that define it (root-level/global flags excluded)."""
        global_flags = set(self.root.flags)
        index: dict[str, list[str]] = {}
        for path, node in self.nodes.items():
            for name in node.flags:
                if path and name not in global_flags:
                    index.setdefault(name, []).append(path)
        return index

    def to_json(self) -> dict[str, Any]:
        return {
            'cli': self.cli,
            'source': self.source,
            'version': self.version,
            'commands': {path: node.to_json() for path, node in sorted(self.nodes.items())},
            'help_refs': self.help_refs,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Inventory:
        nodes: dict[str, Node] = {}
        for path, raw in data.get('commands', {}).items():
            flags = raw.get('flags', {})
            if isinstance(flags, list):  # hand-written inventories may list flags without types
                flags = {name: False for name in flags}
            nodes[path] = Node(
                path=path,
                children=list(raw.get('children', [])),
                aliases=dict(raw.get('aliases', {})),
                flags=dict(flags),
                shorts=dict(raw.get('shorts', {})),
                accepts_args=bool(raw.get('accepts_args', False)),
                hidden_from_help=bool(raw.get('hidden_from_help', False)),
            )
        nodes.setdefault('', Node(path=''))
        # children listed only implicitly (a 'plan list' key without 'list' under 'plan')
        for path in list(nodes):
            if not path:
                continue
            parent, _, name = path.rpartition(' ')
            parent_node = nodes.setdefault(parent, Node(path=parent))
            if name not in parent_node.children:
                parent_node.children.append(name)
        for path, node in list(nodes.items()):
            for name in node.children:
                nodes.setdefault(f'{path} {name}'.strip(), Node(path=f'{path} {name}'.strip()))
        return cls(
            cli=data.get('cli', ''),
            nodes=nodes,
            source=data.get('source', 'file'),
            version=data.get('version', ''),
            help_refs=list(data.get('help_refs', [])),
        )


@dataclass
class Resolution:
    path: list[str]  # command words that resolved
    unknown_word: str | None  # first positional that is not a command where one was required
    unknown_flags: list[str]  # flags the resolved command does not define
    joined_arg: bool = False  # the shape carries a multi-word path in one argv element

    @property
    def ok(self) -> bool:
        return self.unknown_word is None and not self.unknown_flags and not self.joined_arg


def resolve(inventory: Inventory, argv: list[str], joined: int = 0, joined_at: int = 0) -> Resolution:
    """Walk argv down the command tree the way a parser would.

    `joined` > 0 means the `joined` words starting at argv index `joined_at`
    arrived as one argv element; no real command name contains whitespace, so a
    parser reports that element as an unknown command.
    """
    node = inventory.root
    path: list[str] = []
    unknown_word: str | None = None
    unknown_flags: list[str] = []
    walking = True
    skip_value = False
    for index, token in enumerate(argv):
        if joined and index == joined_at:
            return Resolution(path, ' '.join(argv[joined_at : joined_at + joined]), [], joined_arg=True)
        if skip_value:
            skip_value = False
            continue
        if token == '--':
            walking = False
            continue
        if is_flag(token):
            if not node.has_flag(token) and not inventory.root.has_flag(token):
                unknown_flags.append(token.split('=', 1)[0])
            elif '=' not in token and (node.takes_value(token) or inventory.root.takes_value(token)):
                skip_value = True
            continue
        if not walking:
            continue
        if token in PLACEHOLDERS:
            if node.children and not node.accepts_args:
                unknown_word = token  # a value where a subcommand was required
                break
            walking = False
            continue
        child = node.child(token)
        if child is not None:
            path.append(child)
            node = inventory.nodes.get(' '.join(path), Node(path=' '.join(path)))
            continue
        if node.children and not node.accepts_args:
            unknown_word = token
            break
        walking = False
    if unknown_word is not None:
        unknown_flags = []  # parsers stop at the unknown command; later flags were never checked
    return Resolution(path, unknown_word, unknown_flags)


def argv_positionals(argv: list[str]) -> list[str]:
    return [token for token in argv if not is_flag(token)]


# ---------------------------------------------------------------------------
# help parsing


def split_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = [('preamble', [])]
    for line in text.splitlines():
        inline = USAGE_INLINE.match(line)
        if inline and not line.startswith(' '):
            sections.append(('usage', [inline.group('rest')]))
            continue
        header = HEADER.match(line.rstrip())
        if header and not line.startswith((' ', '\t')):
            title = (header.group('title') or header.group('caps') or '').lower()
            sections.append((title, []))
            continue
        sections[-1][1].append(line)
    return sections


def section_kind(title: str) -> str:
    if 'example' in title:
        return 'examples'
    if 'usage' in title:
        return 'usage'
    if 'alias' in title:
        return 'aliases'
    if 'flag' in title or 'option' in title:
        return 'flags'
    if 'command' in title:
        return 'commands'
    return 'other'


def first_column(line: str) -> str:
    stripped = line.strip()
    return re.split(r'\s{2,}|\t', stripped, maxsplit=1)[0]


def parse_help(text: str, cli: str, path: list[str]) -> Node:
    node = Node(path=' '.join(path))
    for title, lines in split_sections(text):
        kind = section_kind(title)
        if kind == 'commands':
            parse_command_lines(node, lines, path)
        elif kind == 'flags':
            parse_flag_lines(node, lines)
        elif kind == 'usage':
            node.accepts_args = node.accepts_args or usage_accepts_args(lines, cli, path)
    return node


def parse_command_lines(node: Node, lines: list[str], path: list[str]) -> None:
    base_indent: int | None = None
    for line in lines:
        if not line.strip() or not line.startswith((' ', '\t')):
            continue
        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            base_indent = indent
        if indent > base_indent:
            continue  # a wrapped description line, not a command entry
        column = first_column(line).rstrip(':')
        words = column.replace(',', ' , ').split()
        # kong and others print the full path ('plan list'); drop the part we are already in
        while words and path and words[: len(path)] == path:
            words = words[len(path) :]
        if not words:
            continue
        names = [word for word in ' '.join(words).split(',')]
        primary = names[0].strip().split()
        if not primary or not COMMAND_WORD.match(primary[0]):
            continue
        name = primary[0]
        if name not in node.children:
            node.children.append(name)
        for alias in names[1:]:
            alias_words = alias.strip().split()
            if alias_words and COMMAND_WORD.match(alias_words[0]):
                node.aliases[alias_words[0]] = name


def parse_flag_lines(node: Node, lines: list[str]) -> None:
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith('-'):
            continue
        column = first_column(line)
        longs = LONG_IN_COLUMN.findall(column)
        shorts = SHORT_IN_COLUMN.findall(column)
        leftover = LONG_IN_COLUMN.sub('', SHORT_IN_COLUMN.sub('', column)).replace(',', ' ').strip()
        takes_value = bool(leftover) or '=' in column
        for name in longs:
            node.flags[name.rstrip('.')] = takes_value
        primary = longs[0] if longs else None
        for short in shorts:
            node.shorts[short] = primary or short
            if primary is None:
                node.flags.setdefault(short, takes_value)


def usage_accepts_args(lines: list[str], cli: str, path: list[str]) -> bool:
    for line in lines:
        words = line.split()
        if not words:
            continue
        # usage lines start with the binary name (sometimes a full path) then the command path
        if Path(words[0]).name == cli or words[0] == cli:
            words = words[1:]
        while words and path and words[0] in path:
            words = words[1:]
        for word in words:
            if USAGE_NOISE.match(word) or is_flag(word.strip('[]')):
                continue
            return True
    return False


def extract_refs(text: str, cli: str) -> list[dict[str, Any]]:
    """Find `<cli> <words> [--flags]` references; used for help text and source scans."""
    pattern = re.compile(
        r'(?<![\w./-])' + re.escape(cli) + r'((?:[ \t]+(?:[a-z][a-z0-9-]*|<[^<>\s]+>|\{[^{}\s]+\}|%[sdv]|\$\w+))*)'
        r'((?:[ \t]+--?[A-Za-z][\w-]*(?:=\S+)?(?:[ \t]+(?!-)[^\s`\'"]+)?)*)'
    )
    refs = []
    for match in pattern.finditer(text):
        words = match.group(1).split()
        flags = [flag_token.split('=', 1)[0] for flag_token in match.group(2).split() if flag_token.startswith('-')]
        if not words and not flags:
            continue
        if words and words[0] in PROSE_WORDS:
            continue  # "acme is a CLI for ..." is prose about the tool, not an invocation
        refs.append({'words': words, 'flags': flags, 'start': match.start()})
    return refs


def example_refs(text: str, cli: str) -> list[dict[str, Any]]:
    """Invocations a help page teaches: its Examples section and `$ cli ...` lines."""
    refs = []
    for title, lines in split_sections(text):
        for line in lines:
            stripped = line.strip()
            prompt = stripped.startswith('$ ')
            if section_kind(title) == 'examples' or prompt:
                refs.extend(extract_refs(stripped.removeprefix('$ ').split('#', 1)[0], cli))
    return refs


def check_ref(inventory: Inventory, words: list[str], flags: list[str]) -> dict[str, Any]:
    """Resolve a reference; trailing words under a leaf are treated as arguments or prose."""
    node = inventory.root
    path: list[str] = []
    for word in words:
        if not COMMAND_WORD.match(word):
            break  # placeholder such as <id> or %s: everything after is arguments
        child = node.child(word)
        if child is not None:
            path.append(child)
            node = inventory.nodes.get(' '.join(path), Node(path=' '.join(path)))
            continue
        if node.children and not node.accepts_args:
            return {'path': path, 'status': 'unknown-command', 'bad': word}
        break
    for flag in flags:
        if not node.has_flag(flag) and not inventory.root.has_flag(flag):
            return {'path': path, 'status': 'unknown-flag', 'bad': flag}
    return {'path': path, 'status': 'ok', 'bad': ''}


# ---------------------------------------------------------------------------
# building


def looks_like_help(result_text: str) -> bool:
    lowered = result_text.lower()
    return 'usage' in lowered or 'commands' in lowered or 'flags' in lowered or 'options' in lowered


def help_text(sandbox: Sandbox, path: list[str]) -> str | None:
    for probe in (['--help'], ['-h']):
        result = sandbox.run(path + probe)
        text = result.stdout + '\n' + result.stderr
        if result.exit_code is None:
            return None
        if looks_like_help(text) and 'unknown command' not in text.lower():
            return text
    return None


def build(sandbox: Sandbox, cli: str, max_depth: int = 4, max_nodes: int = 400) -> Inventory:
    nodes: dict[str, Node] = {}
    help_refs: list[dict[str, Any]] = []
    queue: deque[list[str]] = deque([[]])
    while queue and len(nodes) < max_nodes:
        path = queue.popleft()
        text = help_text(sandbox, path)
        if text is None:
            if not path:
                raise RuntimeError('the binary printed no recognizable --help output')
            nodes[' '.join(path)] = Node(path=' '.join(path), hidden_from_help=True)
            continue
        node = parse_help(text, cli, path)
        nodes[node.path] = node
        for ref in example_refs(text, cli):
            help_refs.append({'where': 'help:' + (node.path or '(root)'), 'words': ref['words'], 'flags': ref['flags']})
        if len(path) < max_depth:
            for name in node.children:
                if name not in SKIP_RECURSE:
                    queue.append(path + [name])
    if queue:
        warn(f'inventory stopped at {max_nodes} commands; raise --max-commands to see the rest')
    version_result = sandbox.run(['--version'])
    version = ''
    if version_result.exit_code == 0:
        version = (version_result.stdout.strip().splitlines() or [''])[0][:80]
    return Inventory(cli=cli, nodes=nodes, source='help', version=version, help_refs=help_refs)
