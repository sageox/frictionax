---
name: friction-analysis
description: Analyze CLI friction telemetry (unknown commands and flags that humans and AI coworkers hit) and turn a friction export into root causes, concrete fixes (catalog entries with an AUTO/ASK rule, flags to add, stale strings, tests) and a self-contained HTML report. Use when asked to "analyze friction", make a "friction report", investigate "agent hallucinations in my CLI", "update the friction catalog", or find out "what commands are agents guessing". Works for any CLI; replays every failure against the current binary in a sandbox to separate "still fails" from "fixed since".
---

# Friction analysis

Most "agent hallucinations" in a CLI are fixable, and many are the CLI's own fault:
stale hints, a shell quirk the CLI could absorb, a catalog that matches too
strictly, sibling commands with different flags. This skill finds out which, with
deterministic rules first and judgment only for what the rules cannot place.

All paths below are relative to this skill's directory (`<skill>`). Every script
is Python 3 stdlib only. **Always run Python with `-I`**: the export is untrusted
data, and isolated mode keeps the current directory and `PYTHONPATH` off the
import path.

## Inputs

| Input | Required | Notes |
|---|---|---|
| Friction export | yes | CSV, JSON or JSONL. Columns such as `timestamp, kind, actor, agent_type, input_redacted, error_msg, client_version`; aggregated pattern exports (`pattern, total_count`) also work. |
| CLI name | yes | As users type it, e.g. `acme`. |
| Current binary | strongly recommended | Without it nothing can be checked against the latest build. |
| Catalog | if the CLI uses frictionax | The `commands`/`tokens` JSON the CLI embeds or syncs. |
| CLI source repo | optional | Finds the strings that taught agents a broken command (`cli-taught`). |
| Side-effect env | only for `--exec` | Ask the user. See Safety. |

Formats for the export, an optional per-CLI config, a hand-written inventory and
`decisions.json` are in `references/formats.md`.

## Safety (read before running anything)

- **Probe mode is the default.** Without `--exec`, the binary only ever runs as
  `<cli> [path] --help` (and `--version`), inside the sandbox.
- **`--exec` executes the CLI once per distinct shape.** Before using it, ask the
  user for the environment variables that turn off their CLI's telemetry,
  daemons, auto-update and network calls, write them to an env file, and pass
  `--env-file`. The script refuses `--exec` without one. When the user does not
  know, stay in probe mode.
- The sandbox always provides: a throwaway `HOME` and XDG dirs, an empty non-git
  working directory, an empty starting environment, HTTP routed to a loopback
  discard port, closed stdin, a timeout that kills the process group. Env files
  cannot override `HOME`, `PATH`, `TMPDIR` or any `XDG_*`, so a replay can never
  touch the real home directory. Shapes whose command contains a destructive verb
  (delete, upgrade, push, login, ...) are never executed.
- **Privacy.** Every invocation is reduced to an anonymized shape: command-path
  words and flag names survive, every other token becomes a placeholder
  (`<path>`, `<id>`, `<n>`, `<text>`, ...). Intermediate files and the report
  hold only shapes and counts, and the renderer refuses to write a report that
  contains a home path, email, UUID, secret or URL. Unknown command words are kept
  verbatim because they are the signal; skim the report before sharing it.
- Work in a fresh directory of its own (`--work`); the export is copied there
  read-only. Never put the work directory inside a repository you commit from.

## Pipeline

### 1. Deterministic pass

```bash
python3 -I <skill>/scripts/analyze.py --work ./friction-work \
  --export ./friction-export.csv --cli acme \
  --binary "$(command -v acme)" \
  --catalog ./path/to/default_catalog.json \
  --source ./path/to/acme-repo
```

Stages: `inventory` (command x flag tree from `--help`) -> `normalize`
(anonymize, dedupe) -> `replay` (exec, opt-in) -> `refs` (stale references in
source and help examples) -> `classify` -> `recommend` -> `render`.

Optional flags: `--exec --env-file side-effects.env` (real replay),
`--feature-env-file features.env` (env that enables feature-gated commands; finds
the `gated` bucket), `--config acme-friction.json`, `--dry-run` (print the argv
each shape would replay), `--skip-replay` (never run the binary).

### 2. Judgment (only the residue)

Read `friction-work/classified.json`. Shapes with `"needs_judgment": true` carry
their `candidates`. For each, decide a bucket and, where one exists, a target,
then write `friction-work/decisions.json`:

```json
{"decisions": {
  "update": {"bucket": "alias", "target": "upgrade", "reason": "agents mean self-upgrade"},
  "repo": {"bucket": "missing-feature", "reason": "no repository command exists"}
}}
```

You may also override a deterministic bucket or target the same way. Keys are the
shape `key` exactly as written in `classified.json`.

### 3. Recommend and render

```bash
python3 -I <skill>/scripts/analyze.py --work ./friction-work --stages recommend,render
```

Settings persist in `friction-work/run.json`, so later passes need only `--work`.

## Buckets (ordered rules, first match wins)

| Bucket | Rule | Fix |
|---|---|---|
| `noise` | deliberate probes (`bogus`, `--nonexistent-flag`) | none; corpus says "stays an error" |
| `fixed` | works on the current binary | none; confirms the fix shipped |
| `gated` | feature-gated command invoked while disabled | say "not enabled", not "unknown"; find where agents learned it |
| `word-split` | `unknown command "plan list"`: a multi-word path in one argv (zsh does not split `$var`) | when the joined argument resolves, split it and run |
| `cli-taught` | the CLI's own hint, help example or doc names it | fix the string; test that every reference resolves |
| `catalog-miss` | an entry exists but exact matching misses trailing args, or its target is gone | prefix matching; retarget; validate targets |
| `bad-fuzzy` | edit-distance suggests an unrelated (maybe mutating) command | scale distance to word length; never fuzzy-suggest mutations |
| `flag-drift` | a flag the command lacks, often borrowed from a sibling | one flag vocabulary per sibling family, not aliases |
| `alias` | a desire path with a clear target (`plan show` -> `plan view`) | catalog entry |
| `missing-feature` | judgment only: agents want something that does not exist | file an issue |

## Judgment rules

- **AUTO rule.** Auto-execute only when the verb is preserved (same word, synonym
  such as show/view, plural, typo, or the same word under another parent) AND the
  target mutates nothing. Everything else is ASK (suggest-only). The script applies
  this; keep it when you override targets.
- Pick the target an agent meant, not the closest string. When nothing fits, say
  `missing-feature` rather than forcing an alias.
- A path or ID typed where a command belongs is `noise`; a real word with no
  target is `missing-feature`.

## Outputs (in `--work`)

| File | What it is |
|---|---|
| `report.html` | the self-contained report: Summary, Where it leaks, Map changes, Flag vocabulary, Privacy, and collapsed Implementation notes |
| `catalog-patch.json` | proposed `commands` and `tokens` entries, each simulated against frictionax's matching |
| `corpus.json` | every anonymized shape with its expected outcome, for a regression test |
| `recommendations.json` | everything above plus strings to fix, flags to add, engine fixes, tests |
| `classified.json`, `shapes.json`, `profile.json`, `replay.json`, `refs.json`, `inventory.json` | intermediate, inspectable |

## Reporting back

Lead with the headline (how many failures were the CLI's own), the top three
actions, and "still fails on latest". Point to `report.html`. Recommend the five
guard tests listed in the report (corpus, reference coherence, catalog validity,
flag vocabulary, redaction), each proven red-first. Do not paste raw export rows
anywhere.

## Self-check

`python3 -I <skill>/tests/smoke_test.py` runs the whole pipeline on a synthetic
export for a fictional CLI and must print `PASSED`.
