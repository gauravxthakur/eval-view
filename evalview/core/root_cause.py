"""Root cause attribution for regression analysis.

Takes a TraceDiff and produces a structured explanation of WHY a test
regressed — not just what changed.

The core analysis is fully deterministic (no LLM). An optional AI
enrichment layer (``enrich_with_ai``) adds deeper explanations for
low-confidence attributions using the project's existing LLM provider.
"""

import logging
from enum import Enum
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from evalview.core.diff import TraceDiff, ParameterDiff, DiffStatus

logger = logging.getLogger(__name__)


class RootCauseCategory(str, Enum):
    """Classification of why a regression occurred."""

    TOOL_ADDED = "tool_added"
    TOOL_REMOVED = "tool_removed"
    TOOL_REORDERED = "tool_reordered"
    PARAMETER_CHANGED = "parameter_changed"
    OUTPUT_DRIFTED = "output_drifted"
    SCORE_ONLY = "score_only"


class Confidence(str, Enum):
    """How clear the causal chain is."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RootCauseAnalysis(BaseModel):
    """Structured root cause analysis for a regression."""

    category: RootCauseCategory
    summary: str = Field(description="One-sentence human-readable explanation")
    root_tool: Optional[str] = Field(
        default=None,
        description="Which specific tool call triggered the cascade",
    )
    parameter_diffs: List[ParameterDiff] = Field(
        default_factory=list,
        description="Specific parameter changes that caused the regression",
    )
    confidence: Confidence
    suggested_fix: str = Field(description="Actionable guidance for the developer")
    ai_explanation: Optional[str] = Field(
        default=None,
        description="LLM-generated deep analysis (only for low-confidence attributions when --ai-root-cause is used)",
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def to_dict(self) -> dict:
        """Serialize for JSON output."""
        d: Dict[str, object] = {
            "category": self.category.value,
            "summary": self.summary,
            "root_tool": self.root_tool,
            "parameter_diffs": [
                {
                    "param": pd.param_name,
                    "golden": pd.golden_value,
                    "actual": pd.actual_value,
                    "type": pd.diff_type,
                    "similarity": pd.similarity,
                }
                for pd in self.parameter_diffs
            ],
            "confidence": self.confidence.value,
            "suggested_fix": self.suggested_fix,
        }
        if self.ai_explanation is not None:
            d["ai_explanation"] = self.ai_explanation
        return d


def analyze_root_cause(diff: TraceDiff) -> Optional[RootCauseAnalysis]:
    """Analyze a TraceDiff and determine the root cause of a regression.

    Uses a deterministic decision tree:
    1. Check if tools were added/removed/reordered
    2. If tools match, check parameter diffs
    3. If tools and params match, it's output drift
    4. If output is similar but score dropped, it's evaluator sensitivity

    Returns None if the diff is PASSED (no regression to analyze).
    """
    if diff.overall_severity == DiffStatus.PASSED:
        return None

    # Classify tool diffs by type
    added_tools = [td for td in diff.tool_diffs if td.type == "added"]
    removed_tools = [td for td in diff.tool_diffs if td.type == "removed"]
    changed_tools = [td for td in diff.tool_diffs if td.type == "changed"]
    reordered_tools = [td for td in diff.tool_diffs if td.type == "reordered"]

    # --- Decision tree ---

    # 1. Tool removed — a baseline tool was skipped
    if removed_tools:
        tool_names = [td.golden_tool for td in removed_tools if td.golden_tool]
        first_tool = tool_names[0] if tool_names else "unknown"
        if len(tool_names) == 1:
            summary = f"Tool '{first_tool}' was expected but not called"
        else:
            summary = f"Tools {', '.join(repr(t) for t in tool_names)} were expected but not called"

        return RootCauseAnalysis(
            category=RootCauseCategory.TOOL_REMOVED,
            summary=summary,
            root_tool=first_tool,
            confidence=Confidence.HIGH,
            suggested_fix=(
                f"Check if your agent's prompt or logic still triggers '{first_tool}'. "
                f"If the tool was intentionally removed, run `evalview snapshot` to update the baseline."
            ),
        )

    # 2. Tool added — a new tool was called that wasn't in the baseline
    if added_tools:
        tool_names = [td.actual_tool for td in added_tools if td.actual_tool]
        first_tool = tool_names[0] if tool_names else "unknown"
        if len(tool_names) == 1:
            summary = f"Tool '{first_tool}' was called but not expected in baseline"
        else:
            summary = f"Tools {', '.join(repr(t) for t in tool_names)} were called but not in baseline"

        return RootCauseAnalysis(
            category=RootCauseCategory.TOOL_ADDED,
            summary=summary,
            root_tool=first_tool,
            confidence=Confidence.HIGH,
            suggested_fix=(
                f"Unexpected call to '{first_tool}'. Check if a prompt change or model update "
                f"is causing extra tool calls. If the new behavior is correct, run `evalview snapshot` to update."
            ),
        )

    # 3. Tool reordered — same tools but different sequence
    #    "changed" diffs where golden_tool != actual_tool indicate reordering
    reordered = reordered_tools + [
        td for td in changed_tools
        if td.golden_tool and td.actual_tool and td.golden_tool != td.actual_tool
    ]
    if reordered:
        first = reordered[0]
        summary = (
            f"Tool sequence changed: '{first.golden_tool}' at step {first.position + 1} "
            f"was replaced by '{first.actual_tool}'"
        )
        return RootCauseAnalysis(
            category=RootCauseCategory.TOOL_REORDERED,
            summary=summary,
            root_tool=first.actual_tool or first.golden_tool,
            confidence=Confidence.MEDIUM,
            suggested_fix=(
                "The agent is calling the right tools but in a different order. "
                "If the new order is valid, run `evalview snapshot --variant <name>` to accept it as an alternative."
            ),
        )

    # 4. Parameter changed — same tool called but with different arguments
    tools_with_param_diffs = [td for td in changed_tools if td.parameter_diffs]
    if tools_with_param_diffs:
        first_td = tools_with_param_diffs[0]
        first_pd = first_td.parameter_diffs[0]
        tool_name = first_td.golden_tool or first_td.actual_tool or "unknown"

        summary = _format_param_change_summary(tool_name, first_pd)
        all_param_diffs = []
        for td in tools_with_param_diffs:
            all_param_diffs.extend(td.parameter_diffs)

        return RootCauseAnalysis(
            category=RootCauseCategory.PARAMETER_CHANGED,
            summary=summary,
            root_tool=tool_name,
            parameter_diffs=all_param_diffs,
            confidence=Confidence.HIGH,
            suggested_fix=_format_param_fix_suggestion(tool_name, first_pd),
        )

    # 5. Output drifted — same tools and params but output changed
    output_sim = diff.output_diff.similarity if diff.output_diff else 1.0
    if output_sim < 0.95:
        model_hint = ""
        if diff.model_changed:
            model_hint = (
                f" Model changed from '{diff.golden_model_id}' to '{diff.actual_model_id}', "
                f"which likely caused the output difference."
            )

        return RootCauseAnalysis(
            category=RootCauseCategory.OUTPUT_DRIFTED,
            summary=f"Same tools and parameters but output changed ({output_sim:.0%} similarity).{model_hint}",
            confidence=Confidence.MEDIUM if diff.model_changed else Confidence.LOW,
            suggested_fix=(
                "The agent used the same tools with the same parameters but produced different output. "
                "This is likely model drift (non-determinism or a model version update). "
                "If the new output is acceptable, run `evalview snapshot` to update the baseline."
            ),
        )

    # 6. Score only — tools and output are similar but score dropped
    return RootCauseAnalysis(
        category=RootCauseCategory.SCORE_ONLY,
        summary=f"Score dropped by {abs(diff.score_diff):.1f} points but tools and output are similar",
        confidence=Confidence.LOW,
        suggested_fix=(
            "The output looks similar but the evaluator scored it lower. "
            "This may be evaluator sensitivity or a subtle quality change. "
            "Check if the score threshold is too tight, or run `evalview snapshot` if the output is acceptable."
        ),
    )


def _format_param_change_summary(tool_name: str, pd: ParameterDiff) -> str:
    """Format a human-readable summary for a parameter change."""
    if pd.diff_type == "value_changed":
        return (
            f"'{tool_name}' called with {pd.param_name}={_truncate(pd.actual_value)} "
            f"instead of {pd.param_name}={_truncate(pd.golden_value)}"
        )
    elif pd.diff_type == "missing":
        return f"'{tool_name}' missing parameter '{pd.param_name}' (was {_truncate(pd.golden_value)})"
    elif pd.diff_type == "added":
        return f"'{tool_name}' has new parameter '{pd.param_name}={_truncate(pd.actual_value)}'"
    elif pd.diff_type == "type_changed":
        return (
            f"'{tool_name}' parameter '{pd.param_name}' changed type: "
            f"{type(pd.golden_value).__name__} → {type(pd.actual_value).__name__}"
        )
    return f"'{tool_name}' parameter '{pd.param_name}' changed"


def _format_param_fix_suggestion(tool_name: str, pd: ParameterDiff) -> str:
    """Format actionable fix suggestion for a parameter change."""
    if pd.diff_type == "value_changed":
        return (
            f"Check if your prompt still instructs the agent to use "
            f"{pd.param_name}={_truncate(pd.golden_value)} for '{tool_name}' calls."
        )
    elif pd.diff_type == "missing":
        return (
            f"The parameter '{pd.param_name}' is no longer being passed to '{tool_name}'. "
            f"Check if a prompt change or tool schema update removed it."
        )
    elif pd.diff_type == "added":
        return (
            f"A new parameter '{pd.param_name}' is being passed to '{tool_name}'. "
            f"If this is intentional, run `evalview snapshot` to update the baseline."
        )
    elif pd.diff_type == "type_changed":
        return (
            f"The parameter '{pd.param_name}' changed type for '{tool_name}'. "
            f"Check if a tool schema update changed the expected type."
        )
    return f"Check the parameters being passed to '{tool_name}'."


def _truncate(value: object, max_len: int = 50) -> str:
    """Truncate a value for display."""
    s = repr(value)
    if len(s) > max_len:
        return s[: max_len - 3] + "..."
    return s


# ---------------------------------------------------------------------------
# Optional AI enrichment
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = """\
You are a regression analysis expert for AI agent testing. You will be given
a deterministic root cause analysis and the raw diff data for a test that
regressed. Your job is to provide a deeper, more actionable explanation.

Be concise (2-4 sentences). Focus on:
1. WHY this change likely happened (model update, prompt drift, schema change, etc.)
2. The likely IMPACT on end-user experience
3. The most specific ACTION the developer should take

Respond with ONLY a valid JSON object:
{"explanation": "your 2-4 sentence analysis"}"""


def _build_ai_user_prompt(
    analysis: RootCauseAnalysis,
    diff: TraceDiff,
) -> str:
    """Build the user prompt for AI enrichment."""
    parts = [
        f"Test: {diff.test_name}",
        f"Status: {diff.overall_severity.value}",
        f"Deterministic root cause: {analysis.category.value} ({analysis.confidence.value} confidence)",
        f"Summary: {analysis.summary}",
        f"Score delta: {diff.score_diff:+.1f}",
    ]

    if diff.output_diff:
        parts.append(f"Output similarity: {diff.output_diff.similarity:.0%}")
        # Include a snippet of the output diff for context
        meaningful = [
            line for line in diff.output_diff.diff_lines
            if (line.startswith("+") or line.startswith("-"))
            and not line.startswith("+++") and not line.startswith("---")
        ]
        if meaningful:
            parts.append("Output diff (first 10 lines):")
            for line in meaningful[:10]:
                parts.append(f"  {line}")

    if diff.model_changed:
        parts.append(f"Model changed: {diff.golden_model_id} → {diff.actual_model_id}")

    if analysis.parameter_diffs:
        parts.append("Parameter changes:")
        for pd in analysis.parameter_diffs[:5]:
            parts.append(
                f"  {pd.param_name}: {_truncate(pd.golden_value, 30)} → {_truncate(pd.actual_value, 30)} ({pd.diff_type})"
            )

    if diff.tool_diffs:
        parts.append("Tool diffs:")
        for td in diff.tool_diffs[:5]:
            parts.append(f"  [{td.type}] pos={td.position} golden={td.golden_tool} actual={td.actual_tool}")

    if diff.output_diff:
        golden_preview = diff.output_diff.golden_preview[:300]
        actual_preview = diff.output_diff.actual_preview[:300]
        parts.append(f"\nBaseline output preview:\n{golden_preview}")
        parts.append(f"\nCurrent output preview:\n{actual_preview}")

    return "\n".join(parts)


async def enrich_with_ai(
    analysis: RootCauseAnalysis,
    diff: TraceDiff,
) -> RootCauseAnalysis:
    """Enrich a deterministic root cause analysis with LLM-generated insight.

    Only calls the LLM for low or medium confidence attributions where the
    deterministic analysis isn't definitive enough. High-confidence results
    are returned unchanged.

    Uses the project's existing LLM provider (same key as LLM-as-judge).
    Degrades gracefully — if the LLM call fails, returns the original analysis.

    Args:
        analysis: The deterministic root cause analysis.
        diff: The TraceDiff with full context.

    Returns:
        The same RootCauseAnalysis with ``ai_explanation`` populated
        (or unchanged if high confidence or LLM unavailable).
    """
    # Skip AI for high-confidence attributions — deterministic is sufficient
    if analysis.confidence == Confidence.HIGH:
        return analysis

    try:
        from evalview.core.llm_provider import LLMClient

        client = LLMClient()
        user_prompt = _build_ai_user_prompt(analysis, diff)

        result = await client.chat_completion(
            system_prompt=_AI_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.3,
            max_tokens=300,
        )

        explanation = result.get("explanation", "")
        if explanation:
            analysis.ai_explanation = explanation

    except (ValueError, ImportError) as e:
        # No LLM provider available — degrade gracefully
        logger.debug("AI root cause enrichment unavailable: %s", e)
    except Exception as e:
        # Any other error — log and continue with deterministic analysis
        logger.warning("AI root cause enrichment failed: %s", e)

    return analysis


async def enrich_diffs_with_ai(
    diffs: List[Tuple[str, TraceDiff]],
) -> Dict[str, Optional[RootCauseAnalysis]]:
    """Enrich all non-passed diffs with AI root cause analysis.

    Args:
        diffs: List of (test_name, TraceDiff) tuples.

    Returns:
        Dict mapping test_name to enriched RootCauseAnalysis (or None).
    """
    import asyncio

    results: Dict[str, Optional[RootCauseAnalysis]] = {}

    async def _enrich_one(name: str, diff: TraceDiff) -> None:
        analysis = analyze_root_cause(diff)
        if analysis is not None and analysis.confidence != Confidence.HIGH:
            analysis = await enrich_with_ai(analysis, diff)
        results[name] = analysis

    tasks = []
    for name, diff in diffs:
        if diff.overall_severity != DiffStatus.PASSED:
            tasks.append(_enrich_one(name, diff))

    if tasks:
        await asyncio.gather(*tasks)

    return results
