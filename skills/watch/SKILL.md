---
name: watch
description: Start EvalView watch mode to automatically re-run regression checks whenever project files change.
---

# Watch Mode

Use this skill when the user wants continuous regression monitoring during development. Watch mode observes file changes and automatically re-runs `evalview check` with debounced triggers.

## What this does

EvalView's watch mode uses `watchdog` to monitor directories for file changes (`.py`, `.yaml`, `.yml`, `.json`, `.md`, `.txt`, `.toml`, `.cfg`, `.ini`). When a change is detected, it runs a regression check via the `gate()` API and displays a live scorecard with pass/fail status, score deltas, tool changes, and streak tracking.

## How to start watch mode

Watch mode is a CLI command (not an MCP tool). Help the user run it:

```
evalview watch
```

### Common options

- `--quick` — Skip LLM judge, deterministic checks only ($0 cost, sub-second)
- `--path src/ --path tests/` — Watch specific directories (default: current directory)
- `--test "my-test"` — Only check a specific test by name
- `--test-dir tests/evalview` — Path to test cases directory (default: `tests`)
- `--interval 1` — Debounce interval in seconds (default: 2.0)
- `--fail-on REGRESSION,TOOLS_CHANGED` — Comma-separated statuses that count as failure (default: REGRESSION)
- `--sound` — Terminal bell on regression

### Examples

```
# Basic: watch everything, full checks
evalview watch

# Fast development loop: no LLM judge, 1-second debounce
evalview watch --quick --interval 1

# Watch specific directories and one test
evalview watch --path src/ --path tests/ --test "calculator-division"

# Strict mode: fail on any behavioral change
evalview watch --fail-on REGRESSION,TOOLS_CHANGED,OUTPUT_CHANGED --sound
```

## Prerequisites

Watch mode requires the `watchdog` package. If not installed:

```
pip install evalview[watch]
```

## Notes

- Watch mode excludes `.evalview/`, `.git/`, `venv/`, `node_modules/`, `__pycache__/`, and other common non-source directories automatically.
- The initial check runs immediately on startup before watching begins.
- Results include a live scorecard with pass counts, regression counts, health percentage, and streak info.
- `--quick` mode is ideal for tight development loops since it costs nothing and runs in sub-second time.
