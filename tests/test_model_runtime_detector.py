from __future__ import annotations

from datetime import datetime, timezone

from evalview.core.diff import DiffEngine, DiffStatus, OutputDiff, TraceDiff
from evalview.core.golden import GoldenMetadata, GoldenTrace
from evalview.core.model_runtime_detector import analyze_model_runtime_change
from evalview.core.types import (
    ExecutionMetrics,
    ExecutionTrace,
    LLMCallInfo,
    Span,
    SpanKind,
    StepMetrics,
    StepTrace,
    TraceContext,
)


def _step(name: str = "lookup") -> StepTrace:
    return StepTrace(
        step_id="1",
        step_name=name,
        tool_name=name,
        parameters={},
        output="ok",
        success=True,
        metrics=StepMetrics(cost=0.0, latency=10.0),
    )


def _trace(model_id: str | None = None, provider: str | None = None, span_model: str | None = None) -> ExecutionTrace:
    now = datetime.now(timezone.utc)
    spans = []
    if span_model:
        spans.append(
            Span(
                span_id="sp1",
                trace_id="tr1",
                kind=SpanKind.LLM,
                name="llm",
                start_time=now,
                end_time=now,
                llm=LLMCallInfo(model=span_model, provider=provider or "openai"),
            )
        )
    return ExecutionTrace(
        session_id="s1",
        start_time=now,
        end_time=now,
        steps=[_step()],
        final_output="ok",
        metrics=ExecutionMetrics(total_cost=0.0, total_latency=10.0),
        trace_context=TraceContext(
            trace_id="tr1",
            root_span_id="sp1",
            spans=spans,
            start_time=now,
            end_time=now,
        ) if spans else None,
        model_id=model_id,
        model_provider=provider,
    )


def _golden(model_id: str | None = None, provider: str | None = None, span_model: str | None = None) -> GoldenTrace:
    return GoldenTrace(
        metadata=GoldenMetadata(
            test_name="sample",
            blessed_at=datetime.now(timezone.utc),
            score=90.0,
            model_id=model_id,
            model_provider=provider,
        ),
        trace=_trace(model_id=model_id, provider=provider, span_model=span_model),
        tool_sequence=["lookup"],
        output_hash="abc",
    )


def _drift_diff(
    *,
    test_name: str,
    runtime_fingerprint_changed: bool = False,
    model_changed: bool = False,
    severity: DiffStatus = DiffStatus.OUTPUT_CHANGED,
) -> TraceDiff:
    return TraceDiff(
        test_name=test_name,
        has_differences=True,
        tool_diffs=[],
        output_diff=OutputDiff(
            similarity=0.72,
            golden_preview="before",
            actual_preview="after",
            diff_lines=[],
            severity=severity,
        ),
        score_diff=-8.0,
        latency_diff=0.0,
        overall_severity=severity,
        model_changed=model_changed,
        golden_model_id="gpt-5.4" if model_changed else None,
        actual_model_id="gpt-5.4-mini" if model_changed else None,
        runtime_fingerprint_changed=runtime_fingerprint_changed,
        golden_runtime_fingerprint="openai/gpt-5.4",
        actual_runtime_fingerprint="openai/gpt-5.4-mini" if (runtime_fingerprint_changed or model_changed) else "openai/gpt-5.4",
    )


def test_diff_marks_declared_model_change():
    diff = DiffEngine().compare(
        _golden(model_id="gpt-5.4", provider="openai"),
        _trace(model_id="gpt-5.4-mini", provider="openai"),
        actual_score=80.0,
    )

    assert diff.model_changed is True
    assert diff.golden_model_id == "gpt-5.4"
    assert diff.actual_model_id == "gpt-5.4-mini"
    assert diff.runtime_fingerprint_changed is True


def test_diff_marks_runtime_fingerprint_change_from_spans():
    diff = DiffEngine().compare(
        _golden(model_id=None, provider=None, span_model="gpt-5.4"),
        _trace(model_id=None, provider=None, span_model="gpt-5.4-mini"),
        actual_score=80.0,
    )

    assert diff.model_changed is False
    assert diff.runtime_fingerprint_changed is True
    assert diff.golden_runtime_fingerprint == "openai/gpt-5.4"
    assert diff.actual_runtime_fingerprint == "openai/gpt-5.4-mini"


def test_run_level_detector_declared_change_is_high_confidence():
    summary = analyze_model_runtime_change(
        [("sample", _drift_diff(test_name="sample", model_changed=True))]
    )

    assert summary.classification == "declared"
    assert summary.confidence == "high"
    assert summary.declared_count == 1


def test_run_level_detector_suspects_runtime_update_from_coordinated_drift():
    diffs = [
        (f"test-{i}", _drift_diff(test_name=f"test-{i}", runtime_fingerprint_changed=(i < 2)))
        for i in range(3)
    ]

    summary = analyze_model_runtime_change(diffs)

    assert summary.classification == "suspected"
    assert summary.confidence in {"medium", "high"}
    assert summary.behavioral_drift_count == 3
    assert summary.affected_count >= 2
