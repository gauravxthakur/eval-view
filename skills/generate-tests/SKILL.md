---
name: generate-tests
description: Generate EvalView test cases — either from a SKILL.md file using LLM-powered generation, or by capturing real agent interactions through a proxy.
---

# Generate Tests

Use this skill when the user wants to create test cases for their AI agent or skill without writing YAML by hand.

## Four approaches

### 1. Generate tests from a SKILL.md file

Use the `generate_skill_tests` MCP tool to auto-generate a test suite from a skill definition. This reads the SKILL.md and produces YAML test cases covering explicit triggers, implicit triggers, contextual triggers, and negative cases.

**Steps:**
1. Ask the user which SKILL.md to generate tests for (or detect it from context).
2. Call `generate_skill_tests` with:
   - `skill_path`: path to the SKILL.md file
   - `output_path` (optional): where to save the generated YAML
   - `count` (optional): number of test cases (default: 10)
3. After generation, offer to run the tests with `run_skill_test`.

**CLI equivalent:**
```
evalview skill generate-tests .claude/skills/my-skill/SKILL.md --auto
evalview skill generate-tests .claude/skills/my-skill/SKILL.md -c 20 -o tests/my-skill-tests.yaml
```

### 2. Create individual test cases manually

Use the `create_test` MCP tool to create a single test YAML file from a description.

**Steps:**
1. Gather from the user: test name, query, expected tools, forbidden tools, expected output keywords, and minimum score.
2. Call `create_test` with the parameters.
3. After creating the test, call `run_snapshot` to establish the golden baseline.

### 3. Capture real interactions

Use the CLI `evalview capture` command to proxy real agent traffic and save interactions as test YAMLs automatically. This records the query, output, and tool calls from live usage.

**CLI equivalent:**
```
evalview capture --agent http://localhost:8080/execute --output-dir tests/test-cases
evalview capture --multi-turn  # saves all turns as one multi-turn conversation test
```

### 4. Validate a skill before testing

Use `validate_skill` to check a SKILL.md for correct structure and completeness before generating tests from it.

## Running generated tests

After generating tests, execute them with `run_skill_test`:
- `test_file`: path to the generated YAML
- `no_rubric: true` for fast deterministic-only checks (no LLM cost)
- `verbose: true` for detailed output on all tests

**CLI equivalent:**
```
evalview skill test tests/my-skill-tests.yaml
evalview skill test tests/my-skill-tests.yaml --no-rubric  # fast, $0
evalview skill test tests/my-skill-tests.yaml --verbose --model claude-sonnet-4-20250514
```
