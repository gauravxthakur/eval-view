"""Model comparison tests with EvalView.

Run with:
    pytest examples/model_comparison_test.py -v

Requires API keys:
    export ANTHROPIC_API_KEY=sk-ant-...
    export OPENAI_API_KEY=sk-...

Each test follows the same pattern:
    1. Call run_eval(model, query=...) to get a ModelResult
    2. Assert evalview.score(result) passes your threshold
    3. (Optional) assert specific content in result.output

pytest.mark.parametrize lets you test N models with one test function.
"""
import json
import re

import pytest
import evalview


# ---------------------------------------------------------------------------
# Basic: same test across multiple models
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["claude-opus-4-6", "gpt-4o", "claude-sonnet-4-6"])
def test_summarization(model):
    """All three models should produce a non-empty summary."""
    result = evalview.run_eval(
        model,
        query="Summarize in one sentence: Large language models are transformer-based "
              "neural networks trained on vast text corpora to predict the next token.",
    )
    assert evalview.score(result) > 0.8, f"{model} failed: {result.error or result.output[:100]}"
    assert len(result.output) > 10, f"{model} gave an empty response"


# ---------------------------------------------------------------------------
# Scored against an expected output (similarity-based)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["claude-opus-4-6", "gpt-4o"])
def test_factual_answer(model):
    """Models should identify Python as a high-level interpreted language."""
    result = evalview.run_eval(
        model,
        query="In one sentence, what kind of programming language is Python?",
        expected="Python is a high-level interpreted programming language.",
        threshold=0.4,  # lower because exact wording varies between models
    )
    assert evalview.score(result) >= 0.4, f"{model} score too low: {result.score:.2f}"


# ---------------------------------------------------------------------------
# Custom scorer — assert specific behavior in the output
# ---------------------------------------------------------------------------

def _contains_json(output, expected):
    """Return 1.0 if the output contains valid JSON, 0.0 otherwise."""
    match = re.search(r"\{.*?\}", output, re.DOTALL)
    if not match:
        return 0.0
    try:
        json.loads(match.group())
        return 1.0
    except json.JSONDecodeError:
        return 0.0


@pytest.mark.parametrize("model", ["claude-opus-4-6", "gpt-4o", "claude-sonnet-4-6"])
def test_json_output(model):
    """Models should return valid JSON when instructed."""
    result = evalview.run_eval(
        model,
        query='Reply with ONLY a JSON object: {"name": "Alice", "age": 30}',
        scorer=_contains_json,
        threshold=1.0,
    )
    assert evalview.score(result) == 1.0, (
        f"{model} did not return valid JSON.\nOutput: {result.output[:200]}"
    )


# ---------------------------------------------------------------------------
# Side-by-side comparison — find the best model for a task
# ---------------------------------------------------------------------------

def test_compare_and_rank():
    """Run models in parallel and verify the winner passes the threshold."""
    results = evalview.compare_models(
        query="What are the three laws of robotics? List them numbered.",
        models=["claude-sonnet-4-6", "gpt-4o"],
        threshold=0.8,
    )

    evalview.print_comparison_table(results)  # visible with pytest -s

    best = results[0]  # sorted best-first
    assert best.score >= 0.8, f"Best model score too low: {best.score:.2f}"
    assert any(w in best.output.lower() for w in ["1.", "first", "1)"]), (
        "Expected numbered laws in output"
    )


# ---------------------------------------------------------------------------
# Cost and latency assertions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "gpt-4o-mini"])
def test_cost_within_budget(model):
    """A one-word query should cost less than $0.001."""
    result = evalview.run_eval(model, query="Say 'hello'.")
    assert result.cost_usd < 0.001, f"{model} cost ${result.cost_usd:.6f}"
    assert result.latency_ms < 10_000, f"{model} took {result.latency_ms:.0f}ms"
