"""A faithful Python model of frictionax's suggestion chain.

Mirrors catalog.go (exact match on positionals-then-sorted-flags, then regex
entries), chain.go (command remap -> token fix -> Levenshtein with maxDistance 2)
and handler.go (auto-execute only for catalog remaps with auto_execute and
confidence >= 0.85). Used to predict what the library does today for each shape
and to check proposed catalog entries before anyone ships them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .inventory import Inventory
from .util import is_flag, levenshtein

AUTO_EXECUTE_THRESHOLD = 0.85
LEVENSHTEIN_MAX_DISTANCE = 2


@dataclass
class Suggestion:
    source: str  # 'command-remap' | 'token-fix' | 'levenshtein'
    corrected: str
    auto: bool
    confidence: float
    entry: dict[str, Any] | None = None
    distance: int = 0


def normalize_command(text: str, cli: str) -> str:
    parts = text.split()
    if parts and parts[0] == cli:
        parts = parts[1:]
    positionals = [part for part in parts if not part.startswith('-')]
    flags = sorted(part for part in parts if part.startswith('-'))
    return ' '.join(positionals + flags)


class Catalog:
    def __init__(self, data: dict[str, Any] | None, cli: str):
        self.cli = cli
        self.data = data or {'commands': [], 'tokens': []}
        self.exact: dict[str, dict[str, Any]] = {}
        self.regex: list[tuple[re.Pattern[str], dict[str, Any]]] = []
        self.invalid: list[dict[str, Any]] = []
        for entry in self.data.get('commands', []) or []:
            if entry.get('has_regex'):
                try:
                    self.regex.append((re.compile(go_regex_to_python(entry['pattern'])), entry))
                except re.error:
                    self.invalid.append(entry)
            else:
                self.exact[normalize_command(entry.get('pattern', ''), cli)] = entry
        self.tokens = {(t.get('pattern', '').lower(), t.get('kind', '')): t for t in self.data.get('tokens', []) or []}

    def lookup(self, text: str) -> dict[str, Any] | None:
        normalized = normalize_command(text, self.cli)
        if normalized in self.exact:
            return self.exact[normalized]
        for pattern, entry in self.regex:
            if pattern.search(normalized):
                return entry
        return None

    def apply(self, entry: dict[str, Any], text: str) -> str:
        if not entry.get('has_regex'):
            return entry.get('target', '')
        pattern = re.compile(go_regex_to_python(entry['pattern']))
        match = pattern.search(text)
        if not match:
            return entry.get('target', '')
        result = entry.get('target', '')
        for index, capture in enumerate(match.groups(), 1):
            result = result.replace(f'${index}', capture or '')
        return result


def go_regex_to_python(pattern: str) -> str:
    """RE2 and Python agree on what catalogs use, except RE2's (?<name>) group spelling."""
    return re.sub(r'\(\?<(?![=!])', '(?P<', pattern)


def command_names(inventory: Inventory) -> list[str]:
    """Same set the cobra adapter feeds Levenshtein: every non-root path, space-joined."""
    return [path for path in sorted(inventory.nodes) if path]


def flag_names(inventory: Inventory, path: str) -> list[str]:
    node = inventory.nodes.get(path)
    if node is None:
        return []
    return list(node.flags) + list(node.shorts)


def suggest(
    catalog: Catalog, inventory: Inventory | None, argv: list[str], kind: str, bad_token: str, parent: str = ''
) -> Suggestion | None:
    full = ' '.join(argv)
    entry = catalog.lookup(full)
    if entry is not None:
        confidence = float(entry.get('confidence', 0))
        return Suggestion(
            'command-remap',
            catalog.apply(entry, full),
            bool(entry.get('auto_execute')) and confidence >= AUTO_EXECUTE_THRESHOLD,
            confidence,
            entry,
        )
    token_entry = catalog.tokens.get((bad_token.lower(), kind)) if bad_token else None
    if token_entry is not None:
        return Suggestion('token-fix', token_entry.get('target', ''), False, float(token_entry.get('confidence', 0)))
    if inventory is None or not bad_token:
        return None
    options = command_names(inventory) if kind == 'unknown-command' else flag_names(inventory, parent)
    best, best_distance = '', LEVENSHTEIN_MAX_DISTANCE + 1
    for option in options:
        distance = levenshtein(bad_token, option)
        if distance < best_distance:
            best, best_distance = option, distance
    if best_distance > LEVENSHTEIN_MAX_DISTANCE:
        return None
    return Suggestion(
        'levenshtein', best, False, 1 - best_distance / (LEVENSHTEIN_MAX_DISTANCE + 1), distance=best_distance
    )


def unrelated(typed: str, suggestion: str) -> bool:
    """A fuzzy suggestion is unrelated when the edit distance is half the word or more."""
    target = suggestion.split()[-1] if suggestion.split() else suggestion
    distance = min(levenshtein(typed, suggestion), levenshtein(typed, target))
    return distance * 2 >= len(typed) or (len(typed) <= 4 and distance >= 2)


def strip_flags(words: list[str]) -> list[str]:
    return [word for word in words if not is_flag(word)]
