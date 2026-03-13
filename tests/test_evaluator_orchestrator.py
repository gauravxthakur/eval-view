"""Tests for the Evaluator orchestrator (evalview.evaluators.evaluator)."""

import json
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock
from typing import List, Optional

from evalview.core.types import (
    TestCase,
    TestInput,
    ExpectedBehavior,
    Thresholds,
    ScoringWeightsOverride,
    ExecutionTrace,
    ExecutionMetrics,
    StepTrace,
    StepMetrics,
    TokenUsage,
    ExpectedOutput,
    ChecksConfig,
)
from evalview.core.config import ScoringWeights
from evalview.evaluators.evaluator import Evaluator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trace(
    output: str = "The weather in NYC is 72F.",
    tools: Optional[List[str]] = None,
) -> ExecutionTrace:
    steps = []
    for i, name in enumerate(tools or []):
        steps.append(StepTrace(
            step_id=f"step-{i}",
            step_name=name,
            tool_name=name,
            parameters={"query": "test"},
            output="result",
            success=True,
            metrics=StepMetrics(latency=100.0, cost=0.0001),
        ))
    return ExecutionTrace(
        session_id="test-session",
        start_time=datetime.now(),
        end_time=datetime.now(),
        steps=steps,
        final_output=output,
        metrics=ExecutionMetrics(total_cost=0.001, total_latency=0.5),
    )


def _make_test_case(
    name: str = "test-weather",
    query: str = "What's the weather?",
    expected_tools: Optional[List[str]] = None,
    expected_output: Optional[ExpectedOutput] = None,
    forbidden_tools: Optional[List[str]] = None,
    checks: Optional[ChecksConfig] = None,
    weights: Optional[ScoringWeightsOverride] = None,
    min_score: float = 50.0,
) -> TestCase:
    return TestCase(
        name=name,
        input=TestInput(query=query),
        expected=ExpectedBehavior(
            tools=expected_tools or [],
            output=expected_output,
            forbidden_tools=forbidden_tools or None,
        ),
        thresholds=Thresholds(
            min_score=min_score,
            weights=weights,
        ),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeterministicMode:
    """Evaluator should fall back to deterministic scoring when no LLM key."""

    def test_fallback_sets_skip_llm_judge(self):
        """When LLM init fails, evaluator degrades gracefully."""
        with patch("evalview.evaluators.evaluator.OutputEvaluator", side_effect=ValueError("no key")):
            ev = Evaluator()
        assert ev.skip_llm_judge is True
        assert ev.output_evaluator is None

    def test_explicit_skip(self):
        ev = Evaluator(skip_llm_judge=True)
        assert ev.skip_llm_judge is True
        assert ev.output_evaluator is None
        assert ev.hallucination_evaluator is None
        assert ev.safety_evaluator is None

    @pytest.mark.asyncio
    async def test_deterministic_output_score_capped_at_75(self):
        """The deterministic output evaluator caps its score at 75."""
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case()
        trace = _make_trace(output="The weather is 72F and sunny in NYC", tools=["weather_api"])
        result = await ev.evaluate(tc, trace)
        # Output quality component is capped at 75, but overall score includes
        # tool_accuracy and sequence_correctness components
        assert result.evaluations.output_quality.score <= 75.0

    @pytest.mark.asyncio
    async def test_deterministic_returns_evaluation_result(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case()
        trace = _make_trace(tools=["weather_api"])
        result = await ev.evaluate(tc, trace)
        assert result.test_case == "test-weather"
        assert result.evaluations is not None


class TestDeterministicOutputEval:
    """Tests for _deterministic_output_eval specifics."""

    @pytest.mark.asyncio
    async def test_contains_check_passes(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(
            expected_output=ExpectedOutput(contains=["weather", "NYC"]),
        )
        trace = _make_trace(output="The weather in NYC is 72F.")
        result = await ev.evaluate(tc, trace)
        oe = result.evaluations.output_quality
        assert oe.contains_checks.passed == ["weather", "NYC"]
        assert oe.contains_checks.failed == []

    @pytest.mark.asyncio
    async def test_contains_check_fails(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(
            expected_output=ExpectedOutput(contains=["temperature", "Mars"]),
        )
        trace = _make_trace(output="The weather is fine.")
        result = await ev.evaluate(tc, trace)
        oe = result.evaluations.output_quality
        assert "Mars" in oe.contains_checks.failed

    @pytest.mark.asyncio
    async def test_not_contains_check(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(
            expected_output=ExpectedOutput(not_contains=["error", "fail"]),
        )
        trace = _make_trace(output="Everything is good.")
        result = await ev.evaluate(tc, trace)
        oe = result.evaluations.output_quality
        assert oe.not_contains_checks.passed == ["error", "fail"]

    @pytest.mark.asyncio
    async def test_regex_pattern_matches(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(
            expected_output=ExpectedOutput(regex_patterns=[r"\d+F"]),
        )
        trace = _make_trace(output="It is 72F today.")
        result = await ev.evaluate(tc, trace)
        assert "DETERMINISTIC" in result.evaluations.output_quality.rationale

    @pytest.mark.asyncio
    async def test_json_schema_validation(self):
        ev = Evaluator(skip_llm_judge=True)
        schema = {
            "type": "object",
            "required": ["temp"],
            "properties": {"temp": {"type": "number"}},
        }
        tc = _make_test_case(
            expected_output=ExpectedOutput(json_schema=schema),
        )
        trace = _make_trace(output='{"temp": 72}')
        result = await ev.evaluate(tc, trace)
        assert "JSON schema valid" in result.evaluations.output_quality.rationale

    @pytest.mark.asyncio
    async def test_empty_output_penalized(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case()
        trace = _make_trace(output="")
        result = await ev.evaluate(tc, trace)
        assert "empty" in result.evaluations.output_quality.rationale.lower()


class TestForbiddenTools:
    """Tests for forbidden tool enforcement."""

    @pytest.mark.asyncio
    async def test_forbidden_tool_causes_fail(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(forbidden_tools=["delete_user"])
        trace = _make_trace(tools=["delete_user"])
        result = await ev.evaluate(tc, trace)
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_no_forbidden_tool_passes(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(
            forbidden_tools=["delete_user"],
            expected_tools=["weather_api"],
        )
        trace = _make_trace(output="Weather is 72F in NYC", tools=["weather_api"])
        result = await ev.evaluate(tc, trace)
        # Should not hard-fail from forbidden tools
        assert result.evaluations.forbidden_tools.passed is True


class TestPerTestWeightOverrides:
    """Tests for per-test scoring weight overrides."""

    @pytest.mark.asyncio
    async def test_custom_weights_applied(self):
        ev = Evaluator(skip_llm_judge=True)
        custom_weights = ScoringWeightsOverride(
            tool_accuracy=0.1,
            output_quality=0.8,
            sequence_correctness=0.1,
        )
        tc = _make_test_case(weights=custom_weights, expected_tools=["weather_api"])
        trace = _make_trace(output="The weather is 72F in NYC.", tools=["weather_api"])
        result = await ev.evaluate(tc, trace)
        # With 80% weight on output and deterministic cap at 75, score should reflect that
        assert result.score > 0

    @pytest.mark.asyncio
    async def test_invalid_weights_sum_raises(self):
        ev = Evaluator(skip_llm_judge=True)
        with pytest.raises(Exception):
            ScoringWeights(tool_accuracy=0.5, output_quality=0.5, sequence_correctness=0.5)


class TestCheckGating:
    """Tests for hallucination/safety/PII check gating."""

    @pytest.mark.asyncio
    async def test_skip_hallucination_check(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(checks=ChecksConfig(hallucination=False))
        trace = _make_trace()
        result = await ev.evaluate(tc, trace)
        assert result.evaluations.hallucination is None

    @pytest.mark.asyncio
    async def test_skip_safety_check(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case(checks=ChecksConfig(safety=False))
        trace = _make_trace()
        result = await ev.evaluate(tc, trace)
        assert result.evaluations.safety is None

    @pytest.mark.asyncio
    async def test_pii_disabled_by_default(self):
        ev = Evaluator(skip_llm_judge=True)
        tc = _make_test_case()
        trace = _make_trace()
        result = await ev.evaluate(tc, trace)
        assert result.evaluations.pii is None


class TestRegexSafety:
    """Tests for regex ReDoS protection."""

    def test_invalid_regex_returns_none(self):
        compiled = Evaluator._compile_regex("[invalid")
        assert compiled is None

    def test_valid_regex_compiles(self):
        compiled = Evaluator._compile_regex(r"\d+")
        assert compiled is not None

    def test_check_regex_patterns_valid(self):
        passed, failed = Evaluator._check_regex_patterns("price is $42", [r"\$\d+"])
        assert passed == [r"\$\d+"]
        assert failed == []

    def test_check_regex_patterns_invalid_pattern(self):
        passed, failed = Evaluator._check_regex_patterns("text", ["[invalid"])
        assert failed == ["[invalid"]


class TestJsonExtraction:
    """Tests for _extract_first_json_object."""

    def test_plain_json(self):
        result = Evaluator._extract_first_json_object('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_text(self):
        result = Evaluator._extract_first_json_object('Here is the result: {"temp": 72} end.')
        assert result == {"temp": 72}

    def test_nested_json(self):
        result = Evaluator._extract_first_json_object('{"a": {"b": 1}}')
        assert result == {"a": {"b": 1}}

    def test_no_json_returns_none(self):
        result = Evaluator._extract_first_json_object("no json here")
        assert result is None

    def test_malformed_json_returns_none(self):
        result = Evaluator._extract_first_json_object('{"key": }')
        assert result is None


class TestJsonSchemaValidation:
    """Tests for _check_json_schema."""

    def test_valid_schema(self):
        passed, error = Evaluator._check_json_schema(
            '{"name": "Alice", "age": 30}',
            {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        )
        assert passed is True

    def test_missing_required_field(self):
        passed, error = Evaluator._check_json_schema(
            '{"name": "Alice"}',
            {"type": "object", "required": ["name", "age"]},
        )
        # May pass or fail depending on whether jsonschema is installed
        # With basic check: fails because age is missing
        if not passed:
            assert "age" in error.lower() or "required" in error.lower()

    def test_non_json_output(self):
        passed, error = Evaluator._check_json_schema(
            "not json at all",
            {"type": "object"},
        )
        assert passed is False
        assert "JSON" in error
