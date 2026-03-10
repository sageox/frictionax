# frictionx

CLI friction detection, correction, and telemetry for Go command-line tools.

When users or AI agents type the wrong command, `frictionx` detects the error, suggests corrections, and optionally auto-executes high-confidence fixes. Friction events are collected for analytics, revealing "desire paths" — patterns where users consistently expect something that doesn't exist yet.

Built by [SageOx](https://sageox.ai). Extracted from the [ox CLI](https://github.com/sageox/ox).

## Why this exists

Traditional CLIs fail hard on unknown commands:

```
Error: unknown command "agent-list"
Run 'mycli --help' for usage.
```

frictionx transforms failure into guidance:

```
Error: unknown command "agent-list"

Did you mean?
    mycli agent list
```

For AI agents, this is even more powerful. Coding agents (Claude Code, Cursor, Copilot, etc.) frequently hallucinate CLI commands and flags. Instead of failing hard, frictionx detects intent, suggests the right command, and teaches the agent the correct syntax — all without permanent aliases cluttering the interface.

The term "desire path" comes from urban planning — the worn trails across lawns where people actually walk. When many users or agents make the same "mistake," that's a signal about how the tool *should* work. frictionx surfaces these patterns through analytics so you can make data-driven decisions about your CLI's design.

**[Read the full design philosophy →](https://github.com/sageox/ox/blob/main/docs/ai/specs/friction-philosophy.md)**

## Features

- **Error detection** — Classifies CLI errors (unknown commands, flags, typos, missing args)
- **Smart suggestions** — Catalog remaps, token fixes, and Levenshtein fallback
- **Auto-execute** — High-confidence catalog matches run automatically
- **Agent-aware** — Detects human vs AI agent context, formats output accordingly
- **Privacy-first** — Built-in secret redaction, path bucketing, field truncation
- **Telemetry** — Background event collection with rate limiting and catalog sync
- **Pluggable** — Works with any CLI framework via adapters (Cobra included)

## Install

```bash
go get github.com/sageox/frictionx
```

## Quick Start

```go
package main

import (
    "fmt"
    "os"

    "github.com/sageox/frictionx"
    frictioncobra "github.com/sageox/frictionx/adapters/cobra"
    "github.com/spf13/cobra"
)

func main() {
    root := &cobra.Command{Use: "mycli"}
    // ... add subcommands ...

    // set up friction handling
    adapter := frictioncobra.NewCobraAdapter(root)
    catalog := frictionx.NewFrictionCatalog("mycli")
    handler := frictionx.NewHandler(adapter, catalog)

    err := root.Execute()
    if err != nil {
        result := handler.HandleWithAutoExecute(os.Args[1:], err)
        if result != nil && result.Suggestion != nil {
            fmt.Fprintln(os.Stderr, frictionx.FormatSuggestion(result.Suggestion, false))
        }
        os.Exit(1)
    }
}
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  CLI Command                         │
│          User types: mycli agent prine               │
└──────────────────────┬──────────────────────────────┘
                       │ error
                       ▼
┌─────────────────────────────────────────────────────┐
│              frictionx.Handler                       │
│  1. Parse error (CLIAdapter)                         │
│  2. Suggest correction (SuggestionEngine)            │
│     catalog remap → token fix → levenshtein          │
│  3. Detect actor (ActorDetector)                     │
│  4. Build FrictionEvent (with redaction)             │
│  5. Determine action (auto-execute vs suggest)       │
└──────────────────────┬──────────────────────────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
     Auto-execute          Suggest only
     (confidence >= 0.85)  (show "did you mean?")
              │                 │
              └────────┬────────┘
                       ▼
┌─────────────────────────────────────────────────────┐
│           frictionx.Collector (optional)             │
│  Buffer events → periodic flush → API submission     │
│  Rate limiting via X-Friction-Sample-Rate            │
│  Catalog updates from server responses               │
└─────────────────────────────────────────────────────┘
```

## Components

### Core Library (`github.com/sageox/frictionx`)

Zero external dependencies. Provides:

| Type | Purpose |
|------|---------|
| `Handler` | Orchestrates error parsing, suggestion, and event creation |
| `FrictionCatalog` | Thread-safe catalog with literal + regex command mappings |
| `SuggestionEngine` | Chains catalog -> token -> levenshtein suggestions |
| `Collector` | Background buffering and periodic API submission |
| `Client` | HTTP client with rate limiting and catalog sync |
| `RingBuffer` | Bounded circular buffer with deduplication |
| `CatalogCache` | On-disk catalog persistence |

### Extension Points

| Interface | Purpose | Default |
|-----------|---------|---------|
| `CLIAdapter` | Parse errors from your CLI framework | Use `adapters/cobra` |
| `ActorDetector` | Detect human vs AI agent context | `EnvActorDetector` (checks `CI` env) |
| `Redactor` | Strip secrets from event data | `NoOpRedactor` / use `redactors/secrets` |
| `Suggester` | Custom suggestion sources | Catalog + Levenshtein chain |

### Cobra Adapter (`github.com/sageox/frictionx/adapters/cobra`)

Ready-to-use adapter for [spf13/cobra](https://github.com/spf13/cobra) CLIs.

### Secret Redactor (`github.com/sageox/frictionx/redactors/secrets`)

25 built-in patterns covering AWS, GitHub, GitLab, Slack, Stripe, JWTs, connection strings, and more.

```go
import "github.com/sageox/frictionx/redactors/secrets"

redactor := secrets.New()
handler := frictionx.NewHandler(adapter, catalog, frictionx.WithRedactor(redactor))
```

## Try it out

The repo includes example binaries you can build and run to see frictionx in action — a sample telemetry server and a CLI that talks to it. These are learning tools, not production services.

### Example server

A minimal HTTP server that collects friction events into SQLite and serves a dashboard.

```bash
# build and run the example server
go run github.com/sageox/frictionx/cmd/frictionx-server@latest --port=8080 --db=./friction.db
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/friction` | POST | Submit friction events |
| `/api/v1/friction/status` | GET | Health + event count |
| `/api/v1/friction/summary` | GET | Aggregated patterns |
| `/api/v1/friction/catalog` | GET/PUT | Manage catalog |
| `/dashboard` | GET | HTML dashboard |

### Example CLI

A companion CLI that reports friction events to the server and queries for summaries.

```bash
# build and run the example CLI
go run github.com/sageox/frictionx/cmd/frictionx@latest --help

# report a friction event
go run github.com/sageox/frictionx/cmd/frictionx@latest report \
  --kind=unknown-command --input="mycli agent prine" --command=agent

# check server status
go run github.com/sageox/frictionx/cmd/frictionx@latest status

# view friction summary
go run github.com/sageox/frictionx/cmd/frictionx@latest summary --since=24h --limit=20

# manage catalog
go run github.com/sageox/frictionx/cmd/frictionx@latest catalog get
go run github.com/sageox/frictionx/cmd/frictionx@latest catalog set --file=catalog.json
```

## License

MIT -- see [LICENSE](LICENSE).
