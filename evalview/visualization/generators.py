"""EvalView visual report generator.

Produces a single self-contained HTML file from EvaluationResult objects and
TraceDiff data.  No external files — Mermaid.js and Chart.js are loaded from
CDN.  The generated file is suitable for:
    • Auto-open in browser after ``evalview check``
    • Attaching to Slack / PRs
    • Returning as a path from the MCP ``generate_visual_report`` tool
    • Sharing with ``--share`` (future)

Usage::
    from evalview.visualization import generate_visual_report
    path = generate_visual_report(results, diffs, output_path="report.html")
"""
from __future__ import annotations

import json
import os
import webbrowser
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from evalview.core.types import EvaluationResult
    from evalview.core.diff import TraceDiff


# ── Mermaid helpers ────────────────────────────────────────────────────────────

def _mermaid_from_steps(steps: List[Any], query: str = "", output: str = "") -> str:
    """Core Mermaid sequence diagram builder from a steps list."""
    if not steps:
        return "sequenceDiagram\n    Note over Agent: Direct response — no tools used"

    lines = ["sequenceDiagram"]
    lines.append("    participant User")
    lines.append("    participant Agent")

    seen_tools: Dict[str, str] = {}
    for step in steps:
        tool: str = str(getattr(step, "tool_name", None) or getattr(step, "step_name", None) or "unknown")
        if tool not in seen_tools:
            alias = f"T{len(seen_tools)}"
            seen_tools[tool] = alias
            short = (tool[:31] + "…") if len(tool) > 32 else tool
            lines.append(f"    participant {alias} as {short}")

    short_query = _safe_mermaid((query[:40] + "…") if len(query) > 40 else query) if query else "..."
    lines.append(f"    User->>Agent: {short_query}")

    current_turn = None

    for step in steps:
        step_turn = getattr(step, "turn_index", None)

        # Add a turn separator when the turn index changes
        if step_turn is not None and step_turn != current_turn:
            step_query = getattr(step, "turn_query", "") or ""
            safe_query = _safe_mermaid((step_query[:57] + "...") if len(step_query) > 60 else step_query)
            if safe_query:
                lines.append(f"    Note over User,Agent: Turn {step_turn} - {safe_query}")
            else:
                lines.append(f"    Note over User,Agent: Turn {step_turn}")
            current_turn = step_turn

        tool = str(getattr(step, "tool_name", None) or getattr(step, "step_name", None) or "unknown")
        alias = seen_tools.get(tool, tool)
        params = getattr(step, "parameters", {}) or {}
        param_str = ", ".join(f"{k}={str(v)[:20]}" for k, v in list(params.items())[:2])
        if len(params) > 2:
            param_str += "…"
        success = getattr(step, "success", True)
        arrow = "->>" if success else "-x"
        lines.append(f"    Agent{arrow}{alias}: {_safe_mermaid(param_str or tool)}")
        out = getattr(step, "output", None)
        out_str = str(out)[:30] if out is not None else "ok"
        lines.append(f"    {alias}-->Agent: {_safe_mermaid(out_str)}")

    short_out = _safe_mermaid((output[:40] + "…") if len(output) > 40 else output) if output else "..."
    lines.append(f"    Agent-->>User: {short_out}")

    return "\n".join(lines)


def _mermaid_trace(result: "EvaluationResult") -> str:
    """Convert an EvaluationResult into a Mermaid sequence diagram."""
    steps = []
    try:
        steps = result.trace.steps or []
    except AttributeError:
        pass
    query: str = str(getattr(result, "input_query", "") or "")
    output: str = str(getattr(result, "actual_output", "") or "")
    return _mermaid_from_steps(steps, query, output)


def _strip_markdown(text: str) -> str:
    """Remove common markdown symbols for clean display in HTML."""
    import re
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text, flags=re.DOTALL)  # bold/italic
    text = re.sub(r'`(.+?)`', r'\1', text, flags=re.DOTALL)               # inline code
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)            # headings
    return text


def _safe_mermaid(s: str) -> str:
    """Strip everything except safe alphanumeric + basic punctuation for Mermaid labels."""
    import re
    s = s.replace("\n", " ").replace("\r", "")
    s = re.sub(r'[^\w\s\.\-_/=:,]', '', s)
    s = s[:28].strip()
    return (s + '...') if len(s) == 28 else s or '...'


# ── KPI helpers ────────────────────────────────────────────────────────────────

def _kpis(results: List["EvaluationResult"]) -> Dict[str, Any]:
    if not results:
        return {}
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    scores = [r.score for r in results]
    costs = []
    latencies = []
    total_input_tokens = 0
    total_output_tokens = 0
    for r in results:
        try:
            costs.append(r.trace.metrics.total_cost or 0)
            latencies.append(r.trace.metrics.total_latency or 0)
            if r.trace.metrics.total_tokens:
                total_input_tokens += r.trace.metrics.total_tokens.input_tokens
                total_output_tokens += r.trace.metrics.total_tokens.output_tokens
        except AttributeError:
            pass
    models = _collect_models(results)
    total_tokens = total_input_tokens + total_output_tokens
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total * 100, 1),
        "avg_score": round(sum(scores) / len(scores), 1),
        "total_cost": round(sum(costs), 6),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 0) if latencies else 0,
        "scores": scores,
        "test_names": [r.test_case for r in results],
        "models": models,
        "models_display": ", ".join(models) if models else "Unknown",
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_tokens,
    }


def _clean_model_name(model_id: str, provider: Optional[str] = None) -> str:
    """Format a model name for display — human-readable, no internal prefixes."""
    # Skip transport-layer "providers" that aren't real LLM providers
    non_providers = {"http", "mcp", "unknown", "none", ""}
    if provider and provider.lower() not in non_providers:
        return f"{provider}/{model_id}"
    return model_id


def _extract_models(result: "EvaluationResult") -> List[str]:
    """Extract best-effort model labels from a result (deduplicated by model ID)."""
    seen_ids: set[str] = set()
    labels: list[str] = []
    trace = result.trace
    model_id = getattr(trace, "model_id", None)
    model_provider = getattr(trace, "model_provider", None)
    if model_id:
        seen_ids.add(model_id)
        labels.append(_clean_model_name(model_id, model_provider))

    # Only add span models if the trace didn't already report a model_id.
    # When model_id is set (from the agent response), span models are
    # typically just the config echo from the HTTP adapter — showing both
    # creates confusing duplicates like "anthropic/claude-sonnet-4-5, claude-sonnet-4-6".
    trace_context = getattr(trace, "trace_context", None)
    if trace_context and not model_id:
        for span in trace_context.spans:
            if span.llm and span.llm.model and span.llm.model not in seen_ids:
                seen_ids.add(span.llm.model)
                provider = span.llm.provider or model_provider
                labels.append(_clean_model_name(span.llm.model, provider))

    return labels


def _collect_models(results: List["EvaluationResult"]) -> List[str]:
    """Collect model labels across a run, ordered by frequency."""
    counts: Counter[str] = Counter()
    for result in results:
        for label in _extract_models(result):
            counts[label] += 1
    return [label for label, _ in counts.most_common()]


def _baseline_meta(golden_traces: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize baseline creation metadata."""
    if not golden_traces:
        return {
            "latest_created_display": "Unknown",
            "models_display": "Unknown",
        }

    blessed_times: list[datetime] = []
    model_counts: Counter[str] = Counter()
    for golden in golden_traces.values():
        metadata = getattr(golden, "metadata", None)
        if not metadata:
            continue
        blessed_at = getattr(metadata, "blessed_at", None)
        if isinstance(blessed_at, datetime):
            blessed_times.append(blessed_at)
        model_id = getattr(metadata, "model_id", None)
        model_provider = getattr(metadata, "model_provider", None)
        if model_id:
            model_counts[f"{model_provider}/{model_id}" if model_provider else str(model_id)] += 1

    latest_created = max(blessed_times).strftime("%Y-%m-%d %H:%M") if blessed_times else "Unknown"
    models = [label for label, _ in model_counts.most_common()]
    return {
        "latest_created_display": latest_created,
        "models_display": ", ".join(models) if models else "Not recorded in snapshot",
    }


# ── Diff helpers ───────────────────────────────────────────────────────────────

def _diff_rows(
    diffs: List["TraceDiff"],
    golden_traces: Optional[Dict[str, Any]] = None,
    actual_results: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    rows = []
    for d in diffs:
        status = str(getattr(d, "overall_severity", "passed")).lower().replace("diffstatus.", "")
        output_diff = getattr(d, "output_diff", None)
        similarity = round(getattr(output_diff, "similarity", 1.0) * 100, 1) if output_diff else 100.0
        semantic_similarity = None
        if output_diff and getattr(output_diff, "semantic_similarity", None) is not None:
            semantic_similarity = round(output_diff.semantic_similarity * 100, 1)
        golden_out = getattr(output_diff, "golden_preview", "") if output_diff else ""
        actual_out = getattr(output_diff, "actual_preview", "") if output_diff else ""
        diff_lines = getattr(output_diff, "diff_lines", []) if output_diff else []
        score_delta = getattr(d, "score_diff", 0.0) or 0.0
        test_name = getattr(d, "test_name", "")

        # Extract tool sequences from golden trace and tool_diffs
        golden_tools: List[str] = []
        actual_tools: List[str] = []
        if golden_traces and test_name in golden_traces:
            gt = golden_traces[test_name]
            golden_tools = getattr(gt, "tool_sequence", []) or []
        # Reconstruct actual tools from golden + diffs
        tool_diffs = getattr(d, "tool_diffs", []) or []
        if actual_results and test_name in actual_results:
            try:
                result = actual_results[test_name]
                actual_tools = [
                    str(getattr(s, "tool_name", None) or getattr(s, "step_name", "?"))
                    for s in (result.trace.steps or [])
                ]
            except AttributeError:
                pass

        # Extract parameter diffs for the HTML template
        param_diffs = []
        for td in tool_diffs:
            for pd in getattr(td, "parameter_diffs", []):
                sim = None
                if pd.similarity is not None:
                    sim = round(pd.similarity * 100, 1)
                param_diffs.append({
                    "step": td.position + 1,
                    "tool": td.golden_tool or td.actual_tool or "?",
                    "param": pd.param_name,
                    "golden": str(pd.golden_value)[:60] if pd.golden_value is not None else "",
                    "actual": str(pd.actual_value)[:60] if pd.actual_value is not None else "",
                    "type": pd.diff_type,
                    "similarity": sim,
                })

        # Generate side-by-side trajectory diagrams when trace data is available
        golden_diagram = ""
        actual_diagram = ""
        if golden_traces and test_name in golden_traces:
            gt = golden_traces[test_name]
            try:
                gt_steps = gt.trace.steps or []
            except AttributeError:
                gt_steps = []
            golden_diagram = _mermaid_from_steps(gt_steps)
        if actual_results and test_name in actual_results:
            actual_diagram = _mermaid_trace(actual_results[test_name])

        rows.append({
            "name": test_name,
            "status": status,
            "score_delta": round(score_delta, 1),
            "similarity": similarity,
            "semantic_similarity": semantic_similarity,
            "golden_tools": golden_tools,
            "actual_tools": actual_tools,
            "golden_out": golden_out[:600],
            "actual_out": actual_out[:600],
            "diff_lines": diff_lines[:50],
            "param_diffs": param_diffs,
            "golden_diagram": golden_diagram,
            "actual_diagram": actual_diagram,
        })
    return rows


# ── Timeline helpers ───────────────────────────────────────────────────────────

def _timeline_data(results: List["EvaluationResult"]) -> List[Dict[str, Any]]:
    rows = []
    for r in results:
        try:
            steps = r.trace.steps or []
            fallback_latency = 0.0
            fallback_cost = 0.0
            if steps:
                total_latency = float(getattr(r.trace.metrics, "total_latency", 0) or 0)
                total_cost = float(getattr(r.trace.metrics, "total_cost", 0) or 0)
                if not any((getattr(getattr(step, "metrics", None), "latency", 0) or 0) > 0 for step in steps):
                    fallback_latency = total_latency / len(steps) if total_latency > 0 else 0.0
                if not any((getattr(getattr(step, "metrics", None), "cost", 0) or 0) > 0 for step in steps):
                    fallback_cost = total_cost / len(steps) if total_cost > 0 else 0.0
            for step in steps:
                lat = getattr(step.metrics, "latency", 0) if hasattr(step, "metrics") else 0
                cost = getattr(step.metrics, "cost", 0) if hasattr(step, "metrics") else 0
                if (not lat or lat <= 0) and fallback_latency:
                    lat = fallback_latency
                if (not cost or cost <= 0) and fallback_cost:
                    cost = fallback_cost
                tool = getattr(step, "tool_name", "unknown")[:20]
                test = r.test_case[:15]
                rows.append({
                    "test": test,
                    "tool": tool,
                    "label": f"{test} \u203a {tool}",
                    "latency": round(lat, 1),
                    "cost": round(cost, 6),
                    "success": getattr(step, "success", True),
                })
        except AttributeError:
            pass
    return rows


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_visual_report(
    results: List["EvaluationResult"],
    diffs: Optional[List["TraceDiff"]] = None,
    output_path: Optional[str] = None,
    auto_open: bool = True,
    title: str = "EvalView Report",
    notes: Optional[str] = None,
    compare_results: Optional[List[List["EvaluationResult"]]] = None,
    compare_labels: Optional[List[str]] = None,
    golden_traces: Optional[Dict[str, Any]] = None,
    judge_usage: Optional[Dict[str, Any]] = None,
    default_tab: Optional[str] = None,
) -> str:
    """Generate a self-contained visual HTML report.

    Args:
        results: List of EvaluationResult objects.
        diffs: Optional list of TraceDiff objects for diff tab.
        output_path: Where to write the HTML (default: .evalview/reports/<timestamp>.html).
        auto_open: If True, open the report in the default browser.
        title: Report title shown in the header.
        notes: Optional free-text note shown in the header.
        golden_traces: Optional dict mapping test name to GoldenTrace. When provided,
            the Diffs tab renders side-by-side baseline vs. current Mermaid diagrams.

    Returns:
        Absolute path to the generated HTML file.
    """
    if output_path is None:
        os.makedirs(".evalview/reports", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f".evalview/reports/{ts}.html"

    kpis = _kpis(results)
    baseline = _baseline_meta(golden_traces)
    traces = []
    for r in results:
        try:
            cost = r.trace.metrics.total_cost or 0.0
            latency = r.trace.metrics.total_latency or 0.0
            tokens = None
            input_tokens = 0
            output_tokens = 0
            if r.trace.metrics.total_tokens:
                input_tokens = r.trace.metrics.total_tokens.input_tokens
                output_tokens = r.trace.metrics.total_tokens.output_tokens
                tokens = input_tokens + output_tokens
        except AttributeError:
            cost, latency, tokens = 0.0, 0.0, None
            input_tokens, output_tokens = 0, 0
        has_steps = bool(getattr(r.trace, "steps", None))
        models = _extract_models(r)
        baseline_created = ""
        baseline_model = "Unknown"
        if golden_traces and r.test_case in golden_traces:
            metadata = getattr(golden_traces[r.test_case], "metadata", None)
            if metadata:
                blessed_at = getattr(metadata, "blessed_at", None)
                if isinstance(blessed_at, datetime):
                    baseline_created = blessed_at.strftime("%Y-%m-%d %H:%M")
                model_id = getattr(metadata, "model_id", None)
                model_provider = getattr(metadata, "model_provider", None)
                if model_id:
                    baseline_model = f"{model_provider}/{model_id}" if model_provider else str(model_id)
                else:
                    trace_model_id = getattr(getattr(golden_traces[r.test_case], "trace", None), "model_id", None)
                    trace_model_provider = getattr(getattr(golden_traces[r.test_case], "trace", None), "model_provider", None)
                    if trace_model_id:
                        baseline_model = f"{trace_model_provider}/{trace_model_id}" if trace_model_provider else str(trace_model_id)
                    else:
                        baseline_model = "Not recorded in snapshot"

        # Extract turn and tool info for the trace list view
        turn_list = []
        if getattr(r.trace, "turns", None):
            for turn in getattr(r.trace, "turns", []) or []:
                turn_entry = {
                    "index": int(getattr(turn, "index", 0) or 0),
                    "query": str(getattr(turn, "query", "") or ""),
                    "output": _strip_markdown(str(getattr(turn, "output", "") or "")),
                    "tools": [str(tool) for tool in (getattr(turn, "tools", None) or [])],
                    "latency_ms": float(getattr(turn, "latency_ms", 0) or 0),
                    "cost": float(getattr(turn, "cost", 0) or 0),
                }
                # Attach per-turn evaluation if present
                eval_obj = getattr(turn, "evaluation", None)
                if eval_obj is not None:
                    turn_entry["evaluation"] = {
                        "passed": eval_obj.passed,
                        "tool_accuracy": eval_obj.tool_accuracy,
                        "forbidden_violations": eval_obj.forbidden_violations,
                        "contains_passed": eval_obj.contains_passed,
                        "contains_failed": eval_obj.contains_failed,
                        "not_contains_passed": eval_obj.not_contains_passed,
                        "not_contains_failed": eval_obj.not_contains_failed,
                    }
                turn_list.append(turn_entry)
        elif has_steps:
            current_t_idx = None
            current_turn_data = None
            turn_fallback_latency = 0.0
            turn_fallback_cost = 0.0
            if not any(getattr(step, "turn_index", None) is not None for step in r.trace.steps):
                turn_fallback_latency = float(getattr(r.trace.metrics, "total_latency", 0) or 0)
                turn_fallback_cost = float(getattr(r.trace.metrics, "total_cost", 0) or 0)
            for step in r.trace.steps:
                t_idx = getattr(step, "turn_index", None)
                if t_idx is not None:
                    if t_idx != current_t_idx:
                        current_t_idx = t_idx
                        current_turn_data = {
                            "index": t_idx,
                            "query": getattr(step, "turn_query", ""),
                            "output": "",
                            "tools": [],
                            "latency_ms": 0.0,
                            "cost": 0.0,
                        }
                        turn_list.append(current_turn_data)

                    if current_turn_data is not None:
                        tool_name = str(getattr(step, "tool_name", None) or getattr(step, "step_name", None) or "unknown")
                        current_turn_data["tools"].append(tool_name)
                        step_latency = float(getattr(getattr(step, "metrics", None), "latency", 0) or 0)
                        step_cost = float(getattr(getattr(step, "metrics", None), "cost", 0) or 0)
                        current_turn_data["latency_ms"] += step_latency
                        current_turn_data["cost"] += step_cost

            if not turn_list and has_steps:
                turn_list.append({
                    "index": 1,
                    "query": getattr(r, "input_query", "") or "",
                    "output": _strip_markdown(getattr(r, "actual_output", "") or ""),
                    "tools": [
                        str(getattr(step, "tool_name", None) or getattr(step, "step_name", None) or "unknown")
                        for step in r.trace.steps
                    ],
                    "latency_ms": turn_fallback_latency,
                    "cost": turn_fallback_cost,
                })

        traces.append({
            "name": r.test_case,
            "diagram": _mermaid_trace(r) if has_steps else "",
            "has_steps": has_steps,
            "passed": r.passed,
            "cost": f"${cost:.6f}".rstrip('0').rstrip('.') if cost else "$0",
            "latency": f"{int(latency)}ms",
            "tokens": f"{tokens:,} tokens" if tokens else "",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "score": round(r.score, 1),
            "model": ", ".join(models) if models else "Unknown",
            "baseline_created": baseline_created or "Unknown",
            "baseline_model": baseline_model,
            "query": getattr(r, "input_query", "") or "",
            "output": _strip_markdown(getattr(r, "actual_output", "") or ""),
            "turns": turn_list,
        })
    actual_results_dict = {r.test_case: r for r in results}
    diff_rows = _diff_rows(diffs or [], golden_traces, actual_results_dict)
    timeline = _timeline_data(results)

    # Build comparison data if multiple runs provided
    compare_data = None
    if compare_results:
        labels = compare_labels or []
        all_runs = [results] + list(compare_results)
        all_labels = labels if labels else [f"Run {i+1}" for i in range(len(all_runs))]
        compare_data = {
            "labels": all_labels,
            "runs": [_kpis(r) for r in all_runs],
        }

    html = _render_template(
        title=title,
        notes=notes or "",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        kpis=kpis,
        baseline=baseline,
        judge_usage=judge_usage or {},
        traces=traces,
        diff_rows=diff_rows,
        timeline=timeline,
        compare=compare_data,
        default_tab=default_tab or "overview",
    )

    abs_path = os.path.abspath(output_path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(html)

    if auto_open:
        webbrowser.open(f"file://{abs_path}")

    return abs_path


# ── Template ───────────────────────────────────────────────────────────────────

def _render_template(**ctx: Any) -> str:
    """Render the report HTML using Jinja2."""
    try:
        from jinja2 import BaseLoader, Environment
    except ImportError:
        return f"<html><body><pre>{json.dumps(ctx, default=str, indent=2)}</pre></body></html>"

    env = Environment(loader=BaseLoader(), autoescape=True)

    # Mark pre-sanitized Mermaid diagrams as safe so Jinja2 autoescape
    # doesn't HTML-encode arrows (-->, ->>) which breaks rendering.
    # User content in labels is already sanitized by _safe_mermaid().
    from markupsafe import Markup
    for t in ctx.get("traces", []):
        if t.get("diagram"):
            t["diagram"] = Markup(t["diagram"])
    for d in ctx.get("diff_rows", []):
        if d.get("golden_diagram"):
            d["golden_diagram"] = Markup(d["golden_diagram"])
        if d.get("actual_diagram"):
            d["actual_diagram"] = Markup(d["actual_diagram"])

    return env.from_string(_TEMPLATE).render(**ctx)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --green:#34d399;--green-dim:#065f46;--red:#fb7185;--red-dim:#9f1239;
  --yellow:#fbbf24;--yellow-dim:#92400e;--blue:#818cf8;--purple:#c084fc;--cyan:#22d3ee;
  --surface-0:#0a0e1a;--surface-1:rgba(255,255,255,.03);--surface-2:rgba(255,255,255,.055);
  --surface-3:rgba(255,255,255,.08);--surface-raised:rgba(255,255,255,.04);
  --border:rgba(255,255,255,.07);--border-subtle:rgba(255,255,255,.05);
  --border-hover:rgba(255,255,255,.14);
  --text:#f1f5f9;--text-secondary:#94a3b8;--text-tertiary:#64748b;
  --radius:16px;--radius-sm:10px;--radius-xs:6px;
  --font:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  --mono:'JetBrains Mono','SF Mono','Fira Code',monospace;
  --shadow-sm:0 1px 2px rgba(0,0,0,.3),0 1px 3px rgba(0,0,0,.15);
  --shadow-md:0 4px 16px rgba(0,0,0,.25),0 2px 4px rgba(0,0,0,.15);
  --shadow-lg:0 8px 32px rgba(0,0,0,.35),0 4px 8px rgba(0,0,0,.2);
  --shadow-glow-green:0 0 20px rgba(52,211,153,.15),0 0 60px rgba(52,211,153,.05);
  --shadow-glow-red:0 0 20px rgba(251,113,133,.15),0 0 60px rgba(251,113,133,.05);
  --shadow-glow-blue:0 0 20px rgba(129,140,248,.15),0 0 60px rgba(129,140,248,.05);
  --transition:all .2s cubic-bezier(.4,0,.2,1);
}
html{scroll-behavior:smooth;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
body{
  font-family:var(--font);font-size:14px;line-height:1.6;
  color:var(--text);min-height:100vh;overflow-x:hidden;
  background:var(--surface-0);
}
/* Subtle mesh gradient background */
body::before{
  content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 80% 50% at 10% 0%,rgba(129,140,248,.1),transparent 50%),
    radial-gradient(ellipse 60% 40% at 90% 100%,rgba(52,211,153,.07),transparent 50%),
    radial-gradient(ellipse 40% 30% at 50% 40%,rgba(192,132,252,.04),transparent 50%);
}

/* ── Header ── */
.header{
  position:sticky;top:0;z-index:200;
  background:rgba(10,14,26,.85);
  border-bottom:1px solid var(--border);
  backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%);
  padding:0 32px;height:56px;
  display:flex;align-items:center;justify-content:space-between;
}
.logo{display:flex;align-items:center;gap:10px}
.logo-icon{
  width:30px;height:30px;border-radius:8px;flex-shrink:0;
  background:linear-gradient(135deg,var(--blue),var(--purple));
  display:flex;align-items:center;justify-content:center;font-size:14px;
  box-shadow:0 0 0 1px rgba(129,140,248,.3),0 2px 12px rgba(129,140,248,.2);
}
.logo-text{font-size:14px;font-weight:700;letter-spacing:-.02em;color:var(--text)}
.logo-sub{font-size:11px;color:var(--text-tertiary);font-weight:400;letter-spacing:-.01em}
.header-right{display:flex;align-items:center;gap:6px}

/* ── Badges ── */
.badge{
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;
  letter-spacing:-.01em;white-space:nowrap;
}
.b-green{background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2)}
.b-red{background:rgba(251,113,133,.1);color:var(--red);border:1px solid rgba(251,113,133,.2)}
.b-yellow{background:rgba(251,191,36,.1);color:var(--yellow);border:1px solid rgba(251,191,36,.2)}
.b-blue{background:rgba(129,140,248,.1);color:var(--blue);border:1px solid rgba(129,140,248,.2)}
.b-purple{background:rgba(192,132,252,.1);color:var(--purple);border:1px solid rgba(192,132,252,.2)}

/* ── Layout ── */
.main{max-width:1280px;margin:0 auto;padding:28px 32px 80px;position:relative;z-index:1}

/* ── Tab bar ── */
.tabbar{
  display:flex;gap:1px;
  background:var(--surface-1);border:1px solid var(--border);
  border-radius:var(--radius-sm);padding:3px;margin-bottom:28px;width:fit-content;
}
.tab{
  background:none;border:none;color:var(--text-tertiary);cursor:pointer;
  font:500 13px/1 var(--font);padding:8px 18px;border-radius:7px;
  transition:var(--transition);letter-spacing:-.01em;
}
.tab:hover{color:var(--text-secondary);background:var(--surface-2)}
.tab.on{
  color:var(--text);background:var(--surface-3);
  box-shadow:var(--shadow-sm);
}
.panel{display:none}.panel.on{display:block}

/* ── KPI Cards with progress rings ── */
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
@media(max-width:1100px){.kpi-row{grid-template-columns:repeat(2,1fr)}}
.kpi{
  background:var(--surface-raised);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;
  position:relative;overflow:hidden;
  transition:var(--transition);cursor:default;
}
.kpi:hover{transform:translateY(-2px);border-color:var(--border-hover);box-shadow:var(--shadow-md)}
.kpi.kpi-pass:hover{box-shadow:var(--shadow-glow-green);border-color:rgba(52,211,153,.25)}
.kpi.kpi-fail:hover{box-shadow:var(--shadow-glow-red);border-color:rgba(251,113,133,.25)}
.kpi.kpi-blue:hover{box-shadow:var(--shadow-glow-blue);border-color:rgba(129,140,248,.25)}
.kpi-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px}
.kpi-label{font-size:11px;font-weight:600;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.06em}
.kpi-ring{position:relative;width:44px;height:44px;flex-shrink:0}
.kpi-ring svg{transform:rotate(-90deg)}
.kpi-ring-label{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:var(--text-secondary)}
.kpi-num{font-size:32px;font-weight:800;letter-spacing:-.04em;line-height:1}
.kpi-num.c-green{color:var(--green)}
.kpi-num.c-red{color:var(--red)}
.kpi-num.c-yellow{color:var(--yellow)}
.kpi-num.c-blue{color:var(--blue)}
.kpi-sub{font-size:12px;color:var(--text-tertiary);margin-top:4px;letter-spacing:-.01em}

/* ── Meta cards ── */
.meta-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
@media(max-width:900px){.meta-row{grid-template-columns:1fr}}
.meta-card{
  background:var(--surface-raised);border:1px solid var(--border);
  border-radius:var(--radius);padding:16px 20px;
  transition:var(--transition);
}
.meta-card:hover{border-color:var(--border-hover)}
.meta-label{font-size:10px;font-weight:700;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
.meta-value{font-size:15px;font-weight:700;color:var(--text);letter-spacing:-.01em}
.meta-sub{font-size:12px;color:var(--text-tertiary);margin-top:3px}

/* ── Cards ── */
.card{
  background:var(--surface-raised);border:1px solid var(--border);
  border-radius:var(--radius);padding:20px;
  position:relative;overflow:hidden;
  transition:var(--transition);
}
.card:hover{border-color:var(--border-hover)}
.card-title{
  font-size:11px;font-weight:700;color:var(--text-tertiary);
  text-transform:uppercase;letter-spacing:.06em;
  margin-bottom:16px;display:flex;align-items:center;gap:8px;
}
.card-title::before{content:'';width:3px;height:12px;border-radius:2px;background:linear-gradient(to bottom,var(--blue),var(--purple))}

/* ── Charts ── */
.chart-row{display:grid;grid-template-columns:1fr 220px;gap:14px;margin-bottom:18px}
@media(max-width:900px){.chart-row{grid-template-columns:1fr}}
.chart-wrap{position:relative;height:200px}

/* ── Trace cards ── */
.item{
  background:var(--surface-raised);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:10px;overflow:hidden;
  transition:var(--transition);
}
.item:hover{border-color:var(--border-hover)}
.item-head{
  padding:14px 20px;display:flex;align-items:center;gap:10px;
  cursor:pointer;transition:background .15s;
}
.item-head:hover{background:var(--surface-2)}
.item-name{font-weight:600;font-size:14px;flex:1;letter-spacing:-.02em}
.item-meta{display:flex;align-items:center;gap:10px;font-size:11px;color:var(--text-tertiary);flex-shrink:0}
.item-meta-pill{
  display:inline-flex;align-items:center;gap:4px;
  padding:2px 8px;border-radius:4px;background:var(--surface-2);
  font-size:11px;font-weight:500;white-space:nowrap;
}
.chevron{color:var(--text-tertiary);font-size:10px;transition:transform .2s;flex-shrink:0}
details[open] .turn-chevron{transform:rotate(90deg)}
.item-body{
  padding:20px;border-top:1px solid var(--border);
  background:rgba(0,0,0,.15);
}
.mermaid-box{
  background:rgba(0,0,0,.25);border:1px solid var(--border-subtle);
  border-radius:var(--radius-sm);padding:28px 20px;overflow-x:auto;
  min-height:200px;
}
.mermaid-box svg{min-width:560px;max-width:100%;height:auto;display:block;margin:0 auto}
.mermaid-box .mermaid{min-width:560px}

/* ── Chat-style conversation turns ── */
.chat-turns{display:flex;flex-direction:column;gap:2px;margin-top:16px}
.chat-turn-header{
  font-size:11px;font-weight:700;color:var(--text-tertiary);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px;
}
.chat-bubble{
  max-width:85%;padding:10px 14px;font-size:13px;line-height:1.55;
  letter-spacing:-.01em;border-radius:var(--radius-sm);
  animation:fadeIn .2s ease-out;
}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.chat-bubble.user{
  align-self:flex-end;
  background:rgba(129,140,248,.12);border:1px solid rgba(129,140,248,.15);
  color:var(--text);border-bottom-right-radius:4px;
}
.chat-bubble.agent{
  align-self:flex-start;
  background:var(--surface-2);border:1px solid var(--border);
  color:var(--text-secondary);border-bottom-left-radius:4px;
}
.chat-meta{
  display:flex;align-items:center;gap:8px;padding:4px 0;
  font-size:10px;color:var(--text-tertiary);
}
.chat-meta.user-side{justify-content:flex-end}
.chat-tool-tag{
  display:inline-flex;align-items:center;gap:3px;
  padding:2px 7px;border-radius:4px;
  background:rgba(129,140,248,.08);border:1px solid rgba(129,140,248,.12);
  font-size:10px;font-weight:600;color:var(--blue);font-family:var(--mono);
}
.chat-eval{
  margin-top:2px;padding:6px 10px;border-radius:var(--radius-xs);
  font-size:11px;font-weight:500;max-width:85%;
}
.chat-eval.pass{background:rgba(52,211,153,.06);border:1px solid rgba(52,211,153,.15);color:var(--green)}
.chat-eval.fail{background:rgba(251,113,133,.06);border:1px solid rgba(251,113,133,.15);color:var(--red)}

/* ── Diff tab ── */
.diff-item{
  background:var(--surface-raised);border:1px solid var(--border);
  border-radius:var(--radius);margin-bottom:10px;overflow:hidden;
  transition:var(--transition);
}
.diff-item:hover{border-color:var(--border-hover)}
.diff-head{padding:14px 20px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-bottom:1px solid var(--border)}
.diff-name{font-weight:600;font-size:14px;flex:1;letter-spacing:-.02em}
.diff-cols{display:grid;grid-template-columns:1fr 1fr}
.diff-col{padding:16px 20px}
.diff-col+.diff-col{border-left:1px solid var(--border)}
.col-title{font-size:10px;font-weight:700;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.tags{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px}
.tag{
  background:var(--surface-2);border:1px solid var(--border);
  border-radius:4px;padding:2px 8px;font-size:11px;font-family:var(--mono);
  font-weight:500;letter-spacing:-.01em;
}
.tag.add{border-color:rgba(52,211,153,.25);color:var(--green);background:rgba(52,211,153,.06)}
.tag.rem{border-color:rgba(251,113,133,.25);color:var(--red);background:rgba(251,113,133,.06)}
.outbox{
  background:rgba(0,0,0,.2);border:1px solid var(--border-subtle);border-radius:var(--radius-xs);
  padding:12px;font:12px/1.6 var(--mono);color:var(--text-tertiary);
  white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;
}
.difflines{
  background:rgba(0,0,0,.2);border:1px solid var(--border-subtle);border-radius:var(--radius-xs);
  padding:10px;font:11px/1.6 var(--mono);max-height:180px;overflow-y:auto;margin-top:8px;
}
.difflines .a{color:var(--green);background:rgba(52,211,153,.05);display:block;padding:0 4px;margin:0 -4px;border-radius:2px}
.difflines .r{color:var(--red);background:rgba(251,113,133,.05);display:block;padding:0 4px;margin:0 -4px;border-radius:2px}
/* Similarity progress bar */
.sim-bar{display:inline-flex;align-items:center;gap:6px;font-size:11px;color:var(--text-tertiary)}
.sim-track{width:48px;height:4px;background:var(--surface-3);border-radius:2px;overflow:hidden;display:inline-block;vertical-align:middle}
.sim-fill{height:100%;border-radius:2px;transition:width .6s cubic-bezier(.4,0,.2,1)}
.sim-fill.high{background:var(--green)}.sim-fill.mid{background:var(--yellow)}.sim-fill.low{background:var(--red)}

/* ── Pipeline vis for tool sequence diff ── */
.pipeline{display:flex;flex-direction:column;gap:8px;padding:16px 20px;border-top:1px solid var(--border)}
.pipeline-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.pipeline-label{font-size:10px;font-weight:700;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:.06em;width:64px;flex-shrink:0}
.pipeline-step{
  display:inline-flex;align-items:center;padding:4px 10px;border-radius:4px;
  font-size:11px;font-family:var(--mono);font-weight:500;
  background:var(--surface-2);border:1px solid var(--border);color:var(--text-secondary);
  position:relative;
}
.pipeline-step+.pipeline-step::before{
  content:'';position:absolute;left:-8px;top:50%;width:6px;height:1px;background:var(--border-hover);
}
.pipeline-step.matched{border-color:rgba(52,211,153,.2);background:rgba(52,211,153,.04)}
.pipeline-step.added{border-color:rgba(52,211,153,.3);color:var(--green);background:rgba(52,211,153,.06)}
.pipeline-step.removed{border-color:rgba(251,113,133,.3);color:var(--red);background:rgba(251,113,133,.06);text-decoration:line-through}

/* ── Timeline ── */
.tl-swimlane{margin-bottom:20px}
.tl-swimlane-label{font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:8px;letter-spacing:-.01em}
.tl-track{display:flex;gap:2px;align-items:center;height:28px}
.tl-bar{
  height:100%;border-radius:4px;display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:600;color:rgba(255,255,255,.8);letter-spacing:-.01em;
  min-width:32px;padding:0 6px;cursor:default;
  transition:var(--transition);position:relative;
}
.tl-bar:hover{filter:brightness(1.2);transform:scaleY(1.15)}
.tl-bar.ok{background:linear-gradient(135deg,rgba(129,140,248,.6),rgba(52,211,153,.4))}
.tl-bar.err{background:linear-gradient(135deg,rgba(251,113,133,.6),rgba(251,191,36,.4))}

/* ── Tables ── */
.ev-table{width:100%;border-collapse:collapse;font-size:13px}
.ev-table th{
  text-align:left;padding:8px 12px;color:var(--text-tertiary);
  font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  border-bottom:1px solid var(--border);background:var(--surface-1);
}
.ev-table td{padding:10px 12px;border-bottom:1px solid var(--border-subtle);transition:background .15s}
.ev-table tr:hover td{background:var(--surface-1)}
.ev-table .mono{font-family:var(--mono);font-size:12px}
.ev-table .num{font-weight:700;font-variant-numeric:tabular-nums}

/* ── Empty states ── */
.empty{text-align:center;padding:80px 40px;color:var(--text-tertiary)}
.empty-icon{font-size:36px;margin-bottom:12px;display:block;opacity:.4}
.empty code{background:var(--surface-3);padding:2px 8px;border-radius:4px;font-family:var(--mono);font-size:12px}

/* ── Trajectory grid ── */
.traj-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)}
.traj-col .col-title{padding-bottom:10px}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:4px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.18)}

/* ── Param diff table ── */
.param-table{width:100%;border-collapse:collapse;font-size:12px}
.param-table th{
  text-align:left;padding:6px 10px;color:var(--text-tertiary);
  font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
  border-bottom:1px solid var(--border);
}
.param-table td{padding:6px 10px;border-bottom:1px solid var(--border-subtle)}

/* ── Compare ── */
table td,table th{transition:background .15s}
</style>
</head>
<body>

<header class="header">
  <div class="logo">
    <div class="logo-icon">◈</div>
    <div>
      <div class="logo-text">{{ title }}</div>
      <div class="logo-sub">{{ generated_at }}{% if notes %} · {{ notes }}{% endif %}</div>
    </div>
  </div>
  <div class="header-right">
    {% if kpis %}
      {% if kpis.failed == 0 %}
        <span class="badge b-green">✓ All Passing</span>
      {% else %}
        <span class="badge b-red">✗ {{ kpis.failed }} Failed</span>
      {% endif %}
      <span class="badge b-blue">{{ kpis.total }} Tests</span>
    {% endif %}
  </div>
</header>

<main class="main">

  <div class="tabbar">
    <button class="tab {% if default_tab == 'overview' %}on{% endif %}" onclick="show('overview',this)">Overview</button>
    <button class="tab {% if default_tab == 'trace' %}on{% endif %}" onclick="show('trace',this)">Execution Trace</button>
    <button class="tab {% if default_tab == 'diffs' %}on{% endif %}" onclick="show('diffs',this)">Diffs</button>
    <button class="tab {% if default_tab == 'timeline' %}on{% endif %}" onclick="show('timeline',this)">Timeline</button>
    {% if compare %}<button class="tab" onclick="show('compare',this)">Compare Runs</button>{% endif %}
  </div>

  <!-- ═══════════ OVERVIEW ═══════════ -->
  <div id="p-overview" class="panel {% if default_tab == 'overview' %}on{% endif %}">
    {% if kpis %}
    <div class="kpi-row">
      <div class="kpi {% if kpis.pass_rate >= 80 %}kpi-pass{% else %}kpi-fail{% endif %}">
        <div class="kpi-top">
          <div class="kpi-label">Pass Rate</div>
          <div class="kpi-ring">
            <svg width="44" height="44" viewBox="0 0 44 44">
              <circle cx="22" cy="22" r="18" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="3"/>
              <circle cx="22" cy="22" r="18" fill="none"
                stroke="{% if kpis.pass_rate >= 80 %}var(--green){% elif kpis.pass_rate >= 60 %}var(--yellow){% else %}var(--red){% endif %}"
                stroke-width="3" stroke-linecap="round"
                stroke-dasharray="{{ (kpis.pass_rate / 100 * 113.1)|round(1) }} 113.1"
                style="filter:drop-shadow(0 0 4px {% if kpis.pass_rate >= 80 %}rgba(52,211,153,.4){% else %}rgba(251,113,133,.4){% endif %})"/>
            </svg>
            <div class="kpi-ring-label">{{ kpis.passed }}/{{ kpis.total }}</div>
          </div>
        </div>
        <div class="kpi-num {% if kpis.pass_rate >= 80 %}c-green{% elif kpis.pass_rate >= 60 %}c-yellow{% else %}c-red{% endif %}">{{ kpis.pass_rate }}%</div>
        <div class="kpi-sub">{{ kpis.passed }} of {{ kpis.total }} tests</div>
      </div>
      <div class="kpi {% if kpis.avg_score >= 80 %}kpi-pass{% else %}kpi-blue{% endif %}">
        <div class="kpi-top">
          <div class="kpi-label">Avg Score</div>
          <div class="kpi-ring">
            <svg width="44" height="44" viewBox="0 0 44 44">
              <circle cx="22" cy="22" r="18" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="3"/>
              <circle cx="22" cy="22" r="18" fill="none"
                stroke="{% if kpis.avg_score >= 80 %}var(--green){% elif kpis.avg_score >= 60 %}var(--yellow){% else %}var(--red){% endif %}"
                stroke-width="3" stroke-linecap="round"
                stroke-dasharray="{{ (kpis.avg_score / 100 * 113.1)|round(1) }} 113.1"
                style="filter:drop-shadow(0 0 4px rgba(129,140,248,.3))"/>
            </svg>
            <div class="kpi-ring-label">{{ kpis.avg_score|int }}</div>
          </div>
        </div>
        <div class="kpi-num {% if kpis.avg_score >= 80 %}c-green{% elif kpis.avg_score >= 60 %}c-yellow{% else %}c-red{% endif %}">{{ kpis.avg_score }}</div>
        <div class="kpi-sub">out of 100</div>
      </div>
      <div class="kpi kpi-blue">
        <div class="kpi-top">
          <div class="kpi-label">Total Cost</div>
        </div>
        <div class="kpi-num c-blue">${{ kpis.total_cost }}</div>
        <div class="kpi-sub">
          {% if kpis.total_tokens %}{{ '{:,}'.format(kpis.total_tokens) }} tokens (verified){% elif kpis.total_cost > 0 %}reported by adapter (no token data){% else %}this run{% endif %}
          {% if kpis.models_display and kpis.models_display != 'Unknown' %}<br>{{ kpis.models_display }}{% endif %}
        </div>
        {% if kpis.total_input_tokens or kpis.total_output_tokens %}
        <div style="margin-top:8px;display:flex;gap:8px;font-size:11px">
          <span style="color:var(--text-tertiary)">in <span style="color:var(--blue);font-weight:600;font-family:var(--mono)">{{ '{:,}'.format(kpis.total_input_tokens) }}</span></span>
          <span style="color:var(--text-tertiary)">out <span style="color:var(--purple);font-weight:600;font-family:var(--mono)">{{ '{:,}'.format(kpis.total_output_tokens) }}</span></span>
        </div>
        {% endif %}
      </div>
      <div class="kpi kpi-blue">
        <div class="kpi-top">
          <div class="kpi-label">Avg Latency</div>
        </div>
        <div class="kpi-num c-blue">{{ kpis.avg_latency_ms|int }}<span style="font-size:14px;font-weight:500;color:var(--text-tertiary);margin-left:2px">ms</span></div>
        <div class="kpi-sub">per test</div>
      </div>
    </div>

    <div class="meta-row">
      <div class="meta-card">
        <div class="meta-label">Agent Model</div>
        <div class="meta-value">{{ kpis.models_display }}</div>
        <div class="meta-sub">{{ kpis.total }} test{% if kpis.total != 1 %}s{% endif %} in this run</div>
      </div>
      {% if kpis.total_tokens %}
      <div class="meta-card">
        <div class="meta-label">Token Usage</div>
        <div class="meta-value">{{ '{:,}'.format(kpis.total_tokens) }} tokens</div>
        <div class="meta-sub">in {{ '{:,}'.format(kpis.total_input_tokens) }} / out {{ '{:,}'.format(kpis.total_output_tokens) }}</div>
      </div>
      {% elif kpis.total_cost > 0 %}
      <div class="meta-card">
        <div class="meta-label">Token Usage</div>
        <div class="meta-value" style="color:var(--yellow)">Not available</div>
        <div class="meta-sub">Your adapter reports cost but not token counts. Cost cannot be independently verified.</div>
      </div>
      {% endif %}
    </div>
    {% if baseline.latest_created_display != 'Unknown' %}
    <div class="meta-row">
      <div class="meta-card">
        <div class="meta-label">Baseline Snapshot</div>
        <div class="meta-value">{{ baseline.latest_created_display }}</div>
        <div class="meta-sub">{% if baseline.models_display != 'Unknown' %}Model: {{ baseline.models_display }}{% endif %}</div>
      </div>
    </div>
    {% endif %}

    {% if judge_usage and judge_usage.call_count %}
    <div class="meta-row">
      <div class="meta-card">
        <div class="meta-label">EvalView Judge{% if judge_usage.model %} ({{ judge_usage.model }}){% endif %}</div>
        <div class="meta-value">
          {% if judge_usage.total_cost > 0 %}
            ${{ judge_usage.total_cost }}
          {% elif judge_usage.is_free %}
            FREE
          {% else %}
            $0
          {% endif %}
        </div>
        <div class="meta-sub">
          {{ '{:,}'.format(judge_usage.total_tokens) }} tokens across {{ judge_usage.call_count }} judge call{% if judge_usage.call_count != 1 %}s{% endif %}
        </div>
      </div>
      <div class="meta-card">
        <div class="meta-label">Judge Token Breakdown</div>
        <div class="meta-value">in {{ '{:,}'.format(judge_usage.input_tokens) }} / out {{ '{:,}'.format(judge_usage.output_tokens) }}</div>
        <div class="meta-sub">{% if judge_usage.pricing %}Rate: {{ judge_usage.pricing }}{% else %}Separate from agent trace cost{% endif %}</div>
      </div>
    </div>
    {% endif %}

    <!-- Score distribution (horizontal bars) + compact donut -->
    <div class="chart-row">
      <div class="card">
        <div class="card-title">Score per Test</div>
        <div style="position:relative;height:{{ [kpis.scores|length * 36 + 40, 180]|max }}px"><canvas id="bars"></canvas></div>
      </div>
      <div class="card">
        <div class="card-title">Distribution</div>
        <div class="chart-wrap"><canvas id="donut"></canvas></div>
      </div>
    </div>

    <!-- Execution cost breakdown -->
    <div class="card">
      <div class="card-title">Execution Cost per Query</div>
      <table class="ev-table">
        {% set has_tokens = traces | selectattr('tokens') | list | length > 0 %}
        <thead>
          <tr>
            <th>Test</th>
            <th>Model</th>
            <th>Trace Cost</th>
            {% if has_tokens %}<th>Tokens</th>{% endif %}
            <th>Latency</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {% for t in traces %}
          <tr>
            <td style="font-weight:600">{{ t.name }}</td>
            <td class="mono" style="color:var(--text-tertiary)">{{ t.model }}</td>
            <td class="mono num" style="color:{% if t.cost == '$0' %}var(--text-tertiary){% else %}var(--blue){% endif %}">{{ t.cost }}</td>
            {% if has_tokens %}<td class="mono" style="color:var(--text-tertiary)">{{ t.tokens or '—' }}</td>{% endif %}
            <td style="color:var(--text-tertiary)">{{ t.latency }}</td>
            <td class="num" style="color:{% if t.score >= 80 %}var(--green){% elif t.score >= 60 %}var(--yellow){% else %}var(--red){% endif %}">{{ t.score }}</td>
          </tr>
          {% endfor %}
          <tr style="background:var(--surface-1)">
            <td style="font-weight:700">Total</td>
            <td style="color:var(--text-tertiary)">—</td>
            <td class="mono num" style="color:var(--blue)">${{ kpis.total_cost }}</td>
            <td colspan="{{ 3 if has_tokens else 2 }}" style="font-size:11px;color:var(--text-tertiary)">avg ${{ '%.6f'|format(kpis.total_cost / kpis.total) if kpis.total else '0' }} per query</td>
          </tr>
        </tbody>
      </table>
      <div style="margin-top:12px;font-size:11px;color:var(--text-tertiary);line-height:1.5">
        Trace cost comes from the agent execution trace only. Mock or non-metered tools will show <code style="background:var(--surface-3);padding:2px 6px;border-radius:4px;font-family:var(--mono);font-size:11px">$0</code> even when EvalView used a separate judge or local model during evaluation.
        {% if judge_usage and judge_usage.call_count %} This check also used {{ judge_usage.call_count }} EvalView judge call{% if judge_usage.call_count != 1 %}s{% endif %} ({{ judge_usage.total_tokens }} tokens).{% endif %}
      </div>
    </div>

    {% else %}
    <div class="empty"><span class="empty-icon">📊</span>No results to display</div>
    {% endif %}
  </div>

  <!-- ═══════════ EXECUTION TRACE ═══════════ -->
  <div id="p-trace" class="panel {% if default_tab == 'trace' %}on{% endif %}">
    {% if traces %}
      {% for t in traces %}
      <div class="item">
        <div class="item-head" onclick="tog('tr{{ loop.index }}',this)">
          <span class="badge {% if t.passed %}b-green{% else %}b-red{% endif %}">{% if t.passed %}✓{% else %}✗{% endif %}</span>
          <span class="item-name">{{ t.name }}</span>
          <div class="item-meta">
            <span class="item-meta-pill" style="color:{% if t.score >= 80 %}var(--green){% elif t.score >= 60 %}var(--yellow){% else %}var(--red){% endif %}">{{ t.score }}/100</span>
            {% if t.cost != "$0" %}<span class="item-meta-pill">💰 {{ t.cost }}</span>{% endif %}
            <span class="item-meta-pill">⚡ {{ t.latency }}</span>
            {% if t.tokens %}<span class="item-meta-pill">{{ t.tokens }}</span>{% endif %}
            <span class="item-meta-pill" style="color:var(--text-tertiary)">🧠 {{ t.model }}</span>
          </div>
          <span class="chevron">▾</span>
        </div>
        <div id="tr{{ loop.index }}" class="item-body" {% if not loop.first %}style="display:none"{% endif %}>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px">
            <span class="badge b-blue">Model: {{ t.model }}</span>
            {% if t.input_tokens or t.output_tokens %}
            <span class="badge b-blue">in {{ '{:,}'.format(t.input_tokens) }} / out {{ '{:,}'.format(t.output_tokens) }} tokens</span>
            {% if t.cost != "$0" %}<span class="badge b-blue">{{ t.cost }}</span>{% endif %}
            {% endif %}
            {% if not t.input_tokens and not t.output_tokens and t.cost != "$0" %}
            <span class="badge b-yellow">{{ t.cost }} (adapter-reported, no token data)</span>
            {% endif %}
            {% if t.baseline_created and t.baseline_created != 'Unknown' %}
            <span class="badge b-purple">Baseline: {{ t.baseline_created }}</span>
            {% endif %}
            {% if t.baseline_model and t.baseline_model != 'Unknown' %}
            <span class="badge b-yellow">Baseline model: {{ t.baseline_model }}</span>
            {% endif %}
          </div>
          {% if t.query %}
          <div style="background:rgba(129,140,248,.06);border:1px solid rgba(129,140,248,.12);border-radius:var(--radius-xs);padding:10px 14px;margin-bottom:14px;font-size:13px;color:var(--text-secondary)">
            <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:rgba(129,140,248,.6);margin-right:8px">Query</span>{{ t.query }}
          </div>
          {% endif %}
          {% if t.has_steps %}
          <div class="mermaid-box"><div class="mermaid">{{ t.diagram }}</div></div>
          {% else %}
          <div style="display:flex;align-items:center;justify-content:center;padding:20px 0 8px">
            <span style="display:inline-flex;align-items:center;gap:8px;background:var(--surface-2);border:1px solid var(--border);border-radius:20px;padding:8px 18px;font-size:12px;color:var(--text-tertiary)">
              <span style="opacity:.5">◎</span> Direct response — no tools invoked
            </span>
          </div>
          {% endif %}
          {% if t.turns %}
          <div class="chat-turns">
            <div class="chat-turn-header">Conversation Turns</div>

            {% for turn in t.turns %}
            <!-- Turn {{ turn.index }} -->
            <div class="chat-meta user-side">
              Turn {{ turn.index }}
              {% if turn.tools %}· {% for tool in turn.tools %}<span class="chat-tool-tag">{{ tool }}</span> {% endfor %}{% endif %}
              · ⚡ {{ turn.latency_ms|round(1) }}ms
              · 💰 ${{ '%.6f'|format(turn.cost) if turn.cost else '0' }}
            </div>
            <div class="chat-bubble user">{{ turn.query }}</div>

            {% if turn.output %}
            <div class="chat-bubble agent">{{ turn.output }}</div>
            {% endif %}

            {% if turn.evaluation %}
            <div class="chat-eval {% if turn.evaluation.passed %}pass{% else %}fail{% endif %}">
              <span style="font-weight:700">
                {% if turn.evaluation.passed %}✅ PASS{% else %}❌ FAIL{% endif %}
              </span>
              {% if turn.evaluation.tool_accuracy is not none %}
              <span style="margin-left:8px;opacity:.7">Tool accuracy: {{ (turn.evaluation.tool_accuracy * 100)|round(0) }}%</span>
              {% endif %}
              {% if turn.evaluation.forbidden_violations %}
              <span style="margin-left:8px;color:var(--red)">Forbidden: {{ turn.evaluation.forbidden_violations|join(', ') }}</span>
              {% endif %}
              {% if turn.evaluation.contains_failed %}
              <span style="margin-left:8px;color:var(--red)">Missing: {{ turn.evaluation.contains_failed|join(', ') }}</span>
              {% endif %}
              {% if turn.evaluation.not_contains_failed %}
              <span style="margin-left:8px;color:var(--red)">Prohibited: {{ turn.evaluation.not_contains_failed|join(', ') }}</span>
              {% endif %}
            </div>
            {% endif %}
            {% endfor %}
          </div>
          {% endif %}
          {% if t.output and not t.turns %}
          <div style="background:rgba(52,211,153,.04);border:1px solid rgba(52,211,153,.1);border-radius:var(--radius-xs);padding:10px 14px;margin-top:14px;font-size:13px;color:var(--text-secondary)">
            <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:rgba(52,211,153,.5);margin-right:8px">Response</span>{{ t.output[:300] }}{% if t.output|length > 300 %}...{% endif %}
          </div>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty"><span class="empty-icon">🔍</span>No trace data available</div>
    {% endif %}
  </div>

  <!-- ═══════════ DIFFS ═══════════ -->
  <div id="p-diffs" class="panel {% if default_tab == 'diffs' %}on{% endif %}">
    {% if diff_rows %}
      {% for d in diff_rows %}
      <div class="diff-item">
        <div class="diff-head">
          {% if d.status == 'regression' %}<span class="badge b-red">⬇ Regression</span>
          {% elif d.status == 'tools_changed' %}<span class="badge b-yellow">⚠ Tools Changed</span>
          {% elif d.status == 'output_changed' %}<span class="badge b-purple">~ Output Changed</span>
          {% else %}<span class="badge b-green">✓ Passed</span>{% endif %}
          <span class="diff-name">{{ d.name }}</span>
          {% if d.score_delta != 0 %}
            <span class="badge {% if d.score_delta > 0 %}b-green{% else %}b-red{% endif %}">{% if d.score_delta > 0 %}+{% endif %}{{ d.score_delta }} pts</span>
          {% endif %}
          <span class="sim-bar">lexical
            <span class="sim-track"><span class="sim-fill {% if d.similarity >= 80 %}high{% elif d.similarity >= 50 %}mid{% else %}low{% endif %}" style="width:{{ d.similarity }}%"></span></span>
            <b style="color:{% if d.similarity >= 80 %}var(--green){% elif d.similarity >= 50 %}var(--yellow){% else %}var(--red){% endif %}">{{ d.similarity }}%</b>
          </span>
          {% if d.semantic_similarity is not none %}
          <span class="sim-bar">semantic
            <span class="sim-track"><span class="sim-fill {% if d.semantic_similarity >= 80 %}high{% elif d.semantic_similarity >= 50 %}mid{% else %}low{% endif %}" style="width:{{ d.semantic_similarity }}%"></span></span>
            <b style="color:{% if d.semantic_similarity >= 80 %}var(--green){% elif d.semantic_similarity >= 50 %}var(--yellow){% else %}var(--red){% endif %}">{{ d.semantic_similarity }}%</b>
          </span>
          {% endif %}
        </div>

        <!-- Tool sequence pipeline -->
        {% if d.golden_tools or d.actual_tools %}
        <div class="pipeline">
          <div class="pipeline-row">
            <span class="pipeline-label">Baseline</span>
            {% for t in d.golden_tools %}<span class="pipeline-step {% if t not in d.actual_tools %}removed{% else %}matched{% endif %}">{{ t }}</span>{% endfor %}
            {% if not d.golden_tools %}<span style="font-size:11px;color:var(--text-tertiary);font-style:italic">No tools</span>{% endif %}
          </div>
          <div class="pipeline-row">
            <span class="pipeline-label">Current</span>
            {% for t in d.actual_tools %}<span class="pipeline-step {% if t not in d.golden_tools %}added{% else %}matched{% endif %}">{{ t }}</span>{% endfor %}
            {% if not d.actual_tools %}<span style="font-size:11px;color:var(--text-tertiary);font-style:italic">No tools</span>{% endif %}
          </div>
        </div>
        {% endif %}

        <div class="diff-cols">
          <div class="diff-col">
            <div class="col-title">Baseline Output</div>
            <div class="tags">{% for t in d.golden_tools %}<span class="tag {% if t not in d.actual_tools %}rem{% endif %}">{{ t }}</span>{% endfor %}</div>
            <div class="outbox">{{ d.golden_out }}</div>
          </div>
          <div class="diff-col">
            <div class="col-title">Current Output</div>
            <div class="tags">{% for t in d.actual_tools %}<span class="tag {% if t not in d.golden_tools %}add{% endif %}">{{ t }}</span>{% endfor %}</div>
            <div class="outbox">{{ d.actual_out }}</div>
            {% if d.diff_lines %}
            <div class="difflines">{% for line in d.diff_lines %}{% if line.startswith('+') %}<div class="a">{{ line }}</div>{% elif line.startswith('-') %}<div class="r">{{ line }}</div>{% else %}<div>{{ line }}</div>{% endif %}{% endfor %}</div>
            {% endif %}
          </div>
        </div>
        {% if d.param_diffs %}
        <div style="padding:16px 20px;border-top:1px solid var(--border)">
          <div class="col-title" style="margin-bottom:12px">Parameter Changes</div>
          <table class="param-table">
            <thead>
              <tr>
                <th>Step</th>
                <th>Tool</th>
                <th>Parameter</th>
                <th>Baseline</th>
                <th>Current</th>
                <th style="text-align:center">Match</th>
              </tr>
            </thead>
            <tbody>
              {% for p in d.param_diffs %}
              <tr>
                <td style="color:var(--text-tertiary)">{{ p.step }}</td>
                <td style="font-family:var(--mono);color:var(--blue)">{{ p.tool }}</td>
                <td style="font-weight:600">{{ p.param }}</td>
                <td style="font-family:var(--mono);font-size:11px;{% if p.type == 'missing' %}color:var(--red){% else %}color:var(--text-tertiary){% endif %}">{{ p.golden or '—' }}</td>
                <td style="font-family:var(--mono);font-size:11px;{% if p.type == 'added' %}color:var(--green){% else %}color:var(--text-tertiary){% endif %}">{{ p.actual or '—' }}</td>
                <td style="text-align:center;font-weight:600;color:{% if p.type == 'added' %}var(--green){% elif p.type == 'missing' %}var(--red){% elif p.similarity is not none %}{% if p.similarity >= 80 %}var(--green){% elif p.similarity >= 50 %}var(--yellow){% else %}var(--red){% endif %}{% else %}var(--yellow){% endif %}">{% if p.type == 'added' %}+new{% elif p.type == 'missing' %}-gone{% elif p.similarity is not none %}{{ p.similarity }}%{% else %}~{% endif %}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        {% endif %}
        {% if d.golden_diagram or d.actual_diagram %}
        <div class="traj-grid">
          <div class="traj-col">
            <div class="col-title">Baseline Trajectory</div>
            <div class="mermaid-box" style="min-height:140px"><div class="mermaid">{{ d.golden_diagram or "sequenceDiagram\n    Note over Agent: No trace data" }}</div></div>
          </div>
          <div class="traj-col">
            <div class="col-title">Current Trajectory</div>
            <div class="mermaid-box" style="min-height:140px"><div class="mermaid">{{ d.actual_diagram or "sequenceDiagram\n    Note over Agent: No trace data" }}</div></div>
          </div>
        </div>
        {% endif %}
      </div>
      {% endfor %}
    {% else %}
      <div class="empty"><span class="empty-icon">✨</span>No diffs yet — run <code>evalview check</code> to compare against a baseline</div>
    {% endif %}
  </div>

  <!-- ═══════════ TIMELINE ═══════════ -->
  <div id="p-timeline" class="panel {% if default_tab == 'timeline' %}on{% endif %}">
    {% if timeline %}
      <div class="card">
        <div class="card-title">Step Latencies</div>
        <div style="position:relative;height:{{ [timeline|length * 38 + 80, 200]|max }}px">
          <canvas id="tlChart"></canvas>
        </div>
      </div>
    {% else %}
      <div class="empty"><span class="empty-icon">⏱</span>No step timing data</div>
    {% endif %}
  </div>

  <!-- ═══════════ COMPARE ═══════════ -->
  {% if compare %}
  <div id="p-compare" class="panel">
    <div class="card" style="margin-bottom:14px">
      <div class="card-title">Pass Rate Across Runs</div>
      <div class="chart-wrap" style="height:240px"><canvas id="cmpPassRate"></canvas></div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <div class="card-title">Avg Score Across Runs</div>
      <div class="chart-wrap" style="height:240px"><canvas id="cmpScore"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title">Run Summary</div>
      <table class="ev-table">
        <thead>
          <tr>
            {% for lbl in compare.labels %}<th>{{ lbl }}</th>{% endfor %}
          </tr>
        </thead>
        <tbody>
          <tr>
            {% for run in compare.runs %}
            <td>
              <div style="font-size:24px;font-weight:800;letter-spacing:-.03em;color:{% if run.pass_rate >= 80 %}var(--green){% else %}var(--red){% endif %}">{{ run.pass_rate }}%</div>
              <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px">{{ run.passed }}/{{ run.total }} · avg {{ run.avg_score }}/100</div>
            </td>
            {% endfor %}
          </tr>
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

</main>

<script>
mermaid.initialize({
  startOnLoad:true,theme:'dark',securityLevel:'loose',
  useMaxWidth:true,
  themeVariables:{
    darkMode:true,
    background:'transparent',
    primaryColor:'rgba(129,140,248,.15)',
    primaryTextColor:'#e2e8f0',
    primaryBorderColor:'rgba(129,140,248,.3)',
    lineColor:'rgba(148,163,184,.3)',
    secondaryColor:'rgba(52,211,153,.1)',
    tertiaryColor:'rgba(192,132,252,.1)',
    noteBkgColor:'rgba(129,140,248,.08)',
    noteTextColor:'#94a3b8',
    noteBorderColor:'rgba(129,140,248,.2)',
    actorBkg:'rgba(129,140,248,.12)',
    actorBorder:'rgba(129,140,248,.25)',
    actorTextColor:'#e2e8f0',
    signalColor:'#94a3b8',
    signalTextColor:'#cbd5e1',
    activationBkgColor:'rgba(129,140,248,.08)',
    activationBorderColor:'rgba(129,140,248,.2)'
  },
  sequence:{
    useMaxWidth:true,
    width:180,
    wrap:false,
    actorFontFamily:'Inter,sans-serif',
    noteFontFamily:'Inter,sans-serif',
    messageFontFamily:'Inter,sans-serif',
    actorFontSize:12,
    messageFontSize:11,
    noteFontSize:10,
    boxTextMargin:8,
    mirrorActors:false,
    messageAlign:'center',
    actorMargin:30,
    bottomMarginAdj:4,
    boxMargin:8,
    noteMargin:8
  }
});

function show(id,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
  document.getElementById('p-'+id).classList.add('on');
  btn.classList.add('on');
}
function tog(id,head){
  const el=document.getElementById(id);
  const open=el.style.display!=='none';
  el.style.display=open?'none':'block';
  head.querySelector('.chevron').style.transform=open?'':'rotate(180deg)';
}

{% if kpis %}
(function(){
  const passed={{ kpis.passed }},failed={{ kpis.failed }};
  const scores={{ kpis.scores|tojson }},names={{ kpis.test_names|tojson }};
  const tc='rgba(148,163,184,.7)',gc='rgba(255,255,255,.04)';

  /* Compact donut */
  new Chart(document.getElementById('donut'),{
    type:'doughnut',
    data:{labels:['Passed','Failed'],datasets:[{
      data:[passed,failed],
      backgroundColor:['rgba(52,211,153,.7)','rgba(251,113,133,.7)'],
      borderColor:['rgba(52,211,153,.15)','rgba(251,113,133,.15)'],
      borderWidth:2,hoverOffset:6
    }]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'78%',
      plugins:{legend:{position:'bottom',labels:{color:tc,font:{family:'Inter',size:11,weight:'500'},padding:16,boxWidth:8,boxHeight:8,usePointStyle:true,pointStyle:'circle'}},
      tooltip:{backgroundColor:'rgba(10,14,26,.9)',borderColor:'rgba(255,255,255,.1)',borderWidth:1,titleFont:{family:'Inter',weight:'600'},bodyFont:{family:'Inter'},padding:10,cornerRadius:8,
        callbacks:{label:ctx=>` ${ctx.label}: ${ctx.raw}`}}}}
  });

  /* Horizontal bar chart sorted by score */
  const sorted=names.map((n,i)=>({name:n,score:scores[i]})).sort((a,b)=>b.score-a.score);
  new Chart(document.getElementById('bars'),{
    type:'bar',
    data:{labels:sorted.map(s=>s.name),datasets:[{
      label:'Score',data:sorted.map(s=>s.score),
      backgroundColor:sorted.map(s=>s.score>=80?'rgba(52,211,153,.5)':s.score>=60?'rgba(251,191,36,.5)':'rgba(251,113,133,.5)'),
      borderColor:sorted.map(s=>s.score>=80?'rgba(52,211,153,.7)':s.score>=60?'rgba(251,191,36,.7)':'rgba(251,113,133,.7)'),
      borderWidth:1,borderRadius:4,borderSkipped:false,
      barPercentage:.7,categoryPercentage:.8
    }]},
    options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      scales:{
        x:{min:0,max:100,grid:{color:gc},ticks:{color:tc,font:{family:'Inter',size:10},callback:v=>v},border:{display:false}},
        y:{grid:{display:false},ticks:{color:'rgba(148,163,184,.9)',font:{family:'Inter',size:11,weight:'500'}},border:{display:false}}
      },
      plugins:{legend:{display:false},tooltip:{backgroundColor:'rgba(10,14,26,.9)',borderColor:'rgba(255,255,255,.1)',borderWidth:1,titleFont:{family:'Inter',weight:'600'},bodyFont:{family:'Inter'},padding:10,cornerRadius:8,
        callbacks:{label:ctx=>` Score: ${ctx.raw}/100`}}}}
  });
})();
{% endif %}

{% if timeline %}
(function(){
  const tl={{ timeline|tojson }};
  if(!tl.length) return;
  const labels=tl.map(r=>r.label||(r.test+' \u203a '+r.tool));
  const vals=tl.map(r=>r.latency||0);
  const costs=tl.map(r=>r.cost||0);
  const maxLatency=Math.max(...vals, 0);
  /* Color intensity by cost */
  const maxCost=Math.max(...costs,0.000001);
  const colors=tl.map((r,i)=>{
    if(!r.success) return 'rgba(251,113,133,.6)';
    const intensity=0.3+0.5*(costs[i]/maxCost);
    return `rgba(129,140,248,${intensity.toFixed(2)})`;
  });
  const borders=tl.map(r=>r.success?'rgba(129,140,248,.7)':'rgba(251,113,133,.7)');
  new Chart(document.getElementById('tlChart'),{
    type:'bar',
    data:{labels,datasets:[{label:'ms',data:vals,backgroundColor:colors,borderColor:borders,borderWidth:1,borderRadius:4,borderSkipped:false,barPercentage:.7}]},
    options:{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      scales:{
        x:{
          suggestedMax:maxLatency > 0 ? maxLatency * 1.15 : 1,
          grid:{color:'rgba(255,255,255,.04)'},
          ticks:{color:'rgba(148,163,184,.7)',font:{family:'Inter',size:10},callback:v=>v+'ms'},
          border:{display:false}
        },
        y:{grid:{display:false},ticks:{color:'rgba(148,163,184,.8)',font:{family:'Inter',size:11}},border:{display:false}}
      },
      plugins:{legend:{display:false},tooltip:{backgroundColor:'rgba(10,14,26,.9)',borderColor:'rgba(255,255,255,.1)',borderWidth:1,titleFont:{family:'Inter',weight:'600'},bodyFont:{family:'Inter'},padding:10,cornerRadius:8,
        callbacks:{
          label:ctx=>` ${ctx.raw}ms`,
          afterLabel:ctx=>` Cost: $${(costs[ctx.dataIndex] || 0).toFixed(6)}`,
          title:ctx=>ctx[0].label
        }}}
    }
  });
})();
{% endif %}

{% if compare %}
(function(){
  const labels={{ compare.labels|tojson }};
  const passRates={{ compare.runs|map(attribute='pass_rate')|list|tojson }};
  const avgScores={{ compare.runs|map(attribute='avg_score')|list|tojson }};
  const tc='rgba(148,163,184,.7)',gc='rgba(255,255,255,.04)';
  const colors=['rgba(129,140,248,.6)','rgba(52,211,153,.6)','rgba(251,113,133,.6)','rgba(251,191,36,.6)','rgba(192,132,252,.6)'];
  const borders=['rgba(129,140,248,.8)','rgba(52,211,153,.8)','rgba(251,113,133,.8)','rgba(251,191,36,.8)','rgba(192,132,252,.8)'];
  const opts={responsive:true,maintainAspectRatio:false,
    scales:{y:{grid:{color:gc},ticks:{color:tc},border:{display:false}},x:{grid:{display:false},ticks:{color:tc,font:{size:11}},border:{display:false}}},
    plugins:{legend:{display:false}}};
  new Chart(document.getElementById('cmpPassRate'),{type:'bar',
    data:{labels,datasets:[{label:'Pass Rate %',data:passRates,
      backgroundColor:colors.slice(0,labels.length),borderColor:borders.slice(0,labels.length),
      borderWidth:1,borderRadius:8,borderSkipped:false}]},
    options:{...opts,scales:{...opts.scales,y:{...opts.scales.y,min:0,max:100}}}});
  new Chart(document.getElementById('cmpScore'),{type:'bar',
    data:{labels,datasets:[{label:'Avg Score',data:avgScores,
      backgroundColor:colors.slice(0,labels.length),borderColor:borders.slice(0,labels.length),
      borderWidth:1,borderRadius:8,borderSkipped:false}]},
    options:{...opts,scales:{...opts.scales,y:{...opts.scales.y,min:0,max:100}}}});
})();
{% endif %}
</script>

<!-- Share bar -->
<div style="
  position:fixed;bottom:0;left:0;right:0;z-index:100;
  background:rgba(10,14,26,.92);backdrop-filter:blur(16px);
  -webkit-backdrop-filter:blur(16px);
  border-top:1px solid var(--border);
  padding:10px 24px;
  display:flex;align-items:center;justify-content:space-between;
  font-family:var(--font);font-size:12px;color:var(--text-tertiary);
">
  <span>
    Built with <a href="https://github.com/hidai25/eval-view" target="_blank" rel="noopener" style="color:var(--blue);text-decoration:none;font-weight:600">EvalView</a>
    <span style="opacity:.3;margin:0 6px">|</span>
    Agent testing &amp; regression detection
  </span>
  <span style="display:flex;align-items:center;gap:6px">
    <a href="https://twitter.com/intent/tweet?text=Testing%20my%20AI%20agent%20with%20EvalView%20%E2%80%94%20catches%20regressions%20before%20they%20ship.%20%F0%9F%9B%A1%EF%B8%8F&url=https%3A%2F%2Fgithub.com%2Fhidai25%2Feval-view"
       target="_blank" rel="noopener"
       style="display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:6px;background:rgba(29,155,240,.1);color:#1d9bf0;text-decoration:none;font-weight:600;font-size:11px;transition:all .15s;border:1px solid rgba(29,155,240,.15)"
       onmouseover="this.style.background='rgba(29,155,240,.2)';this.style.borderColor='rgba(29,155,240,.3)'" onmouseout="this.style.background='rgba(29,155,240,.1)';this.style.borderColor='rgba(29,155,240,.15)'">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
      Share
    </a>
    <a href="https://github.com/hidai25/eval-view"
       target="_blank" rel="noopener"
       style="display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:6px;background:var(--surface-2);color:var(--text-secondary);text-decoration:none;font-weight:600;font-size:11px;transition:all .15s;border:1px solid var(--border)"
       onmouseover="this.style.background='var(--surface-3)';this.style.borderColor='var(--border-hover)'" onmouseout="this.style.background='var(--surface-2)';this.style.borderColor='var(--border)'">
      <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>
      Star
    </a>
  </span>
</div>
<div style="height:44px"></div><!-- spacer for fixed bar -->

</body>
</html>"""
