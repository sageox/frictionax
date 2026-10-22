# Formats

## Export

CSV, JSON (a list, or an object with an `events` / `patterns` list) or JSONL.
Column names are matched loosely; the first present alias wins.

| Field | Accepted names |
|---|---|
| input | `input_redacted`, `input`, `pattern`, `argv`, `command_line` |
| error | `error_msg`, `error`, `message`, `error_message` |
| kind | `kind`, `failure_kind` |
| actor | `actor` (`agent`, `human`) |
| agent type | `agent_type`, `agent` |
| version | `client_version`, `version`, `cli_version`, `latest_version` |
| time | `timestamp`, `ts`, `created_at`, `last_seen`, `first_seen` (ISO 8601 or epoch) |
| weight | `total_count`, `count` (aggregated pattern exports; default 1) |

Error messages are parsed for cobra, kong, urfave/cli, clap, argparse and
commander.js. Add patterns for a custom dispatcher in the config.

## Config (`--config`, optional)

```json
{
  "free_text_flags": ["--content", "--message"],
  "id_patterns": ["^tm_[a-z0-9]+$"],
  "noise_patterns": ["\\bselftest\\b"],
  "gated": ["cart", "beta export"],
  "verb_groups": [["show", "inspect"]],
  "destructive_verbs": ["rotate"],
  "flag_groups": [["--limit", "--page-size"]],
  "unknown_command_patterns": ["unknown command or invalid id: (?P<tok>\\S+)"],
  "unknown_flag_patterns": []
}
```

Every key is optional and extends the built-in defaults. Unknown keys are an error.

## Inventory (`--inventory`, instead of `--help` probing)

```json
{
  "cli": "acme",
  "commands": {
    "": {"children": ["plan", "status"], "flags": {"--verbose": false}},
    "plan": {"children": ["list", "view"]},
    "plan list": {"flags": {"--json": false, "--limit": true}},
    "plan view": {"accepts_args": true}
  }
}
```

`flags` maps a flag to whether it takes a value (a plain list is accepted too).
Children may be implied by keys alone. Use this when `--help` cannot be parsed or
the CLI must not run at all.

## Decisions (`decisions.json`)

```json
{"decisions": {"<shape key>": {"bucket": "alias", "target": "plan view", "reason": "..."}}}
```

`bucket` is any bucket except `unclassified`. `target` is a command path without
the CLI name; omit it for buckets without one. Problems (unknown keys, invalid
buckets) are reported, never guessed.

## Corpus (`corpus.json`)

One row per shape: `argv` (placeholders intact), `joined` / `joined_at` (a
multi-word path that arrived as one argv element), `events`, `bucket`, and
`expected`: `resolves`, `auto:<target>`, `suggests:<target>`, `gated`, or `error`.
A corpus test fills placeholders with inert values, runs the parser and the
friction handler on synthesized errors, and asserts `expected` without executing
any command.
