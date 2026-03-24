"""Monitor command — continuous regression detection with Slack alerts."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import click

from evalview.commands.shared import (
    console,
    _load_config_if_exists,
    _parse_fail_statuses,
    _execute_check_tests,
    _analyze_check_diffs,
)
from evalview.core.diff import DiffStatus
from evalview.telemetry.decorators import track_command


class MonitorError(Exception):
    """Raised when monitor cannot start due to missing prerequisites."""


def _resolve_slack_webhook(
    cli_flag: Optional[str],
    config: Any,
) -> Optional[str]:
    """Resolve Slack webhook URL with clear priority: CLI flag > config > env var."""
    if cli_flag:
        return cli_flag
    if config:
        monitor_cfg = config.get_monitor_config()
        if monitor_cfg.slack_webhook:
            return monitor_cfg.slack_webhook
    return os.environ.get("EVALVIEW_SLACK_WEBHOOK")


def _append_history(history_path: Path, record: dict) -> None:
    """Append one JSON record to the JSONL history file.

    Creates parent directories and the file if they do not exist.
    Each call appends a single newline-terminated JSON object.
    """
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _detect_spikes(
    results: List[Any],
    golden_traces: Dict[str, Any],
    cost_threshold: Optional[float],
    latency_threshold: Optional[float],
) -> List[Dict[str, Any]]:
    """Compare current cost/latency against golden baselines. Returns list of alerts."""
    alerts: List[Dict[str, Any]] = []
    if not cost_threshold and not latency_threshold:
        return alerts

    for r in results:
        name = r.test_case
        golden = golden_traces.get(name)
        if not golden:
            continue

        baseline_cost = golden.trace.metrics.total_cost
        baseline_latency = golden.trace.metrics.total_latency
        current_cost = r.trace.metrics.total_cost
        current_latency = r.trace.metrics.total_latency

        if cost_threshold and baseline_cost > 0 and current_cost / baseline_cost > cost_threshold:
            alerts.append({
                "test_name": name,
                "alert_type": "cost_spike",
                "current": current_cost,
                "baseline": baseline_cost,
                "multiplier": current_cost / baseline_cost,
            })

        if latency_threshold and baseline_latency > 0 and current_latency / baseline_latency > latency_threshold:
            alerts.append({
                "test_name": name,
                "alert_type": "latency_spike",
                "current": current_latency,
                "baseline": baseline_latency,
                "multiplier": current_latency / baseline_latency,
            })

    return alerts


def _run_monitor_loop(
    test_path: str,
    interval: int,
    slack_webhook: Optional[str],
    fail_on: str,
    timeout: float,
    test_filter: Optional[str],
    config: Any = None,
    history_path: Optional[Path] = None,
    cost_threshold: Optional[float] = None,
    latency_threshold: Optional[float] = None,
) -> None:
    """Main monitor loop. Runs check cycles until Ctrl+C.

    Raises:
        MonitorError: If baselines or test cases are missing.
    """
    from evalview.core.loader import TestCaseLoader
    from evalview.core.golden import GoldenStore
    from evalview.core.messages import (
        get_random_monitor_start_message,
        get_random_monitor_cycle_message,
        get_random_monitor_clean_message,
    )
    from evalview.core.slack_notifier import SlackNotifier

    notifier = SlackNotifier(slack_webhook) if slack_webhook else None

    # Verify snapshots exist
    store = GoldenStore()
    goldens = store.list_golden()
    if not goldens:
        raise MonitorError("No baselines found. Run `evalview snapshot` first.")

    # Load test cases
    loader = TestCaseLoader()
    test_cases = loader.load_from_directory(Path(test_path))

    if test_filter:
        test_cases = [tc for tc in test_cases if tc.name == test_filter]
        if not test_cases:
            raise MonitorError(f"No test found with name: {test_filter}")

    console.print(f"\n[cyan]{get_random_monitor_start_message()}[/cyan]")
    history_hint = f"  |  History: {history_path}" if history_path else ""
    console.print(f"[dim]  Tests: {len(test_cases)}  |  Interval: {interval}s  |  Slack: {'✓' if notifier else '—'}{history_hint}[/dim]")
    console.print("[dim]  Press Ctrl+C to stop.[/dim]\n")

    # Track state across cycles
    previously_failing: Set[str] = set()
    cycle_count = 0
    total_cost = 0.0
    shutdown = False

    original_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(sig: int, frame: Any) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _handle_sigint)

    fail_statuses = _parse_fail_statuses(fail_on)

    try:
        while not shutdown:
            cycle_count += 1
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            console.print(f"[dim][{now}][/dim] {get_random_monitor_cycle_message()}")

            try:
                diffs, results, _, golden_traces = _execute_check_tests(
                    test_cases, config, json_output=True, timeout=timeout
                )
            except Exception as e:
                console.print(f"[red]  ✗ Cycle {cycle_count} failed: {e}[/red]")
                _sleep_interruptible(interval, lambda: shutdown)
                continue

            # Track cumulative cost
            cycle_cost = sum(r.trace.metrics.total_cost for r in results)
            total_cost += cycle_cost

            analysis = _analyze_check_diffs(diffs)

            # Determine currently failing tests (only those matching fail_on)
            currently_failing: Set[str] = set()
            for name, diff in diffs:
                if diff.overall_severity in fail_statuses:
                    currently_failing.add(name)

            # Count severity buckets for history record and display
            regressions = sum(1 for _, d in diffs if d.overall_severity == DiffStatus.REGRESSION)
            tools_changed = sum(1 for _, d in diffs if d.overall_severity == DiffStatus.TOOLS_CHANGED)
            output_changed = sum(1 for _, d in diffs if d.overall_severity == DiffStatus.OUTPUT_CHANGED)
            passed = len(diffs) - regressions - tools_changed - output_changed

            if not currently_failing:
                cost_part = f"  [dim]${cycle_cost:.4f}[/dim]" if cycle_cost > 0 else ""
                console.print(f"[green]  {get_random_monitor_clean_message()} ({len(diffs)} tests){cost_part}[/green]")

                # Send recovery if we were failing before
                if previously_failing and notifier:
                    asyncio.run(notifier.send_recovery_alert(len(diffs)))
                    console.print("[dim]  📤 Slack: recovery notification sent[/dim]")
            else:
                # Show summary using analysis dict
                parts = []
                if analysis["has_regressions"]:
                    parts.append(f"[red]{regressions} regression{'s' if regressions > 1 else ''}[/red]")
                if analysis["has_tools_changed"]:
                    parts.append(f"[yellow]{tools_changed} tool change{'s' if tools_changed > 1 else ''}[/yellow]")
                if analysis["has_output_changed"]:
                    parts.append(f"[dim]{output_changed} output change{'s' if output_changed > 1 else ''}[/dim]")

                console.print(f"  ⚠  {', '.join(parts)}")

                for name, diff in diffs:
                    if diff.overall_severity in fail_statuses:
                        console.print(f"    [red]✗ {name}[/red] ({diff.overall_severity.value})")

                # Only alert on NEW failures (avoid spamming)
                new_failures = currently_failing - previously_failing
                if new_failures and notifier:
                    alert_diffs = [(n, d) for n, d in diffs if n in currently_failing]
                    asyncio.run(notifier.send_regression_alert(alert_diffs, analysis))
                    console.print(f"[dim]  📤 Slack: alerted on {len(new_failures)} new failure(s)[/dim]")

            # Detect cost/latency spikes against golden baselines
            spike_alerts = _detect_spikes(results, golden_traces, cost_threshold, latency_threshold)
            if spike_alerts:
                for a in spike_alerts:
                    if a["alert_type"] == "cost_spike":
                        console.print(
                            f"  [yellow]💰 {a['test_name']}: cost spike "
                            f"${a['baseline']:.4f} → ${a['current']:.4f} ({a['multiplier']:.1f}x)[/yellow]"
                        )
                    else:
                        console.print(
                            f"  [yellow]⏱  {a['test_name']}: latency spike "
                            f"{a['baseline']:.1f}s → {a['current']:.1f}s ({a['multiplier']:.1f}x)[/yellow]"
                        )
                if notifier:
                    asyncio.run(notifier.send_cost_latency_alert(spike_alerts))
                    console.print(f"[dim]  📤 Slack: {len(spike_alerts)} performance alert(s) sent[/dim]")

            # Append cycle record to JSONL history file if requested
            if history_path is not None:
                record = {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "cycle": cycle_count,
                    "total_tests": len(diffs),
                    "passed": passed,
                    "regressions": regressions,
                    "tools_changed": tools_changed,
                    "output_changed": output_changed,
                    "cost": round(cycle_cost, 6),
                    "failing_tests": sorted(currently_failing),
                    "cost_alerts": sum(1 for a in spike_alerts if a["alert_type"] == "cost_spike"),
                    "latency_alerts": sum(1 for a in spike_alerts if a["alert_type"] == "latency_spike"),
                }
                _append_history(history_path, record)

            previously_failing = currently_failing

            _sleep_interruptible(interval, lambda: shutdown)
    finally:
        signal.signal(signal.SIGINT, original_sigint)

    # Summary on exit
    console.print(f"\n[cyan]Monitor stopped after {cycle_count} cycle(s).[/cyan]")
    if total_cost > 0:
        console.print(f"[dim]  Total cost: ${total_cost:.4f}[/dim]")
    if history_path is not None and cycle_count > 0:
        console.print(f"[dim]  History written to: {history_path}[/dim]")
    console.print()


def _run_monitor_dashboard(
    test_path: str,
    interval: int,
    slack_webhook: Optional[str],
    fail_on: str,
    timeout: float,
    test_filter: Optional[str],
    config: Any = None,
    history_path: Optional[Path] = None,
    cost_threshold: Optional[float] = None,
    latency_threshold: Optional[float] = None,
) -> None:
    """Monitor loop with a live-updating Rich dashboard.

    Single screen that refreshes each cycle instead of scrolling logs.
    """
    from rich.live import Live
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.columns import Columns

    from evalview.core.loader import TestCaseLoader
    from evalview.core.golden import GoldenStore
    from evalview.core.slack_notifier import SlackNotifier

    notifier = SlackNotifier(slack_webhook) if slack_webhook else None

    store = GoldenStore()
    goldens = store.list_golden()
    if not goldens:
        raise MonitorError("No baselines found. Run `evalview snapshot` first.")

    loader = TestCaseLoader()
    test_cases = loader.load_from_directory(Path(test_path))

    if test_filter:
        test_cases = [tc for tc in test_cases if tc.name == test_filter]
        if not test_cases:
            raise MonitorError(f"No test found with name: {test_filter}")

    fail_statuses = _parse_fail_statuses(fail_on)

    # Status emoji for last-N history
    _STATUS_EMOJI = {
        DiffStatus.PASSED: "[green]●[/green]",
        DiffStatus.TOOLS_CHANGED: "[yellow]●[/yellow]",
        DiffStatus.OUTPUT_CHANGED: "[yellow]●[/yellow]",
        DiffStatus.REGRESSION: "[red]●[/red]",
    }

    # Track state
    previously_failing: Set[str] = set()
    cycle_count = 0
    total_cost = 0.0
    start_time = time.time()
    last_check_time = ""
    next_check_time = ""
    slack_alerts_sent = 0
    test_history: Dict[str, List[DiffStatus]] = {}  # test_name → last N statuses
    current_statuses: Dict[str, DiffStatus] = {}
    checking = False
    error_msg = ""
    shutdown = False

    original_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(sig: int, frame: Any) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, _handle_sigint)

    def _build_dashboard() -> Panel:
        """Build the dashboard panel for the current state."""
        uptime_secs = int(time.time() - start_time)
        uptime_m = uptime_secs // 60
        uptime_s = uptime_secs % 60

        # Header stats
        header = Text()
        header.append(f"  Cycle: {cycle_count}", style="bold")
        header.append(f"  |  Uptime: {uptime_m}m{uptime_s:02d}s")
        header.append(f"  |  Cost: ${total_cost:.4f}")
        header.append(f"  |  Slack: {slack_alerts_sent} alert{'s' if slack_alerts_sent != 1 else ''}")

        # Test table
        table = Table(show_header=True, header_style="bold", expand=True, padding=(0, 1))
        table.add_column("Test", style="bold", ratio=3)
        table.add_column("Status", ratio=2)
        table.add_column("History", ratio=2, justify="center")

        for tc in test_cases:
            name = tc.name
            status = current_statuses.get(name)

            if status is None:
                status_str = "[dim]pending[/dim]"
            elif status == DiffStatus.PASSED:
                status_str = "[green]PASSED[/green]"
            elif status == DiffStatus.REGRESSION:
                status_str = "[red]REGRESSION[/red]"
            elif status == DiffStatus.TOOLS_CHANGED:
                status_str = "[yellow]TOOLS_CHANGED[/yellow]"
            elif status == DiffStatus.OUTPUT_CHANGED:
                status_str = "[yellow]OUTPUT_CHANGED[/yellow]"
            else:
                status_str = f"[dim]{status.value}[/dim]"

            # History dots (last 5 cycles)
            history = test_history.get(name, [])
            dots = " ".join(
                _STATUS_EMOJI.get(s, "[dim]○[/dim]") for s in history[-5:]
            )
            if not dots:
                dots = "[dim]○ ○ ○ ○ ○[/dim]"

            table.add_row(name, status_str, dots)

        # Footer
        footer = Text()
        if checking:
            footer.append("  Checking...", style="cyan")
        elif error_msg:
            footer.append(f"  Error: {error_msg}", style="red")
        else:
            footer.append(f"  Last: {last_check_time}", style="dim")
            footer.append(f"  |  Next: {next_check_time}", style="dim")
        footer.append("  |  Press Ctrl+C to stop", style="dim")

        # Assemble
        from rich.console import Group
        content = Group(header, "", table, "", footer)
        return Panel(content, title="EvalView Monitor", border_style="blue")

    with Live(_build_dashboard(), console=console, refresh_per_second=1) as live:
        while not shutdown:
            # Run check cycle
            cycle_count += 1
            checking = True
            last_check_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            live.update(_build_dashboard())

            try:
                diffs, results, _, golden_traces = _execute_check_tests(
                    test_cases, config, json_output=True, timeout=timeout
                )
                error_msg = ""
            except Exception as e:
                error_msg = str(e)[:60]
                checking = False
                live.update(_build_dashboard())
                _sleep_interruptible(interval, lambda: shutdown)
                continue

            checking = False

            # Update state
            cycle_cost = sum(r.trace.metrics.total_cost for r in results)
            total_cost += cycle_cost

            for name, diff in diffs:
                current_statuses[name] = diff.overall_severity
                if name not in test_history:
                    test_history[name] = []
                test_history[name].append(diff.overall_severity)

            # Slack alerts (same logic as scrolling mode)
            currently_failing: Set[str] = set()
            for name, diff in diffs:
                if diff.overall_severity in fail_statuses:
                    currently_failing.add(name)

            if not currently_failing and previously_failing and notifier:
                asyncio.run(notifier.send_recovery_alert(len(diffs)))
                slack_alerts_sent += 1

            new_failures = currently_failing - previously_failing
            if new_failures and notifier:
                alert_diffs = [(n, d) for n, d in diffs if n in currently_failing]
                analysis = _analyze_check_diffs(diffs)
                asyncio.run(notifier.send_regression_alert(alert_diffs, analysis))
                slack_alerts_sent += 1

            # Cost/latency spike alerts
            spike_alerts = _detect_spikes(results, golden_traces, cost_threshold, latency_threshold)
            if spike_alerts and notifier:
                asyncio.run(notifier.send_cost_latency_alert(spike_alerts))
                slack_alerts_sent += 1

            # History file
            if history_path is not None:
                regressions = sum(1 for _, d in diffs if d.overall_severity == DiffStatus.REGRESSION)
                tools_changed = sum(1 for _, d in diffs if d.overall_severity == DiffStatus.TOOLS_CHANGED)
                output_changed = sum(1 for _, d in diffs if d.overall_severity == DiffStatus.OUTPUT_CHANGED)
                passed = len(diffs) - regressions - tools_changed - output_changed
                record = {
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "cycle": cycle_count,
                    "total_tests": len(diffs),
                    "passed": passed,
                    "regressions": regressions,
                    "tools_changed": tools_changed,
                    "output_changed": output_changed,
                    "cost": round(cycle_cost, 6),
                    "failing_tests": sorted(currently_failing),
                }
                _append_history(history_path, record)

            previously_failing = currently_failing

            # Update dashboard and wait
            next_time = datetime.now(timezone.utc).timestamp() + interval
            next_check_time = datetime.fromtimestamp(next_time, tz=timezone.utc).strftime("%H:%M:%S UTC")
            live.update(_build_dashboard())

            _sleep_interruptible(interval, lambda: shutdown)

    signal.signal(signal.SIGINT, original_sigint)
    console.print(f"\n[cyan]Monitor stopped after {cycle_count} cycle(s).[/cyan]")
    if total_cost > 0:
        console.print(f"[dim]  Total cost: ${total_cost:.4f}[/dim]")
    console.print()


def _sleep_interruptible(seconds: int, should_stop: Any) -> None:
    """Sleep in 1-second ticks so Ctrl+C is responsive."""
    for _ in range(seconds):
        if should_stop():
            break
        time.sleep(1)


@click.command("monitor")
@click.argument("test_path", default="tests", type=click.Path(exists=True))
@click.option("--interval", "-i", type=int, default=None, help="Seconds between checks (default: 300)")
@click.option("--slack-webhook", default=None, help="Slack webhook URL for alerts")
@click.option("--fail-on", default=None, help="Comma-separated statuses that trigger alerts (default: REGRESSION)")
@click.option("--timeout", type=float, default=None, help="Timeout per test in seconds (default: 30)")
@click.option("--test", "-t", "test_filter", default=None, help="Monitor only this specific test")
@click.option(
    "--history",
    "history_path",
    default=None,
    type=click.Path(),
    help="Append each cycle's results to a JSONL file",
)
@click.option("--alert-cost-spike", "cost_spike", type=float, default=None, help="Alert when cost exceeds baseline by this multiplier (e.g. 2.0)")
@click.option("--alert-latency-spike", "latency_spike", type=float, default=None, help="Alert when latency exceeds baseline by this multiplier (e.g. 3.0)")
@click.option("--dashboard", is_flag=True, help="Live-updating terminal dashboard instead of scrolling logs")
@track_command("monitor")
def monitor(
    test_path: str,
    interval: Optional[int],
    slack_webhook: Optional[str],
    fail_on: Optional[str],
    timeout: Optional[float],
    test_filter: Optional[str],
    history_path: Optional[str],
    cost_spike: Optional[float],
    latency_spike: Optional[float],
    dashboard: bool = False,
) -> None:
    """Continuously check for regressions with optional Slack alerts.

    Runs evalview check in a loop, alerting you when regressions appear
    and notifying when they're resolved. Designed for production monitoring.

    \b
    Examples:
        evalview monitor                                # Check every 5 min
        evalview monitor --interval 60                  # Check every minute
        evalview monitor --slack-webhook https://...    # Alert to Slack
        evalview monitor --test "weather-lookup"        # Monitor one test
        evalview monitor --fail-on REGRESSION,TOOLS_CHANGED
        evalview monitor --history monitor_log.jsonl    # Persist cycle history
        evalview monitor --alert-cost-spike 2.0        # Alert if cost doubles
        evalview monitor --alert-latency-spike 3.0     # Alert if latency triples

    \b
    Configuration (config.yaml):
        monitor:
          interval: 300
          slack_webhook: https://hooks.slack.com/services/...
          fail_on: [REGRESSION]
          cost_threshold: 2.0
          latency_threshold: 3.0

    \b
    Environment variables:
        EVALVIEW_SLACK_WEBHOOK    Slack webhook URL (fallback)
    """
    # Resolve defaults from config file
    from evalview.core.config import apply_judge_config

    config = _load_config_if_exists()
    apply_judge_config(config)
    monitor_cfg = config.get_monitor_config() if config else None

    resolved_interval = interval or (monitor_cfg.interval if monitor_cfg else 300)
    resolved_fail_on = fail_on or (",".join(monitor_cfg.fail_on) if monitor_cfg else "REGRESSION")
    resolved_timeout = timeout or (monitor_cfg.timeout if monitor_cfg else 30.0)

    if resolved_interval < 10:
        click.echo("Error: --interval must be at least 10 seconds.", err=True)
        sys.exit(1)

    if resolved_timeout <= 0:
        click.echo("Error: --timeout must be a positive number.", err=True)
        sys.exit(1)

    resolved_history = Path(history_path) if history_path else None
    resolved_cost_threshold = cost_spike or (monitor_cfg.cost_threshold if monitor_cfg else None)
    resolved_latency_threshold = latency_spike or (monitor_cfg.latency_threshold if monitor_cfg else None)

    try:
        if dashboard:
            _run_monitor_dashboard(
                test_path=test_path,
                interval=resolved_interval,
                slack_webhook=_resolve_slack_webhook(slack_webhook, config),
                fail_on=resolved_fail_on,
                timeout=resolved_timeout,
                test_filter=test_filter,
                config=config,
                history_path=resolved_history,
                cost_threshold=resolved_cost_threshold,
                latency_threshold=resolved_latency_threshold,
            )
        else:
            _run_monitor_loop(
                test_path=test_path,
                interval=resolved_interval,
                slack_webhook=_resolve_slack_webhook(slack_webhook, config),
                fail_on=resolved_fail_on,
                timeout=resolved_timeout,
                test_filter=test_filter,
                config=config,
                history_path=resolved_history,
                cost_threshold=resolved_cost_threshold,
                latency_threshold=resolved_latency_threshold,
            )
    except MonitorError as e:
        console.print(f"[red]❌ {e}[/red]")
        sys.exit(1)
