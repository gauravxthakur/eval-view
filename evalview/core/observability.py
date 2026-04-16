"""Shared observability signal extraction for EvalView.

Provides a single source of truth for extracting and summarizing behavioral
anomalies, trust scores, and coherence issues from evaluation results.

Every surface that displays or transmits observability data should call
``extract_observability_summary()`` rather than re-implementing the
extraction logic.

The ``AnomalyReportDict``, ``TrustReportDict``, and ``CoherenceReportDict``
TypedDicts define the canonical schema for observability reports.  All
``to_dict()`` methods on the report dataclasses conform to these schemas,
and all consumers should type-hint against them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from typing import TypedDict


# ── Report schemas (TypedDict) ───────────────────────────────────────────
# These define the dict shape produced by to_dict() on each report class.
# Consumers (templates, CI comments, cloud push) should type-hint against
# these rather than using Dict[str, Any].


class AnomalyEntryDict(TypedDict):
    """Schema for a single anomaly entry in the anomaly report."""
    pattern: str
    severity: str
    description: str
    step_indices: List[int]
    tool_name: Optional[str]
    evidence: Dict[str, Any]


class AnomalyReportDict(TypedDict):
    """Schema for the anomaly_report field on EvaluationResult."""
    anomalies: List[AnomalyEntryDict]
    total_steps: int
    unique_tools: int
    error_count: int
    summary: str


class TrustFlagDict(TypedDict):
    """Schema for a single gaming flag in the trust report."""
    check: str
    severity: str
    description: str
    evidence: Dict[str, Any]


class TrustReportDict(TypedDict):
    """Schema for the trust_report field on EvaluationResult."""
    flags: List[TrustFlagDict]
    trust_score: float
    summary: str


class CoherenceIssueDict(TypedDict):
    """Schema for a single coherence issue in the coherence report."""
    category: str
    severity: str
    turn_index: int
    reference_turn: Optional[int]
    description: str
    evidence: Dict[str, Any]


class CoherenceReportDict(TypedDict):
    """Schema for the coherence_report field on EvaluationResult."""
    issues: List[CoherenceIssueDict]
    total_turns: int
    coherence_score: float
    summary: str

# ── Thresholds (single source of truth) ──────────────────────────────────

#: Trust score below this threshold is considered "low trust"
LOW_TRUST_THRESHOLD: float = 0.8


# ── Summary dataclass ──────────��─────────────────────────────────────────


@dataclass
class ObservabilitySummary:
    """Aggregated observability signals across a set of evaluation results."""

    anomaly_count: int = 0
    anomaly_tests: List[str] = field(default_factory=list)

    low_trust_count: int = 0
    low_trust_tests: List[str] = field(default_factory=list)

    coherence_issue_count: int = 0
    coherence_tests: List[str] = field(default_factory=list)

    @property
    def has_signals(self) -> bool:
        return (
            self.anomaly_count > 0
            or self.low_trust_count > 0
            or self.coherence_issue_count > 0
        )

    def to_verdict_payload(self) -> Dict[str, Any]:
        """Return the dict shape used for verdict enrichment and CI comments."""
        payload: Dict[str, Any] = {}
        if self.anomaly_count:
            payload["behavioral_anomalies"] = {
                "count": self.anomaly_count,
                "tests": self.anomaly_tests[:10],
            }
        if self.low_trust_count:
            payload["low_trust_tests"] = {
                "count": self.low_trust_count,
                "tests": self.low_trust_tests[:10],
            }
        if self.coherence_issue_count:
            payload["coherence_issues"] = {
                "count": self.coherence_issue_count,
                "tests": self.coherence_tests[:10],
            }
        return payload


# ── Extraction helper ���───────────────────────────���───────────────────────


def extract_observability_summary(
    results: Optional[List[Any]],
) -> ObservabilitySummary:
    """Extract observability signals from a list of EvaluationResult objects.

    Safe to call with None, empty list, or results that lack the new fields.
    """
    summary = ObservabilitySummary()
    if not results:
        return summary

    for r in results:
        test_name = getattr(r, "test_case", "?")
        try:
            ar = getattr(r, "anomaly_report", None)
            if isinstance(ar, dict) and ar.get("anomalies"):
                summary.anomaly_count += 1
                summary.anomaly_tests.append(test_name)

            tr = getattr(r, "trust_report", None)
            if isinstance(tr, dict):
                trust_score = float(tr.get("trust_score", 1.0))
                if trust_score < LOW_TRUST_THRESHOLD:
                    summary.low_trust_count += 1
                    summary.low_trust_tests.append(test_name)

            cr = getattr(r, "coherence_report", None)
            if isinstance(cr, dict) and cr.get("issues"):
                summary.coherence_issue_count += 1
                summary.coherence_tests.append(test_name)
        except (TypeError, ValueError, AttributeError):
            continue

    return summary
