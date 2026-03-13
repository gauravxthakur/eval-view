# Test Generation

> EvalView can now generate a draft regression suite in one command. Use it when you have an agent endpoint or traffic logs but no meaningful test suite yet.

## Two Generation Paths

### 1. Generate from a live agent

```bash
evalview generate --agent http://localhost:8000
```

What it does:
- probes the agent with diverse prompts
- discovers tool-aware behavior paths
- clusters duplicate trajectories
- writes draft YAML tests to `tests/generated/`
- writes `tests/generated/generated.report.json`

Use this when:
- you have no production traffic yet
- you want a fast draft suite from a running endpoint
- you need tool-path coverage quickly

### 2. Generate from existing traffic

```bash
evalview generate --from-log traffic.jsonl
```

Supported log formats:
- `jsonl`
- `openai`
- `evalview`

Use this when:
- you already have staging or production logs
- you want to bootstrap tests from real behavior
- you do not want to probe a live endpoint directly

## What Gets Generated

Each generated test is native EvalView YAML with:
- inferred tool expectations
- output contains / not_contains checks when stable
- thresholds from observed behavior
- generation metadata in `meta`

Generated tests are marked as drafts:

```yaml
meta:
  generated_by: evalview generate
  review_status: draft
  confidence: high
  rationale: Observed tool path: weather_api
  behavior_class: tool_path
```

## Approval Workflow

Generated tests are intentionally blocked from snapshotting until reviewed.

```bash
evalview snapshot tests/generated --approve-generated
```

That command:
1. updates generated YAML files to `review_status: approved`
2. stamps `approved_at`
3. snapshots approved tests as baselines

If you forget the flag, EvalView refuses to baseline draft-generated tests.

## CI Review Workflow

Every generated suite writes a report file:

```bash
tests/generated/generated.report.json
```

Turn it into a PR comment:

```bash
evalview ci comment --results tests/generated/generated.report.json --dry-run
```

The comment includes:
- discovered tools
- generated behavior paths
- coverage gaps
- approval instructions

## Recommended Flow

### Cold start

```bash
evalview generate --agent http://localhost:8000
evalview ci comment --results tests/generated/generated.report.json --dry-run
evalview snapshot tests/generated --approve-generated
evalview check tests/generated
```

### Existing traffic

```bash
evalview generate --from-log traffic.jsonl
evalview snapshot tests/generated --approve-generated
evalview check tests/generated
```

## Important Options

```bash
evalview generate [OPTIONS]

Options:
  --agent URL                  Agent endpoint URL
  --from-log PATH              Build suite from logs instead of live probing
  --log-format FORMAT          auto|jsonl|openai|evalview
  --budget N                   Max probe runs or imported entries
  --out DIR                    Output directory (default: tests/generated)
  --seed FILE                  Newline-delimited seed prompts
  --include-tools LIST         Focus on specific tools
  --exclude-tools LIST         Avoid specific tools
  --allow-live-side-effects    Allow side-effecting prompts
  --dry-run                    Preview without writing files
```

## What It Does Well

- zero-to-suite onboarding
- tool-path clustering
- clarification and multi-turn draft detection
- schema-aware probing when tool discovery is available
- safe-mode probing by default

## Current Limits

- generated assertions are conservative by design
- multi-turn generation is currently strongest for clarification-followup flows
- safety contracts are inferred heuristically unless the tool schema is explicit

That is intentional. EvalView generates draft regression tests, not blind truth claims.

## Relationship to Other Commands

- `generate`: create the first draft suite
- `capture`: record real interactions as tests
- `expand`: create variations from an existing seed test
- `record`: interactive one-by-one test recording

Use `generate` for onboarding, `capture` for real traffic, and `expand` when you already have a strong seed test.
