"""Assign every shape to exactly one root-cause bucket. Rules are ordered; first match wins.

Deterministic first, judgment second: anything these rules cannot place lands in
`unclassified` with its candidate targets, for the AI coworker to decide in
decisions.json. Counts always reconcile to the export: a shape's events move as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .frictionsim import Catalog, Suggestion, suggest, unrelated
from .inventory import Inventory, Resolution, resolve
from .util import PLACEHOLDERS, display, is_close_typo, is_flag, singular_plural, verb_group

# bucket ids in report order; the family decides the color and the headline grouping
BUCKETS = (
    'cli-taught',
    'word-split',
    'catalog-miss',
    'bad-fuzzy',
    'alias',
    'flag-drift',
    'missing-feature',
    'gated',
    'fixed',
    'noise',
    'unclassified',
)
FAMILY = {
    'cli-taught': 'defect',
    'word-split': 'defect',
    'catalog-miss': 'defect',
    'bad-fuzzy': 'defect',
    'alias': 'gap',
    'flag-drift': 'gap',
    'missing-feature': 'gap',
    'gated': 'gated',
    'fixed': 'works',
    'noise': 'noise',
    'unclassified': 'noise',
}


@dataclass
class Context:
    cli: str
    config: Config
    catalog: Catalog
    inventory: Inventory | None = None
    inventory_all: Inventory | None = None
    replay: dict[str, dict[str, Any]] = field(default_factory=dict)
    refs: list[dict[str, Any]] = field(default_factory=list)


def static_view(shape: dict[str, Any], ctx: Context) -> Resolution:
    """Resolution from the inventory, or a best effort from the error message alone."""
    if ctx.inventory is not None:
        return resolve(ctx.inventory, shape['argv'], shape.get('joined', 0), shape.get('joined_at', 0))
    positionals = [token for token in shape['argv'] if not is_flag(token) and token not in PLACEHOLDERS]
    if shape.get('joined'):
        return Resolution(positionals[: shape['joined_at']], shape['error_token'], [], joined_arg=True)
    if shape['error_kind'] == 'unknown-command' and shape['error_token'] in positionals:
        index = positionals.index(shape['error_token'])
        return Resolution(positionals[:index], shape['error_token'], [])
    if shape['error_kind'] == 'unknown-flag':
        return Resolution(positionals, None, [shape['error_token']])
    return Resolution(positionals, None, [])


def latest_outcome(
    shape: dict[str, Any], resolution: Resolution, today: Suggestion | None, ctx: Context
) -> dict[str, Any]:
    exec_result = ctx.replay.get(shape['key'])
    if exec_result and exec_result.get('result') in ('works', 'corrected'):
        return {'outcome': 'works', 'source': 'replay', 'how': exec_result['result']}
    if exec_result and exec_result.get('corrected') and exec_result.get('result') == 'other-error':
        return {'outcome': 'works', 'source': 'replay', 'how': 'corrected'}
    if exec_result and exec_result.get('result') in ('unknown-command', 'unknown-flag'):
        return {
            'outcome': 'fails',
            'source': 'replay',
            'how': exec_result['result'],
            'suggested': exec_result.get('suggested', ''),
        }
    if ctx.inventory is None:
        return {'outcome': 'unverified', 'source': 'none', 'how': 'no binary or inventory'}
    if resolution.ok:
        return {'outcome': 'works', 'source': 'static', 'how': 'resolves'}
    if today is not None and today.auto and target_resolves(today.corrected, ctx):
        return {'outcome': 'works', 'source': 'static', 'how': 'corrected'}
    return {
        'outcome': 'fails',
        'source': 'static',
        'how': 'unknown-flag' if resolution.unknown_flags else 'unknown-command',
    }


def target_resolves(target: str, ctx: Context) -> bool:
    if ctx.inventory is None:
        return True
    words = target.split()
    if words and words[0] == ctx.cli:
        words = words[1:]
    literal = [word for word in words if '$' not in word]
    return resolve(ctx.inventory, literal).ok


def matching_ref(resolution: Resolution, ctx: Context) -> dict[str, Any] | None:
    for ref in ctx.refs:
        ref_path = ref.get('path', [])
        if resolution.unknown_word is not None:
            if (
                ref['status'] == 'unknown-command'
                and ref_path == resolution.path
                and ref['bad'] == resolution.unknown_word
            ):
                return ref
        elif resolution.unknown_flags and ref_path == resolution.path:
            if set(resolution.unknown_flags) & set(ref['flags']):
                return ref
    return None


def catalog_miss(shape: dict[str, Any], resolution: Resolution, today: Suggestion | None, ctx: Context) -> str | None:
    positionals = [token for token in shape['argv'] if not is_flag(token)]
    has_flags = any(is_flag(token) for token in shape['argv'])
    if today is not None and today.source == 'command-remap' and not target_resolves(today.corrected, ctx):
        return f'entry "{today.entry["pattern"]}" targets "{today.corrected}", which does not exist'
    if today is not None and today.source == 'command-remap':
        return None
    for length in range(len(positionals), 0, -1):
        if length == len(positionals) and not has_flags:
            continue  # that is the exact lookup frictionax already does
        key = ' '.join(positionals[:length])
        entry = ctx.catalog.exact.get(key)
        if entry is not None:
            return f'entry "{entry["pattern"]}" exists but exact matching misses trailing arguments'
    return None


def alias_candidates(resolution: Resolution, ctx: Context) -> list[dict[str, Any]]:
    """Deterministic target candidates for an unknown command word, best tier first."""
    if ctx.inventory is None or resolution.unknown_word is None:
        return []
    word = resolution.unknown_word
    path = resolution.path
    node = ctx.inventory.node(path)
    if node is None:
        return []
    tiers: list[list[dict[str, Any]]] = [[], [], [], []]
    group = verb_group(word, ctx.config.verb_groups)
    for child in node.children:
        if group is not None and verb_group(child, ctx.config.verb_groups) == group:
            tiers[0].append({'target': path + [child], 'how': 'synonym'})
        elif singular_plural(word, child) or is_close_typo(word, child):
            tiers[1].append({'target': path + [child], 'how': 'near spelling'})
    if word == 'help':
        tiers[2].append({'target': path + ['--help'], 'how': 'help as a subcommand'})
    for other_path, other in ctx.inventory.nodes.items():
        if other_path != ' '.join(path) and other.child(word) is not None:
            tiers[3].append({'target': (other_path.split() if other_path else []) + [word], 'how': 'lives elsewhere'})
    for tier in tiers:
        if tier:
            return tier
    return []


def flags_as_subcommands(resolution: Resolution, ctx: Context) -> list[str] | None:
    """`<group> --list` where `<group> list` exists: agents spelling a subcommand as a flag."""
    if ctx.inventory is None or not resolution.unknown_flags:
        return None
    node = ctx.inventory.node(resolution.path)
    if node is None:
        return None
    names = [flag.lstrip('-') for flag in resolution.unknown_flags]
    if all(node.child(name) for name in names) and len(names) == 1:
        return resolution.path + [node.child(names[0]) or names[0]]
    return None


def gated(resolution: Resolution, ctx: Context) -> bool:
    if resolution.unknown_word is None:
        return False
    full = ' '.join(resolution.path + [resolution.unknown_word])
    if resolution.unknown_word in ctx.config.gated or full in ctx.config.gated:
        return True
    if ctx.inventory_all is not None and ctx.inventory is not None:
        return full in ctx.inventory_all.nodes and full not in ctx.inventory.nodes
    return False


def classify_shape(shape: dict[str, Any], ctx: Context) -> dict[str, Any]:
    resolution = static_view(shape, ctx)
    bad_token = resolution.unknown_word or (resolution.unknown_flags[0] if resolution.unknown_flags else '')
    kind = 'unknown-command' if resolution.unknown_word or resolution.joined_arg else 'unknown-flag'
    today = suggest(ctx.catalog, ctx.inventory, shape['argv'], kind, bad_token, ' '.join(resolution.path))
    latest = latest_outcome(shape, resolution, today, ctx)
    exec_suggested = (ctx.replay.get(shape['key']) or {}).get('suggested', '')
    record: dict[str, Any] = {
        'key': shape['key'],
        'argv': shape['argv'],
        'count': shape['count'],
        'joined': shape.get('joined', 0),
        'joined_at': shape.get('joined_at', 0),
        'path': resolution.path,
        'unknown_word': resolution.unknown_word,
        'unknown_flags': resolution.unknown_flags,
        'latest': latest,
        'still_fails': latest['outcome'] != 'works',
        'today': ({'source': today.source, 'corrected': today.corrected, 'auto': today.auto} if today else None),
        'actors': shape.get('actors', {}),
        'agents': shape.get('agents', {}),
        'versions': shape.get('versions', []),
        'burst': shape.get('burst', 0),
        'candidates': [],
        'needs_judgment': False,
    }

    def done(bucket: str, reason: str, **extra: Any) -> dict[str, Any]:
        record.update(bucket=bucket, family=FAMILY[bucket], reason=reason, **extra)
        return record

    text = display(shape['argv'])
    if any(pattern.search(text) for pattern in ctx.config.noise_patterns):
        return done('noise', 'deliberate probe of error handling')
    if latest['outcome'] == 'works':
        how = {
            'resolves': 'resolves on the current binary',
            'corrected': 'the catalog auto-corrects it',
            'works': 'runs on the current binary',
        }.get(latest['how'], 'works on the current binary')
        return done('fixed', how)
    if gated(resolution, ctx):
        return done('gated', 'feature-gated command invoked while disabled')
    if resolution.joined_arg:
        return done('word-split', 'a multi-word command path arrived as one argument')
    ref = matching_ref(resolution, ctx)
    if ref is not None:
        where = ref['file'] + (f':{ref["line"]}' if ref.get('line') else '')
        return done(
            'cli-taught',
            f'the CLI itself references it ({where})',
            ref={'where': where, 'ref': ref['ref']},
            runtime_stale=ref['status'] == 'ok',
            candidates=alias_candidates(resolution, ctx),
        )
    miss = catalog_miss(shape, resolution, today, ctx)
    if miss is not None:
        return done('catalog-miss', miss, candidates=alias_candidates(resolution, ctx))
    fuzzy = exec_suggested or (today.corrected if today and today.source == 'levenshtein' else '')
    if resolution.unknown_word and fuzzy and unrelated(resolution.unknown_word, fuzzy):
        mutating = ctx.config.is_destructive(fuzzy.split())
        return done(
            'bad-fuzzy',
            f'fuzzy match suggests unrelated "{fuzzy}"' + (' (mutating)' if mutating else ''),
            fuzzy={'suggested': fuzzy, 'mutating': mutating},
            candidates=alias_candidates(resolution, ctx),
        )
    as_subcommand = flags_as_subcommands(resolution, ctx)
    if as_subcommand is not None:
        return done(
            'alias',
            'a subcommand spelled as a flag',
            candidates=[{'target': as_subcommand, 'how': 'flag as subcommand'}],
            target=as_subcommand,
        )
    if resolution.unknown_flags:
        return done('flag-drift', 'flag the command does not define', flag_evidence=flag_evidence(resolution, ctx))
    candidates = alias_candidates(resolution, ctx)
    if len(candidates) == 1:
        return done(
            'alias',
            f'missing desire-path entry ({candidates[0]["how"]})',
            candidates=candidates,
            target=candidates[0]['target'],
        )
    record['needs_judgment'] = True
    reason = 'several plausible targets' if candidates else 'no deterministic target'
    return done('unclassified', reason, candidates=candidates)


def flag_evidence(resolution: Resolution, ctx: Context) -> dict[str, list[str]]:
    """Where else each unknown flag exists: siblings first, then anywhere in the tree."""
    if ctx.inventory is None:
        return {}
    index = ctx.inventory.flag_index()
    parent = ' '.join(resolution.path[:-1])
    evidence = {}
    for flag in resolution.unknown_flags:
        owners = index.get(flag, [])
        siblings = [path for path in owners if path.rpartition(' ')[0] == parent and path != ' '.join(resolution.path)]
        evidence[flag] = siblings or owners
    return evidence


def run(shapes: list[dict[str, Any]], ctx: Context) -> dict[str, Any]:
    records = [classify_shape(shape, ctx) for shape in shapes]
    return {'shapes': records, 'buckets': tally(records)}


def tally(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    buckets = {name: {'events': 0, 'shapes': 0, 'still_fails': 0} for name in BUCKETS}
    for record in records:
        bucket = buckets[record['bucket']]
        bucket['events'] += record['count']
        bucket['shapes'] += 1
        bucket['still_fails'] += record['count'] if record['still_fails'] else 0
    return {name: value for name, value in buckets.items() if value['shapes']}
