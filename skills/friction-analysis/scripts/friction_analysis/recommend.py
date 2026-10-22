"""Turn classified shapes (plus the AI coworker's decisions) into concrete changes.

Outputs catalog entries with the AUTO rule applied and simulated against frictionax,
catalog cleanup, token fixes, a command x flag vocabulary, strings to fix, engine
fixes, the five guard tests, and a regression corpus with an expected outcome for
every shape.

AUTO rule: auto-execute only when the verb is preserved (same word, synonym,
plural, typo, or the same word under another parent) AND the target does not
mutate anything. Everything else is suggest-only.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .classify import BUCKETS, FAMILY, Context, alias_candidates, tally
from .frictionsim import Catalog, suggest
from .inventory import Resolution, resolve
from .util import is_flag, plural, verb_preserved

AUTO_CONFIDENCE = 0.9
ASK_CONFIDENCE = 0.8
TOKEN_CONFIDENCE = 0.8
MAPPED_BUCKETS = ('alias', 'catalog-miss', 'bad-fuzzy', 'cli-taught')


def apply_decisions(records: list[dict[str, Any]], decisions: dict[str, Any]) -> list[str]:
    """Merge decisions.json into the records; return problems instead of guessing."""
    problems = []
    table = decisions.get('decisions', decisions) if isinstance(decisions, dict) else {}
    by_key = {record['key']: record for record in records}
    for key, decision in table.items():
        record = by_key.get(key)
        if record is None:
            problems.append(f'decision for unknown shape: {key}')
            continue
        bucket = decision.get('bucket', record['bucket'])
        if bucket not in BUCKETS or bucket == 'unclassified':
            problems.append(f'{key}: invalid bucket {bucket!r}')
            continue
        record.update(bucket=bucket, family=FAMILY[bucket], decided=True, needs_judgment=False)
        if decision.get('reason'):
            record['reason'] = decision['reason']
        if decision.get('target'):
            record['target'] = decision['target'].split()
        elif 'target' in decision and decision['target'] in ('', None):
            record.pop('target', None)
    return problems


def typed_words(record: dict[str, Any]) -> list[str]:
    if record.get('unknown_word'):
        return record['path'] + [record['unknown_word']]
    if record.get('unknown_flags'):
        return record['path'] + record['unknown_flags'][:1]
    return [token for token in record['argv'] if not is_flag(token)]


def pick_target(record: dict[str, Any]) -> list[str] | None:
    if record.get('target'):
        return list(record['target'])
    candidates = record.get('candidates') or []
    if len(candidates) == 1:
        return list(candidates[0]['target'])
    return None


def auto_mode(typed: list[str], target: list[str], ctx: Context) -> tuple[bool, str]:
    if ctx.config.is_destructive(target):
        return False, 'target mutates state'
    typed_word = typed[-1].lstrip('-') if typed else ''
    target_word = next((word.lstrip('-') for word in reversed(target) if word), '')
    if verb_preserved(typed_word, target_word, ctx.config.verb_groups):
        return True, 'verb preserved'
    return False, 'verb changes'


def trailing(record: dict[str, Any], typed: list[str]) -> bool:
    return len(record['argv']) > len(typed) or bool(record.get('joined'))


def catalog_entries(records: list[dict[str, Any]], ctx: Context) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record['bucket'] not in MAPPED_BUCKETS:
            continue
        target = pick_target(record)
        if not target:
            continue
        typed = typed_words(record)
        if typed == target:
            continue
        groups[(' '.join(typed), ' '.join(target))].append(record)
    entries = []
    for (typed_text, target_text), members in groups.items():
        typed, target = typed_text.split(), target_text.split()
        auto, why = auto_mode(typed, target, ctx)
        with_args = any(trailing(member, typed) for member in members)
        if with_args and not typed_text.startswith('-') and not any(word.startswith('-') for word in typed):
            entry = {
                'pattern': '^' + re.escape(typed_text).replace('\\ ', ' ') + r'(\s.*)?$',
                'target': target_text + '$1',
                'has_regex': True,
            }
        else:
            entry = {'pattern': typed_text, 'target': target_text, 'has_regex': False}
        entry.update(
            auto_execute=auto,
            confidence=AUTO_CONFIDENCE if auto else ASK_CONFIDENCE,
            count=sum(member['count'] for member in members),
            description=f'{members[0]["bucket"]}: {typed_text} -> {target_text}',
        )
        entries.append(
            {
                'entry': entry,
                'mode': 'AUTO' if auto else 'ASK',
                'why': why,
                'typed': typed_text,
                'target': target_text,
                'events': entry['count'],
                'bucket': members[0]['bucket'],
                'shapes': [member['key'] for member in members],
            }
        )
    verify_entries(entries, records, ctx)
    return sorted(entries, key=lambda e: (-e['events'], e['typed']))


def verify_entries(entries: list[dict[str, Any]], records: list[dict[str, Any]], ctx: Context) -> None:
    """Simulate frictionax with the proposed entries: each member shape must land on its target."""
    existing = list(ctx.catalog.data.get('commands', []) or [])
    proposed = {
        'commands': existing + [e['entry'] for e in entries],  # later entries win, as in catalog.go
        'tokens': list(ctx.catalog.data.get('tokens', []) or []),
    }
    simulated = Catalog(proposed, ctx.cli)
    by_key = {record['key']: record for record in records}
    for entry in entries:
        landed = 0
        for key in entry['shapes']:
            record = by_key[key]
            result = suggest(
                simulated, ctx.inventory, record['argv'], 'unknown-command', record.get('unknown_word') or ''
            )
            if (
                result is not None
                and result.corrected.split()[: len(entry['target'].split())] == entry['target'].split()
            ):
                landed += 1
        entry['verified'] = landed == len(entry['shapes'])
        if ctx.inventory is not None:
            entry['shadowed'] = resolve(ctx.inventory, entry['typed'].split()).ok


def catalog_cleanup(ctx: Context) -> list[dict[str, Any]]:
    """Existing entries that can never fire (a real command shadows them) or point nowhere."""
    if ctx.inventory is None:
        return []
    out = []
    for entry in ctx.catalog.data.get('commands', []) or []:
        if entry.get('has_regex'):
            continue
        pattern_words = entry.get('pattern', '').split()
        target_words = [word for word in entry.get('target', '').split() if '$' not in word]
        if pattern_words and resolve(ctx.inventory, pattern_words).ok:
            out.append({'pattern': entry['pattern'], 'action': 'delete', 'why': 'a real command now resolves first'})
        elif target_words and not resolve(ctx.inventory, target_words).ok:
            node_path, word = pattern_words[:-1], pattern_words[-1] if pattern_words else ''
            candidates = alias_candidates(Resolution(node_path, word, []), ctx) if word else []
            retarget = ' '.join(candidates[0]['target']) if len(candidates) == 1 else ''
            out.append(
                {
                    'pattern': entry['pattern'],
                    'action': 'retarget' if retarget else 'review',
                    'target': retarget,
                    'why': f'target "{entry.get("target")}" does not exist',
                }
            )
    for entry in ctx.catalog.invalid:
        out.append({'pattern': entry.get('pattern', ''), 'action': 'fix', 'why': 'regex does not compile'})
    return out


def mark_replaced(cleanup: list[dict[str, Any]], entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A cleanup row whose pattern a new entry redefines is that entry, not a second change."""
    new_patterns = {entry['entry']['pattern'] for entry in entries} | {entry['typed'] for entry in entries}
    for row in cleanup:
        if row['pattern'] in new_patterns:
            row['replaced_by_entry'] = True
    for entry in entries:
        if any(row['pattern'] == entry['typed'] for row in cleanup):
            entry['why'] += '; replaces the existing entry'
    return cleanup


def flag_vocabulary(records: list[dict[str, Any]], ctx: Context) -> dict[str, Any]:
    requested: Counter[tuple[str, str]] = Counter()
    for record in records:
        # flags the CLI itself taught are still flags agents ask for
        if record['bucket'] not in ('flag-drift', 'cli-taught'):
            continue
        for flag in record['unknown_flags']:
            requested[(' '.join(record['path']), flag)] += record['count']
    if not requested:
        return {'rows': [], 'columns': [], 'cells': {}, 'add': {}, 'tokens': [], 'families': []}
    inventory = ctx.inventory
    nodes = {path for path, _ in requested}
    families: dict[str, list[str]] = defaultdict(list)
    if inventory is not None:
        for path in inventory.nodes:
            if path:
                families[path.split()[-1]].append(path)
    tokens, covered = token_entries(requested, ctx)
    covered_flags = {flag for _, flag in covered}
    family_rows: list[dict[str, Any]] = []
    add: dict[str, list[str]] = defaultdict(list)
    for verb, members in sorted(families.items()):
        if len(members) < 2 or not nodes & set(members):
            continue
        contract = []
        candidate_flags = {flag for path, flag in requested if path in members} - covered_flags
        for path in members:
            candidate_flags.update(name for name in inventory.nodes[path].flags if name not in inventory.root.flags)
        for flag in sorted(candidate_flags):
            present = [path for path in members if flag in inventory.nodes[path].flags]
            asked = sum(count for (path, name), count in requested.items() if name == flag and path in members)
            if (present and asked) or len(present) * 2 > len(members) or asked >= 2:
                contract.append(flag)
        for path in members:
            for flag in contract:
                if flag not in inventory.nodes[path].flags and flag not in inventory.root.flags:
                    add[path].append(flag)
        family_rows.append({'verb': verb, 'members': sorted(members), 'contract': contract})
    flag_owners = inventory.flag_index() if inventory is not None else {}
    for (path, flag), count in requested.items():
        if (path, flag) in covered:
            continue  # a token fix maps it to the flag the command already has
        family = family_of(path, family_rows)
        sibling_has = family is not None and any(owner in family['members'] for owner in flag_owners.get(flag, []))
        if flag not in add[path] and (count >= 2 or sibling_has):
            add[path].append(flag)
    # columns: most-requested flags first, then contract flags nobody asked for yet
    column_weight: Counter[str] = Counter({flag: 0 for flags in add.values() for flag in flags})
    row_weight: Counter[str] = Counter({path: 0 for path in add})
    for (path, flag), count in requested.items():
        column_weight[flag] += count
        row_weight[path] += count
    columns = [flag for flag, _ in sorted(column_weight.items(), key=lambda kv: (-kv[1], kv[0]))][:6]
    rows = [path for path, _ in sorted(row_weight.items(), key=lambda kv: (-kv[1], kv[0]))][:8]
    cells: dict[str, dict[str, str]] = {}
    for path in rows:
        cells[path] = {}
        node = inventory.nodes.get(path) if inventory else None
        for flag in columns:
            if node is not None and (flag in node.flags or flag in inventory.root.flags):
                cells[path][flag] = 'has'
            elif requested.get((path, flag)):
                cells[path][flag] = f'missing:{requested[(path, flag)]}'
            elif flag in add.get(path, []):
                cells[path][flag] = 'add'
            else:
                cells[path][flag] = 'na'
    return {
        'rows': rows,
        'columns': columns,
        'cells': cells,
        'add': {k: sorted(v) for k, v in add.items() if v},
        'tokens': tokens,
        'families': family_rows,
        'requested': [
            {'command': path, 'flag': flag, 'events': count} for (path, flag), count in requested.most_common()
        ],
    }


def family_of(path: str, families: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((family for family in families if path in family['members']), None)


def token_entries(
    requested: Counter[tuple[str, str]], ctx: Context
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """An unknown flag whose synonym the command already has becomes a suggest-only token fix.

    Returns the entries and the (command, flag) pairs they cover.
    """
    if ctx.inventory is None:
        return [], set()
    picks: dict[str, Counter[str]] = defaultdict(Counter)
    covered: set[tuple[str, str]] = set()
    for (path, flag), count in requested.items():
        node = ctx.inventory.nodes.get(path)
        if node is None:
            continue
        for group in ctx.config.flag_groups:
            if flag in group:
                for synonym in group:
                    if synonym != flag and synonym in node.flags:
                        picks[flag][synonym] += count
                        covered.add((path, flag))
    out = []
    for flag, targets in sorted(picks.items()):
        target, count = targets.most_common(1)[0]
        out.append(
            {'pattern': flag, 'target': target, 'kind': 'unknown-flag', 'count': count, 'confidence': TOKEN_CONFIDENCE}
        )
    return out, covered


def strings_to_fix(records: list[dict[str, Any]], refs: list[dict[str, Any]], ctx: Context) -> list[dict[str, Any]]:
    # a reference agents actually followed into a failure; "ok" ones resolve in --help but fail when run
    followed = {record['ref']['ref'] for record in records if record.get('ref')}
    out = []
    for ref in refs:
        where = ref['file'] + (f':{ref["line"]}' if ref.get('line') else '')
        if ref['status'] == 'ok' and ref['ref'] not in followed:
            continue
        hint = ''
        if ref['status'] == 'unknown-command':
            candidates = alias_candidates(Resolution(ref['path'], ref['bad'], []), ctx)
            hint = ' or '.join(' '.join([ctx.cli] + c['target']) for c in candidates[:2])
            if not hint and ctx.inventory is not None:
                # the stale word was often an extra level: `acme notes sync` -> `acme sync`
                rest = ref['words'][ref['words'].index(ref['bad']) + 1 :] if ref['bad'] in ref['words'] else []
                if rest and resolve(ctx.inventory, ref['path'] + rest).ok:
                    hint = ' '.join([ctx.cli] + ref['path'] + rest)
            problem = f'no command "{ref["bad"]}" under "{" ".join([ctx.cli] + ref["path"])}"'
        elif ref['status'] == 'unknown-flag':
            problem = f'"{" ".join([ctx.cli] + ref["path"])}" has no {ref["bad"]}'
            owners = ctx.inventory.flag_index().get(ref['bad'], []) if ctx.inventory is not None else []
            hint = f'add {ref["bad"]} (as on {owners[0]})' if owners else f'drop {ref["bad"]}'
        else:
            problem = 'resolves in --help but fails when run (the flag is not reachable)'
        out.append(
            {'where': where, 'ref': ref['ref'], 'problem': problem, 'hint': hint, 'agents_hit': ref['ref'] in followed}
        )
    return out


def engine_fixes(
    buckets: dict[str, dict[str, int]],
    records: list[dict[str, Any]],
    strings: list[dict[str, Any]],
    profile: dict[str, Any],
    ctx: Context,
) -> list[dict[str, Any]]:
    cli = ctx.cli
    fixes = []
    if profile.get('privacy_events'):
        fixes.append(
            {
                'id': 'privacy',
                'family': 'defect',
                'events': profile['privacy_events'],
                'title': 'Redact friction events',
                'detail': 'Keep only command-path words and flag names; replace every other token with <arg>. '
                'Configure WithRedactor, or overwrite Event.Input from argv, before events leave the machine.',
            }
        )

    def add(bucket: str, title: str, detail: str) -> None:
        if bucket in buckets:
            fixes.append(
                {
                    'id': bucket,
                    'family': FAMILY[bucket],
                    'events': buckets[bucket]['still_fails'] or buckets[bucket]['events'],
                    'title': title,
                    'detail': detail,
                }
            )

    burst = profile.get('word_split_burst_events', 0)
    split_events = buckets.get('word-split', {}).get('events', 0)
    add(
        'word-split',
        'Split multi-word arguments',
        f'When one argv element contains whitespace and its words resolve to a command, split it and run, '
        f'with a note that zsh does not split an unquoted $var. {burst} of {split_events} events arrived in sub-second bursts.',
    )
    add(
        'cli-taught',
        'Fix what the CLI teaches',
        f"{plural(len(strings), 'reference')} in {cli}'s own strings fail. Correct them, make documented flags reachable, "
        f'and test that every reference resolves.',
    )
    add(
        'catalog-miss',
        'Match catalog entries by prefix',
        'Exact whole-input matching misses trailing arguments. Match the longest positional prefix, carry the rest through, '
        'and validate that every target exists.',
    )
    add(
        'bad-fuzzy',
        'Stop unrelated fuzzy suggestions',
        'Scale edit distance to word length (1 for 4 letters or fewer, else 2, never half the word) and never '
        'suggest a mutating command from a fuzzy match.',
    )
    add(
        'gated',
        'Explain gated commands',
        f'Print "{cli} <name> is not enabled" instead of "unknown command", and find where agents learned the name.',
    )
    add(
        'flag-drift',
        'Share flags across siblings',
        'Give sibling commands one flag vocabulary instead of aliasing each spelling.',
    )
    add('alias', 'Add desire-path entries', 'Map what agents type to the command they meant.')
    return fixes


def tests(
    buckets: dict[str, dict[str, int]],
    entries: list[dict[str, Any]],
    vocabulary: dict[str, Any],
    strings: list[dict[str, Any]],
    profile: dict[str, Any],
    ctx: Context,
) -> list[dict[str, Any]]:
    cli = ctx.cli
    return [
        {
            'name': 'TestFrictionCorpus',
            'guards': 'every bucket',
            'asserts': f'each of the {profile.get("shapes", 0)} anonymized shapes in corpus.json gets its expected outcome '
            '(resolves, auto -> target, suggests target, gated message, or stays an error), using the parser '
            'and the friction handler on synthesized errors so no command executes.',
            'red_first': 'delete one AUTO entry from the catalog; that shape must fail.',
        },
        {
            'name': 'TestCommandReferencesResolve',
            'guards': 'cli-taught',
            'asserts': f'every quoted `{cli} ...` in shipped strings, help text and bundled skills resolves, flags included '
            f'({plural(len(strings), "reference")} fail today).',
            'red_first': 'reintroduce one stale string; the test names its file and line.',
        },
        {
            'name': 'TestCatalogTargetsResolve',
            'guards': 'catalog-miss',
            'asserts': 'every non-regex target resolves, every regex compiles, and no pattern is shadowed by a real command.',
            'red_first': 'point one entry at a command that does not exist.',
        },
        {
            'name': 'TestFlagVocabulary',
            'guards': 'flag-drift',
            'asserts': 'sibling commands that share a verb carry the shared flags: '
            + (
                '; '.join(f'every {f["verb"]}: {" ".join(f["contract"])}' for f in vocabulary.get('families', [])[:3])
                or 'one vocabulary per sibling family'
            )
            + '.',
            'red_first': 'remove a contract flag from one sibling.',
        },
        {
            'name': 'TestFrictionEventsAreRedacted',
            'guards': 'privacy',
            'asserts': 'an event built from argv with a synthetic home path, email, ID and free text carries none of them; '
            'only command-path words and flag names survive.',
            'red_first': 'remove the redactor; the synthetic values appear in the event.',
        },
    ]


def expected_outcome(
    record: dict[str, Any],
    entry_by_shape: dict[str, dict[str, Any]],
    vocabulary: dict[str, Any],
    tokens: dict[str, str],
) -> str:
    bucket = record['bucket']
    entry = entry_by_shape.get(record['key'])
    if bucket == 'fixed':
        today = record.get('today') or {}
        return (
            f'auto:{today["corrected"]}'
            if record['latest'].get('how') == 'corrected' and today.get('corrected')
            else 'resolves'
        )
    if entry is not None:
        return ('auto:' if entry['mode'] == 'AUTO' else 'suggests:') + entry['target']
    if bucket == 'word-split':
        return 'resolves'
    if bucket == 'flag-drift':
        path = ' '.join(record['path'])
        if all(flag in vocabulary.get('add', {}).get(path, []) for flag in record['unknown_flags']):
            return 'resolves'
        flag = record['unknown_flags'][0] if record['unknown_flags'] else ''
        return f'suggests:{tokens[flag]}' if flag in tokens else 'error'
    if bucket == 'cli-taught' and record.get('unknown_flags'):
        path = ' '.join(record['path'])
        added = all(flag in vocabulary.get('add', {}).get(path, []) for flag in record['unknown_flags'])
        return 'resolves' if added or record.get('runtime_stale') else 'error'
    if bucket == 'gated':
        return 'gated'
    if bucket == 'unclassified':
        return 'needs-judgment'
    return 'error'


def top_actions(
    fixes: list[dict[str, Any]], entries: list[dict[str, Any]], vocabulary: dict[str, Any]
) -> list[dict[str, Any]]:
    actions = []
    for fix in fixes:
        detail = {
            'privacy': 'Keep command path and flag names. Drop every value.',
            'word-split': 'One argument held a whole command path. Split and run.',
            'cli-taught': 'Its hints name commands that fail. Fix and test them.',
            'catalog-miss': 'Entries miss when arguments follow. Match by prefix.',
            'bad-fuzzy': 'Short typos reach unrelated, even mutating, commands.',
            'gated': 'Say "not enabled", not "unknown command".',
            'flag-drift': f'Add {plural(sum(len(v) for v in vocabulary.get("add", {}).values()), "flag")} so siblings match.',
            'alias': f'{plural(len(entries), "entry", "entries")}: '
            f'{sum(1 for e in entries if e["mode"] == "AUTO")} AUTO, {sum(1 for e in entries if e["mode"] == "ASK")} ASK.',
        }.get(fix['id'], fix['detail'])
        actions.append(
            {'id': fix['id'], 'family': fix['family'], 'title': fix['title'], 'detail': detail, 'events': fix['events']}
        )
    privacy = [a for a in actions if a['id'] == 'privacy']
    rest = sorted((a for a in actions if a['id'] != 'privacy'), key=lambda a: -a['events'])
    return (privacy + rest)[:3]


def run(
    classified: dict[str, Any],
    decisions: dict[str, Any] | None,
    refs: list[dict[str, Any]],
    profile: dict[str, Any],
    ctx: Context,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = classified['shapes']
    problems = apply_decisions(records, decisions or {})
    buckets = tally(records)
    entries = catalog_entries(records, ctx)
    entry_by_shape = {key: entry for entry in entries for key in entry['shapes']}
    vocabulary = flag_vocabulary(records, ctx)
    tokens = {token['pattern']: token['target'] for token in vocabulary.get('tokens', [])}
    strings = strings_to_fix(records, refs, ctx)
    fixes = engine_fixes(buckets, records, strings, profile, ctx)
    corpus = []
    for record in records:
        record['expected'] = expected_outcome(record, entry_by_shape, vocabulary, tokens)
        corpus.append(
            {
                'argv': record['argv'],
                'joined': record.get('joined', 0),
                'joined_at': record.get('joined_at', 0),
                'events': record['count'],
                'bucket': record['bucket'],
                'expected': record['expected'],
            }
        )
    after = sum(r['count'] for r in records if r['expected'] == 'resolves' or r['expected'].startswith('auto:'))
    gated_events: Counter[str] = Counter()
    for record in records:
        if record['bucket'] == 'gated' and record.get('unknown_word'):
            gated_events[' '.join(record['path'] + [record['unknown_word']])] += record['count']
    fuzzy_rows: Counter[tuple[str, str]] = Counter()
    for record in records:
        if record['bucket'] == 'bad-fuzzy' and record['key'] not in entry_by_shape:
            fuzzy_rows[(record['unknown_word'], record.get('fuzzy', {}).get('suggested', ''))] += record['count']
    judgment = [
        {
            'key': r['key'],
            'events': r['count'],
            'reason': r['reason'],
            'candidates': [' '.join(c['target']) for c in r.get('candidates', [])],
        }
        for r in records
        if r.get('needs_judgment')
    ]
    recommendations = {
        'buckets': buckets,
        'shapes': records,
        'catalog_entries': entries,
        'catalog_cleanup': mark_replaced(catalog_cleanup(ctx), entries),
        'gated': [{'command': name, 'events': count} for name, count in gated_events.most_common()],
        'fuzzy': [
            {'typed': typed, 'suggested': suggested, 'events': count}
            for (typed, suggested), count in fuzzy_rows.most_common()
        ],
        'vocabulary': vocabulary,
        'strings_to_fix': strings,
        'engine_fixes': fixes,
        'tests': tests(buckets, entries, vocabulary, strings, profile, ctx),
        'top_actions': top_actions(fixes, entries, vocabulary),
        'judgment': judgment,
        'decision_problems': problems,
        'estimate': {
            'events': profile.get('events', 0),
            'works_now': buckets.get('fixed', {}).get('events', 0),
            'works_after': after,
        },
        'catalog_patch': {'commands': [e['entry'] for e in entries], 'tokens': vocabulary.get('tokens', [])},
    }
    return recommendations, corpus
