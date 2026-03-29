# EvalView Operating Model

> Frontier-lab rigor, startup-team practicality.

This is the operating model behind EvalView.

The goal is not to imitate Anthropic's scale. The goal is to give smaller teams the same discipline:

- shorter idea -> implementation -> eval -> ship loops
- behavior-first testing instead of vague "quality" claims
- more automation in execution, more human judgment in architecture and review

EvalView should help teams move faster **without** needing unlimited model access, huge eval budgets, or a dedicated platform team.

## Core Principles

### 1. Build for the loop, not the demo

The valuable loop is:

1. capture behavior
2. snapshot the current baseline
3. detect drift
4. classify what changed
5. auto-heal the safe cases
6. turn failures into better evals

That loop is the product.

### 2. Behavior over benchmark theater

More evals do not automatically make an agent better.

EvalView should bias users toward:

- targeted tests
- behavior tags
- focused slices
- real production failure modes

Examples:

- `tool_use`
- `retrieval`
- `clarification`
- `multi_step`
- `handoff`
- `memory`

### 3. Deterministic checks first, LLM judgment second

For most teams, cost and trust matter.

EvalView should prefer:

- tool diffs
- parameter diffs
- output similarity
- latency/cost thresholds
- trace inspection

Use LLM-as-judge where it adds signal, not by default everywhere.

### 4. Human judgment stays at the top of the stack

Agents should accelerate implementation.
Humans should still own:

- product scope
- invariants
- architecture
- review
- release decisions

EvalView should help users apply automation safely, not hand over control blindly.

### 5. Small-team constraints are a product requirement

Assume the user does **not** have:

- frontier-lab budgets
- full internal model access
- a platform team
- time to maintain giant eval suites

Design for:

- cheap targeted runs
- deterministic signals
- clear diffs
- bounded retries
- reviewable reports

## How EvalView Should Run EvalView

Dogfooding is not enough.
The dogfood loop should be explicit and repeatable.

### Internal build loop

For every meaningful change:

1. write a short spec
2. implement with agent help
3. review hard
4. run the relevant behavior-tagged slice
5. snapshot intentional changes
6. run `check`
7. ship

### Internal ship gate

Before shipping:

- run the tagged behavior slice most affected by the change
- run one broader regression slice if the change is core
- review the HTML or CLI diff output if behavior changed
- only re-snapshot when the new behavior is intentional

### Bug-to-eval rule

Every meaningful failure should become one of:

- a new regression test
- a stronger existing test
- a new behavior tag
- a documented invariant

The system compounds when bugs become assets.

## Solo-Dev Workflow

This is the intended solo or small-team operating loop:

### Step 1: Specify

Before coding, write:

- the problem
- the invariants
- the acceptable behavior
- the tests or tags affected

### Step 2: Delegate

Use coding agents for:

- implementation
- refactors
- repetitive edits
- initial test drafts

### Step 3: Review

Treat coding as curation.

Your job is:

- reject weak architecture
- reject loose semantics
- reject vague output
- keep product scope sharp

### Step 4: Eval

Run EvalView on the change:

- focused tagged slice first
- then broader check if needed

### Step 5: Ship

Only ship once the intended behavior is:

- explained
- tested
- compared
- reviewable

## What This Means for the Product Roadmap

Prioritize features that tighten the loop:

1. stronger MCP and agent-native workflows
2. better trace inspection
3. behavior-tagged organization
4. clearer classification and explanations
5. trace -> candidate eval generation

Avoid broad platform sprawl that widens the product without sharpening the loop.

## Product Positioning

EvalView should align with frontier-lab standards on:

- eval discipline
- behavior regression testing
- fast iteration loops
- serious review flows

EvalView should **not** assume frontier-lab resources.

The positioning is:

**Anthropic-style eval discipline for normal teams.**

Or more concretely:

**Frontier-lab rigor, startup-team practicality.**

## Practical Checklist

When deciding whether to build something, ask:

- Does this shorten the loop from change -> eval -> decision?
- Does this help classify drift more clearly?
- Does this help a small team move faster without losing trust?
- Does this reduce human toil without hiding behavior changes?

If the answer is no, it is probably not a priority.
