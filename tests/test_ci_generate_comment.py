"""Tests for generated-suite PR comments."""

from __future__ import annotations

import json

from click.testing import CliRunner


def _generate_report() -> dict:
    return {
        "report_version": 1,
        "source": "logs",
        "probes_run": 12,
        "tests_generated": 5,
        "discovery": {
            "count": 2,
            "tools": [
                {"name": "weather_api", "description": "Get the weather"},
                {"name": "calculator", "description": "Perform arithmetic"},
            ],
        },
        "behavior_signatures": {
            "tool_path:weather_api": 2,
            "tool_path:calculator": 1,
            "refusal": 1,
        },
        "covered": {
            "tool_paths": 2,
            "direct_answers": 1,
            "clarifications": 1,
            "multi_turn": 1,
            "refusals": 1,
            "error_paths": 0,
        },
        "draft_tests": [
            {
                "name": "Weather Test",
                "signature": "tool_path:weather_api",
                "rationale": "Observed weather tool path",
            },
            {
                "name": "Refusal Test",
                "signature": "refusal",
                "rationale": "Observed refusal path",
            },
        ],
        "gaps": [
            "No error-path behavior observed.",
            "Discovered but not exercised: calculator",
        ],
        "changes_since_last_generation": {
            "new_signatures": ["refusal"],
            "resolved_signatures": [],
            "new_tools": ["weather_api"],
            "resolved_gaps": ["No clarification path observed."],
            "new_gaps": ["No error-path behavior observed."],
            "tests_generated_delta": 1,
        },
    }


def test_generate_suite_pr_comment_contains_review_workflow():
    """Generated-suite comments should summarize coverage and next steps."""
    from evalview.ci.comment import generate_suite_pr_comment

    comment = generate_suite_pr_comment(_generate_report(), "https://example.com/run/123")

    assert "EvalView Generate" in comment
    assert "Draft Test(s)" in comment
    assert "Discovered Tools" in comment
    assert "Changes Since Last Generation" in comment
    assert "Coverage Gaps" in comment
    assert "snapshot --approve-generated" in comment
    assert "weather_api" in comment


def test_ci_comment_detects_generate_report_format(tmp_path):
    """ci comment should render generated-suite reports via the new formatter."""
    from evalview.commands.ci_cmd import ci_comment

    report_path = tmp_path / "generated.report.json"
    report_path.write_text(json.dumps(_generate_report()), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(ci_comment, ["--results", str(report_path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "EvalView Generate" in result.output
    assert "Coverage Gaps" in result.output
