"""Find every `<cli> <words> --flags` reference the CLI ships, and check it resolves.

A CLI teaches its own vocabulary through hints, error messages, docs and bundled
agent skills. When one of those strings names a command or flag that no longer
exists, agents type it faithfully and fail: the `cli-taught` bucket. References
are only taken from string literals (quoted or backticked) in code, and from
inline code or fenced blocks in docs, so ordinary prose does not count.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .inventory import Inventory, check_ref, extract_refs

CODE_SUFFIXES = frozenset(
    {
        '.go',
        '.py',
        '.rs',
        '.ts',
        '.tsx',
        '.js',
        '.mjs',
        '.cjs',
        '.rb',
        '.java',
        '.kt',
        '.swift',
        '.cs',
        '.c',
        '.cc',
        '.cpp',
        '.h',
        '.sh',
        '.bash',
        '.zsh',
        '.json',
        '.yaml',
        '.yml',
        '.toml',
        '.tmpl',
    }
)
DOC_SUFFIXES = frozenset({'.md', '.mdx', '.txt', '.rst'})
SKIP_DIRS = frozenset(
    {
        '.git',
        'node_modules',
        'vendor',
        'dist',
        'build',
        'target',
        '.venv',
        'venv',
        '__pycache__',
        'testdata',
        'fixtures',
        'test',
        'tests',
        '__tests__',
        '.next',
        'coverage',
    }
)
TEST_FILE = re.compile(r'(_test\.go|_test\.py|^test_.*\.py|\.(test|spec)\.[jt]sx?)$')
MAX_FILE_BYTES = 1_000_000


def source_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob('*')):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if not path.is_file() or path.is_symlink() or TEST_FILE.search(path.name):
            continue
        if path.suffix.lower() in CODE_SUFFIXES | DOC_SUFFIXES and path.stat().st_size <= MAX_FILE_BYTES:
            files.append(path)
    return files


def quoted_contexts(line: str, is_doc: bool, in_fence: bool) -> list[str]:
    """Return the pieces of a line that are string literals or code, not prose."""
    if is_doc and in_fence:
        return [re.sub(r'^\s*[$>]\s*', '', line)]
    pieces = re.findall(r'`([^`]+)`', line)
    if not is_doc:
        pieces += re.findall(r'"((?:[^"\\]|\\.)*)"', line)
        pieces += re.findall(r"'((?:[^'\\]|\\.)*)'", line)
    return pieces


CUE = re.compile(r'(?:^|[\s(:])(?:run|try|use|type|execute|call|via|with|see|then|or|and)\s+$', re.IGNORECASE)


def invocation_cue(piece: str, start: int) -> bool:
    """A reference counts when it opens the literal or follows an imperative ("run ...")."""
    before = piece[:start]
    return not before.strip(' \t$>') or bool(CUE.search(before))


def scan(root: Path, cli: str, inventory: Inventory) -> list[dict[str, Any]]:
    found: dict[tuple[str, int, str], dict[str, Any]] = {}
    for path in source_files(root):
        is_doc = path.suffix.lower() in DOC_SUFFIXES
        in_fence = False
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if is_doc and line.lstrip().startswith(('```', '~~~')):
                in_fence = not in_fence
                continue
            for piece in quoted_contexts(line, is_doc, in_fence):
                for ref in extract_refs(piece, cli):
                    if not invocation_cue(piece, ref['start']):
                        continue
                    verdict = check_ref(inventory, ref['words'], ref['flags'])
                    display = ' '.join([cli] + ref['words'] + ref['flags'])
                    key = (str(path.relative_to(root)), number, display)
                    found[key] = {
                        'file': key[0],
                        'line': number,
                        'ref': display,
                        'words': ref['words'],
                        'flags': ref['flags'],
                        **verdict,
                    }
    return sorted(found.values(), key=lambda r: (r['status'] == 'ok', r['file'], r['line']))


def help_refs(inventory: Inventory) -> list[dict[str, Any]]:
    """References printed by the binary's own --help output (examples, hints)."""
    out = []
    seen: set[tuple[str, str]] = set()
    for ref in inventory.help_refs:
        display = ' '.join([inventory.cli] + ref['words'] + ref['flags'])
        if (ref['where'], display) in seen:
            continue
        seen.add((ref['where'], display))
        out.append(
            {
                'file': ref['where'],
                'line': 0,
                'ref': display,
                'words': ref['words'],
                'flags': ref['flags'],
                **check_ref(inventory, ref['words'], ref['flags']),
            }
        )
    return out
