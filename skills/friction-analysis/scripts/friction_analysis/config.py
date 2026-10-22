"""Optional per-CLI analysis config. Every CLI-specific value lives here, never in code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import DEFAULT_DESTRUCTIVE_VERBS, DEFAULT_FLAG_GROUPS, DEFAULT_NOISE_PATTERNS, DEFAULT_VERB_GROUPS, read_json

KNOWN_KEYS = frozenset(
    {
        'free_text_flags',
        'id_patterns',
        'noise_patterns',
        'gated',
        'verb_groups',
        'destructive_verbs',
        'flag_groups',
        'unknown_command_patterns',
        'unknown_flag_patterns',
        'comment',
    }
)


@dataclass
class Config:
    free_text_flags: frozenset[str] = frozenset()
    id_patterns: tuple[re.Pattern[str], ...] = ()
    noise_patterns: tuple[re.Pattern[str], ...] = ()
    gated: frozenset[str] = frozenset()
    verb_groups: tuple[tuple[str, ...], ...] = DEFAULT_VERB_GROUPS
    destructive_verbs: frozenset[str] = DEFAULT_DESTRUCTIVE_VERBS
    flag_groups: tuple[tuple[str, ...], ...] = DEFAULT_FLAG_GROUPS
    unknown_command_patterns: tuple[str, ...] = ()
    unknown_flag_patterns: tuple[str, ...] = ()

    def is_destructive(self, words: list[str]) -> bool:
        if '--help' in words or '-h' in words:
            return False  # a help request never mutates anything
        return any(word in self.destructive_verbs for word in words)


def load(path: Path | None) -> Config:
    if path is None:
        return Config(noise_patterns=tuple(re.compile(p) for p in DEFAULT_NOISE_PATTERNS))
    data: dict[str, Any] = read_json(path)
    unknown = set(data) - KNOWN_KEYS
    if unknown:
        raise ValueError(f'unknown config keys: {", ".join(sorted(unknown))}')
    for key in ('unknown_command_patterns', 'unknown_flag_patterns'):
        for pattern in data.get(key, []):
            if '(?P<tok>' not in pattern:
                raise ValueError(f'{key}: every pattern needs a (?P<tok>...) group: {pattern}')
    return Config(
        free_text_flags=frozenset(data.get('free_text_flags', [])),
        id_patterns=tuple(re.compile(p) for p in data.get('id_patterns', [])),
        noise_patterns=tuple(re.compile(p) for p in list(DEFAULT_NOISE_PATTERNS) + data.get('noise_patterns', [])),
        gated=frozenset(data.get('gated', [])),
        verb_groups=tuple(tuple(g) for g in data.get('verb_groups', [])) + DEFAULT_VERB_GROUPS,
        destructive_verbs=DEFAULT_DESTRUCTIVE_VERBS | frozenset(data.get('destructive_verbs', [])),
        flag_groups=tuple(tuple(g) for g in data.get('flag_groups', [])) + DEFAULT_FLAG_GROUPS,
        unknown_command_patterns=tuple(data.get('unknown_command_patterns', [])),
        unknown_flag_patterns=tuple(data.get('unknown_flag_patterns', [])),
    )
