#!/usr/bin/env python3
"""Friction analysis: export -> anonymized shapes -> replay -> buckets -> fixes -> HTML report.

Run with `python3 -I` (isolated mode): the export is untrusted data, so neither the
current directory nor PYTHONPATH may contribute modules. Only this script's own
directory is added to the import path.

    python3 -I analyze.py --work ./friction-work --export friction.csv --cli acme \\
        --binary "$(command -v acme)" --catalog path/to/default_catalog.json

Stages (default: all): inventory, normalize, replay, refs, classify, recommend, render.
Nothing is executed beyond `<cli> ... --help` unless --exec is passed with an env
file of side-effect switches. Re-run `--stages recommend,render` after writing
decisions.json; settings persist in <work>/run.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never write caches into the installed skill directory
sys.path.insert(0, str(Path(__file__).resolve().parent))  # trusted: the skill's own scripts

# imports follow the sys.path line above on purpose
from friction_analysis import classify, normalize, recommend, render, replay  # noqa: E402
from friction_analysis import config as config_module  # noqa: E402
from friction_analysis import inventory as inventory_module  # noqa: E402
from friction_analysis import refs as refs_module  # noqa: E402
from friction_analysis.frictionsim import Catalog  # noqa: E402
from friction_analysis.sandbox import Sandbox, load_env, resolve_binary, warn  # noqa: E402
from friction_analysis.util import read_json, read_json_or, write_json  # noqa: E402

STAGES = ('inventory', 'normalize', 'replay', 'refs', 'classify', 'recommend', 'render')
# writes to the user's catalog, so it never runs by default and needs --apply-catalog
APPLY_STAGE = 'apply'
PATH_SETTINGS = ('export', 'inventory', 'catalog', 'source', 'config', 'feature_env_file', 'decisions', 'out')


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Analyze a CLI friction export and render a report.')
    parser.add_argument('--work', required=True, type=Path, help='work directory (created; holds every output)')
    parser.add_argument('--export', type=Path, help='friction export: CSV, JSON or JSONL')
    parser.add_argument('--cli', help='CLI name as typed by users, e.g. acme')
    parser.add_argument(
        '--binary', help='command that runs the current CLI build (shell-split), e.g. "/usr/local/bin/acme"'
    )
    parser.add_argument('--inventory', type=Path, help='hand-written command inventory JSON instead of --help probing')
    parser.add_argument('--catalog', type=Path, help='current friction catalog JSON (commands/tokens)')
    parser.add_argument('--source', type=Path, help='CLI source repository to scan for stale command references')
    parser.add_argument('--config', type=Path, help='per-CLI analysis config JSON')
    parser.add_argument(
        '--exec', action='store_true', help='execute every shape in the sandbox (needs --env/--env-file)'
    )
    parser.add_argument(
        '--env', action='append', default=[], metavar='KEY=VALUE', help='side-effect switch for the sandbox'
    )
    parser.add_argument(
        '--env-file', action='append', default=[], type=Path, help='file of KEY=VALUE side-effect switches'
    )
    parser.add_argument(
        '--feature-env-file', type=Path, help='env that enables feature-gated commands (finds the gated bucket)'
    )
    parser.add_argument('--decisions', type=Path, help='judgment file (default: <work>/decisions.json)')
    parser.add_argument('--stages', help='comma-separated subset of: ' + ','.join(STAGES) + f' (or {APPLY_STAGE})')
    parser.add_argument('--apply-catalog', type=Path, help='catalog file the apply stage rewrites (after review)')
    parser.add_argument('--skip-replay', action='store_true', help='never run the binary, not even --help')
    parser.add_argument('--dry-run', action='store_true', help='print the argv each shape would replay, then stop')
    parser.add_argument('--timeout', type=float, help='seconds per invocation (default 10)')
    parser.add_argument('--max-commands', type=int, help='inventory size cap (default 400)')
    parser.add_argument('--out', type=Path, help='report path (default: <work>/report.html)')
    return parser.parse_args(argv)


def settings(args: argparse.Namespace) -> dict:
    """Merge this invocation's flags over the persisted run.json so later passes need only --work."""
    work = args.work.resolve()
    saved = read_json_or(work / 'run.json', {})
    current = {
        'cli': args.cli,
        'binary': args.binary,
        'exec': args.exec or None,
        'skip_replay': args.skip_replay or None,
        'timeout': args.timeout,
        'max_commands': args.max_commands,
        'env_files': [str(p.resolve()) for p in args.env_file] or None,
    }
    for name in PATH_SETTINGS:
        value = getattr(args, name)
        current[name] = str(value.resolve()) if value else None
    merged = {**saved, **{k: v for k, v in current.items() if v is not None}}
    merged.setdefault('timeout', 10.0)
    merged.setdefault('max_commands', 400)
    if not merged.get('cli'):
        raise SystemExit('--cli is required on the first run')
    return merged


def need(path: Path, stage: str) -> dict | list:
    if not path.exists():
        raise SystemExit(f'{path.name} is missing: run the {stage} stage first')
    return read_json(path)


def method_lines(run: dict, profile: dict, inventory: inventory_module.Inventory | None) -> tuple[str, str]:
    cli = run['cli']
    version = (inventory.version if inventory else '') or 'the current build'
    if not version.startswith(cli):
        version = f'{cli} {version}'  # `acme --version` usually already names the CLI
    shapes = profile.get('shapes', 0)
    if run.get('replay') == 'exec':
        return (
            f'{profile.get("events", 0)} failed invocations, {shapes} distinct shapes, each replayed against {version} in a sandbox.',
            f'Replayed with a throwaway HOME and XDG dirs, an empty non-git working directory, stdin closed, HTTP routed '
            f'to a loopback discard port, a {run["timeout"]:g}s timeout, and the side-effect switches below. Shapes whose '
            'command, unknown word or catalog auto-correct target mutates state kept their static outcome.',
        )
    if inventory is not None and inventory.source == 'help':
        return (
            f'{profile.get("events", 0)} failed invocations, {shapes} distinct shapes, checked against {version} --help. Nothing else ran.',
            f'Outcomes are static: each shape was resolved against the command tree parsed from {cli} --help, run in the sandbox.',
        )
    if inventory is not None:
        return (
            f'{profile.get("events", 0)} failed invocations, {shapes} distinct shapes, checked against a supplied command inventory.',
            'Outcomes are static: each shape was resolved against the supplied inventory.',
        )
    return (
        f'{profile.get("events", 0)} failed invocations, {shapes} distinct shapes. Not replayed.',
        'No binary or inventory was available, so "still fails on latest" could not be checked.',
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    run = settings(args)
    stages = [s.strip() for s in (args.stages or ','.join(STAGES)).split(',') if s.strip()]
    unknown = set(stages) - set(STAGES) - {APPLY_STAGE}
    if unknown:
        raise SystemExit(f'unknown stages: {", ".join(sorted(unknown))}')
    cli = run['cli']
    config = config_module.load(Path(run['config']) if run.get('config') else None)
    catalog_data = read_json(Path(run['catalog'])) if run.get('catalog') else None
    catalog = Catalog(catalog_data, cli)
    binary = None if run.get('skip_replay') or not run.get('binary') else resolve_binary(run['binary'])
    user_env = load_env(args.env, [Path(p) for p in run.get('env_files', [])])
    if args.env or run.get('env_files'):
        run['env_keys'] = sorted(user_env)

    inventory = None
    inventory_all = None
    if 'inventory' in stages:
        if run.get('inventory'):
            inventory = inventory_module.Inventory.from_json(read_json(Path(run['inventory'])))
            inventory.cli = inventory.cli or cli
            inventory.source = 'file'  # supplied by the user: nothing ran to produce it in this session
        elif binary is not None:
            with Sandbox(binary, user_env, run['timeout']) as sandbox:
                inventory = inventory_module.build(sandbox, cli, max_nodes=run['max_commands'])
            if run.get('feature_env_file'):
                feature_env = {**user_env, **load_env([], [Path(run['feature_env_file'])])}
                with Sandbox(binary, feature_env, run['timeout']) as sandbox:
                    inventory_all = inventory_module.build(sandbox, cli, max_nodes=run['max_commands'])
                write_json(work / 'inventory-all.json', inventory_all.to_json())
        if inventory is not None:
            write_json(work / 'inventory.json', inventory.to_json())
    hand_written = bool(run.get('inventory'))  # never runs anything, so --skip-replay keeps it
    if inventory is None and (work / 'inventory.json').exists() and (hand_written or not run.get('skip_replay')):
        inventory = inventory_module.Inventory.from_json(read_json(work / 'inventory.json'))
    if inventory_all is None and (work / 'inventory-all.json').exists() and not run.get('skip_replay'):
        inventory_all = inventory_module.Inventory.from_json(read_json(work / 'inventory-all.json'))

    if 'normalize' in stages:
        if not run.get('export'):
            raise SystemExit('--export is required for the normalize stage')
        shapes, profile = normalize.run(Path(run['export']), work, cli, inventory, catalog_data, config)
        write_json(work / 'shapes.json', shapes)
        write_json(work / 'profile.json', profile)
    shapes = need(work / 'shapes.json', 'normalize')
    profile = need(work / 'profile.json', 'normalize')

    replay_results = read_json_or(work / 'replay.json', {}) if not run.get('skip_replay') else {}
    if 'replay' in stages and run.get('exec') and binary is not None:
        if not user_env:
            raise SystemExit(
                '--exec runs the CLI: pass --env-file with the switches that disable its telemetry, '
                'daemons and network calls (ask the user for them)'
            )
        warn(
            f'executing {cli} for {len(shapes)} shapes in a sandbox (HOME, XDG and cwd are throwaway; '
            f'switches: {", ".join(sorted(user_env))})'
        )
        with Sandbox(binary, user_env, run['timeout']) as sandbox:
            replay_results = replay.run(shapes, sandbox, cli, inventory, config, catalog, dry_run=args.dry_run)
        if args.dry_run:
            return 0
        write_json(work / 'replay.json', replay_results)
        run['replay'] = 'exec'
    elif 'replay' in stages and args.dry_run:
        with Sandbox(binary or ['true'], user_env, run['timeout']) as sandbox:
            replay.run(shapes, sandbox, cli, inventory, config, catalog, dry_run=True)
        return 0
    if not replay_results:
        run['replay'] = 'static' if inventory is not None else 'none'

    if 'refs' in stages:
        found = refs_module.help_refs(inventory) if inventory is not None else []
        if run.get('source') and inventory is not None:
            found += refs_module.scan(Path(run['source']), cli, inventory)
        elif run.get('source'):
            warn('--source needs a binary or --inventory to check references against; skipped')
        write_json(work / 'refs.json', found)
    refs = read_json_or(work / 'refs.json', [])

    ctx = classify.Context(
        cli=cli,
        config=config,
        catalog=catalog,
        inventory=inventory,
        inventory_all=inventory_all,
        replay=replay_results,
        refs=refs,
    )
    if 'classify' in stages:
        write_json(work / 'classified.json', classify.run(shapes, ctx))
    classified = need(work / 'classified.json', 'classify')

    decisions_path = Path(run['decisions']) if run.get('decisions') else work / 'decisions.json'
    if 'recommend' in stages:
        decisions = read_json_or(decisions_path, {})
        recommendations, corpus = recommend.run(classified, decisions, refs, profile, ctx)
        write_json(work / 'recommendations.json', recommendations)
        write_json(work / 'corpus.json', corpus)
        write_json(work / 'catalog-patch.json', recommendations['catalog_patch'])
    recommendations = need(work / 'recommendations.json', 'recommend')

    run['method_line'], run['method_note'] = method_lines(run, profile, inventory)
    write_json(work / 'run.json', run)
    report_path = Path(run['out']) if run.get('out') else work / 'report.html'
    if 'render' in stages:
        page = render.render({'cli': cli, 'profile': profile, 'rec': recommendations, 'run': run})
        report_path.write_text(page, encoding='utf-8')

    if APPLY_STAGE in stages:
        if not args.apply_catalog:
            raise SystemExit(
                'the apply stage rewrites a catalog: pass --apply-catalog <path> after the user reviews it'
            )
        target = args.apply_catalog.resolve()
        current = read_json(target) if target.exists() else {'version': '', 'commands': [], 'tokens': []}
        merged, changes = recommend.apply_patch(
            current, recommendations['catalog_patch'], recommendations['catalog_cleanup']
        )
        write_json(target, merged, indent=2)  # catalogs are hand-reviewed files: conventional indent
        print(f'applied {len(changes)} changes to {target}')
        for change in changes:
            print(f'  {change}')
        return 0

    print_summary(profile, recommendations, report_path, decisions_path)
    return 0


def print_summary(profile: dict, rec: dict, report: Path, decisions: Path) -> None:
    buckets = ', '.join(
        f'{name} {values["events"]}' for name, values in sorted(rec['buckets'].items(), key=lambda kv: -kv[1]['events'])
    )
    still = sum(values['still_fails'] for values in rec['buckets'].values())
    print(f'events: {profile["events"]} in {profile["shapes"]} shapes')
    print(f'buckets: {buckets}')
    print(f'still fails on latest: {still}; would work after fixes: {rec["estimate"]["works_after"]}')
    print(
        f'catalog entries: {len(rec["catalog_entries"])}; strings to fix: {len(rec["strings_to_fix"])}; '
        f'privacy: {profile.get("privacy_events", 0)} events with raw values'
    )
    if rec['judgment']:
        print(
            f'needs judgment: {len(rec["judgment"])} shapes; write {decisions} then rerun with --stages recommend,render'
        )
    for problem in rec['decision_problems']:
        print(f'decision problem: {problem}')
    print(f'report: {report}')


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, RuntimeError) as error:
        warn(str(error))
        sys.exit(2)
