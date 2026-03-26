"""Model/runtime change detection utilities.

This module layers three signals:
1. Declared model changes from adapter-reported model IDs
2. Runtime fingerprint changes from observed model labels in the trace
3. Coordinated behavioral drift across multiple tests in the same check run
"""
from __future__ import annotations

from math import ceil
from typing import Any, Iterable, List, Optional

from pydantic import BaseModel, Field

def _clean_model_label(model_id: str, provider: Optional[str] = None) -> str:
    return f"{provider}/{model_id}" if provider else model_id


def extract_trace_model_labels(
    trace: Any,
    *,
    fallback_model_id: Optional[str] = None,
    fallback_provider: Optional[str] = None,
) -> List[str]:
    """Extract a stable list of observed model labels from a trace-like object."""
    labels: List[str] = []
    seen: set[str] = set()

    model_id = getattr(trace, "model_id", None) or fallback_model_id
    provider = getattr(trace, "model_provider", None) or fallback_provider
    if model_id:
        label = _clean_model_label(str(model_id), str(provider) if provider else None)
        if label not in seen:
            seen.add(label)
            labels.append(label)

    trace_context = getattr(trace, "trace_context", None)
    spans = getattr(trace_context, "spans", None) if trace_context is not None else None
    for span in spans or []:
        llm = getattr(span, "llm", None)
        span_model = getattr(llm, "model", None) if llm is not None else None
        if not span_model:
            continue
        span_provider = getattr(llm, "provider", None) or provider
        label = _clean_model_label(str(span_model), str(span_provider) if span_provider else None)
        if label not in seen:
            seen.add(label)
            labels.append(label)

    return labels


def fingerprint_from_labels(labels: Iterable[str]) -> Optional[str]:
    unique = [label for label in dict.fromkeys(label.strip() for label in labels if label)]
    if not unique:
        return None
    return " | ".join(unique)


def _is_true(value: Any) -> bool:
    return value is True


def _string_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _has_structural_tool_change(diff: Any) -> bool:
    return any(
        getattr(td, "type", None) in ("added", "removed", "reordered")
        or (
            getattr(td, "type", None) == "changed"
            and getattr(td, "golden_tool", None) != getattr(td, "actual_tool", None)
        )
        for td in getattr(diff, "tool_diffs", []) or []
    )


def _is_behavioral_drift(diff: Any) -> bool:
    status = getattr(getattr(diff, "overall_severity", None), "value", getattr(diff, "overall_severity", None))
    if status == "passed":
        return False
    if _has_structural_tool_change(diff):
        return False
    return status in ("output_changed", "regression")


class ModelRuntimeChangeSummary(BaseModel):
    """Run-level model/runtime change signal."""

    classification: str = "none"  # none | declared | suspected
    confidence: str = "low"  # low | medium | high
    affected_count: int = 0
    declared_count: int = 0
    fingerprint_changed_count: int = 0
    behavioral_drift_count: int = 0
    retry_persisted_count: int = 0
    retry_recovered_count: int = 0
    drift_ratio: float = 0.0
    baseline_fingerprints: List[str] = Field(default_factory=list)
    current_fingerprints: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.classification != "none"


def analyze_model_runtime_change(
    diffs: list[tuple[str, Any]],
    *,
    healing_summary: Optional[Any] = None,
) -> ModelRuntimeChangeSummary:
    """Infer whether a check run looks like a model/runtime update."""
    total = len(diffs)
    declared = [d for _, d in diffs if _is_true(getattr(d, "model_changed", False))]
    fingerprint_changed = [d for _, d in diffs if _is_true(getattr(d, "runtime_fingerprint_changed", False))]
    behavioral = [d for _, d in diffs if _is_behavioral_drift(d)]

    baseline_fingerprints = sorted(
        {
            fp for _, d in diffs
            for fp in [_string_or_none(getattr(d, "golden_runtime_fingerprint", None))]
            if fp
        }
    )
    current_fingerprints = sorted(
        {
            fp for _, d in diffs
            for fp in [_string_or_none(getattr(d, "actual_runtime_fingerprint", None))]
            if fp
        }
    )

    retry_persisted_count = 0
    retry_recovered_count = 0
    if healing_summary:
        for result in getattr(healing_summary, "results", []) or []:
            trigger = getattr(getattr(result, "diagnosis", None), "trigger", None)
            trigger_value = getattr(trigger, "value", trigger)
            if trigger_value != "model_update":
                continue
            if getattr(result, "healed", False):
                retry_recovered_count += 1
            else:
                retry_persisted_count += 1

    drift_ratio = round(len(behavioral) / total, 2) if total else 0.0
    coordinated_threshold = max(2, ceil(total * 0.4)) if total else 2

    classification = "none"
    confidence = "low"
    evidence: List[str] = []

    if declared:
        classification = "declared"
        confidence = "high"
        evidence.append(
            f"{len(declared)} test(s) reported a model ID change from the adapter"
        )
    elif fingerprint_changed and len(behavioral) >= coordinated_threshold:
        classification = "suspected"
        confidence = "high" if len(fingerprint_changed) >= 2 or drift_ratio >= 0.6 else "medium"
        evidence.append(
            f"{len(fingerprint_changed)} test(s) changed runtime fingerprint without a clean baseline match"
        )
    elif len(behavioral) >= coordinated_threshold:
        classification = "suspected"
        confidence = "medium"
        evidence.append(
            f"{len(behavioral)} test(s) drifted together in the same check run"
        )

    if classification != "none":
        if baseline_fingerprints and current_fingerprints and baseline_fingerprints != current_fingerprints:
            evidence.append(
                f"runtime fingerprint changed: {', '.join(baseline_fingerprints[:2])} -> {', '.join(current_fingerprints[:2])}"
            )
        if retry_persisted_count:
            evidence.append(
                f"{retry_persisted_count} retried test(s) still failed after retry"
            )
        if retry_recovered_count:
            evidence.append(
                f"{retry_recovered_count} retried test(s) recovered on retry"
            )

    return ModelRuntimeChangeSummary(
        classification=classification,
        confidence=confidence,
        affected_count=max(len(declared), len(fingerprint_changed), len(behavioral)),
        declared_count=len(declared),
        fingerprint_changed_count=len(fingerprint_changed),
        behavioral_drift_count=len(behavioral),
        retry_persisted_count=retry_persisted_count,
        retry_recovered_count=retry_recovered_count,
        drift_ratio=drift_ratio,
        baseline_fingerprints=baseline_fingerprints,
        current_fingerprints=current_fingerprints,
        evidence=evidence,
    )
