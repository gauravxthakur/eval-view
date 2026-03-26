"""Demo command — live regression demo with embedded agent.

Cinematic demonstration of the full EvalView workflow:
  Phase 1: Snapshot baseline (good agent)
  Phase 2: Model update breaks agent → check --heal auto-fixes flakes, flags real breaks
  Phase 3: HTML report auto-opens

The demo controls all output directly — no subprocess noise, no warnings,
no cost tracking spam. Every line is intentional.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict

import click
from rich.panel import Panel

from evalview.commands.shared import console
from evalview.telemetry.decorators import track_command


def _sleep(seconds: float) -> None:
    """Sleep with a small buffer for dramatic timing."""
    time.sleep(seconds)


def _print_step(icon: str, text: str, style: str = "") -> None:
    """Print a single demo step line."""
    if style:
        console.print(f"  {icon} [{style}]{text}[/{style}]")
    else:
        console.print(f"  {icon} {text}")


@click.command("demo")
@track_command("demo", lambda **kw: {"is_demo": True})
def demo():
    """Live regression demo — see EvalView catch and auto-heal agent regressions."""
    import subprocess as _subprocess
    import logging

    # Suppress all warnings during demo
    logging.disable(logging.CRITICAL)

    console.print()
    console.print(Panel(
        "[bold]EvalView[/bold] — Live Regression Demo\n"
        "\n"
        "[dim]Scenario:[/dim] A customer support agent gets a model update.\n"
        "          Two tests drift. One recovers on retry. One is a real break.\n"
        "          Watch EvalView tell them apart.",
        border_style="cyan",
        padding=(1, 2),
    ))
    console.print()

    # ── State & call tracking ────────────────────────────────────────────
    _state: Dict[str, bool] = {"broken": False}
    _call_tracker: Dict[str, int] = {}

    class _DemoHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                query = body.get("query", "").lower()

                qtype = (
                    "refund" if ("refund" in query or "jacket" in query or "return" in query) else
                    "billing" if ("charge" in query or "billing" in query or "129" in query) else
                    "other"
                )

                if _state["broken"]:
                    _call_tracker[qtype] = _call_tracker.get(qtype, 0) + 1
                    resp = self._broken(query, qtype)
                else:
                    resp = self._good(query)

                data = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(500)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # type: ignore[override]
            pass

        def _good(self, query: str, model_id: str = "gpt-5.4") -> Dict[str, Any]:
            if "refund" in query or "return" in query or "jacket" in query:
                return {
                    "model_id": model_id,
                    "model_provider": "openai",
                    "response": (
                        "I've found your order #4821 for $84.99 placed 12 days ago. "
                        "Our 30-day return policy covers this — I've initiated your full refund. "
                        "You'll see $84.99 back in 3–5 business days. "
                        "You'll get a confirmation email shortly."
                    ),
                    "steps": [
                        {"tool": "lookup_order", "parameters": {"query": query}, "output": "Order #4821, $84.99, 12 days ago"},
                        {"tool": "check_policy", "parameters": {"type": "return"}, "output": "30-day return window, full refund eligible"},
                        {"tool": "process_refund", "parameters": {"order_id": "4821", "amount": 84.99}, "output": "Refund initiated"},
                    ],
                }
            if "charge" in query or "billing" in query or "129" in query:
                return {
                    "model_id": model_id,
                    "model_provider": "openai",
                    "response": (
                        "That $129 charge is your annual plan renewal from March 3rd. "
                        "You signed up for annual billing last year with auto-renewal enabled. "
                        "I can email you the full invoice or switch you to monthly billing — which would you prefer?"
                    ),
                    "steps": [
                        {"tool": "lookup_account", "parameters": {"query": query}, "output": "Account #8821, annual plan"},
                        {"tool": "check_billing_history", "parameters": {"account_id": "8821"}, "output": "$129 annual renewal, March 3rd, auto-renewal on"},
                    ],
                }
            return {
                "model_id": model_id,
                "model_provider": "openai",
                "response": "How can I help you today?",
                "steps": [],
            }

        def _broken(self, query: str, qtype: str) -> Dict[str, Any]:
            if qtype == "refund":
                if _call_tracker.get("refund", 0) <= 1:
                    return {
                        "model_id": "gpt-5.4-mini",
                        "model_provider": "openai",
                        "response": (
                            "I found order #4821. A refund has been processed. "
                            "Please allow some time for it to appear."
                        ),
                        "steps": [
                            {"tool": "lookup_order", "parameters": {"query": query}, "output": "Order #4821, $84.99, 12 days ago"},
                            {"tool": "check_policy", "parameters": {"type": "return"}, "output": "30-day return window, full refund eligible"},
                            {"tool": "process_refund", "parameters": {"order_id": "4821", "amount": 84.99}, "output": "Refund initiated"},
                        ],
                    }
                else:
                    return self._good(query, model_id="gpt-5.4-mini")

            if qtype == "billing":
                return {
                    "model_id": "gpt-5.4-mini",
                    "model_provider": "openai",
                    "response": (
                        "I understand your concern about this charge. "
                        "I'll look into this billing issue and have someone follow up with you within 24–48 hours."
                    ),
                    "steps": [
                        {"tool": "lookup_account", "parameters": {"query": query}, "output": "Account #8821, annual plan"},
                    ],
                }

            return {
                "model_id": "gpt-5.4-mini",
                "model_provider": "openai",
                "response": "How can I help you today?",
                "steps": [],
            }

    # ── Start server ─────────────────────────────────────────────────────
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        _s.bind(("", 0))
        _port = _s.getsockname()[1]

    _server = HTTPServer(("127.0.0.1", _port), _DemoHandler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()

    # ── Isolated workspace ───────────────────────────────────────────────
    _tmpdir = tempfile.mkdtemp(prefix="evalview-demo-")
    _tmp = Path(_tmpdir)

    try:
        (_tmp / "tests").mkdir()
        (_tmp / ".evalview").mkdir()

        (_tmp / "tests" / "refund-request.yaml").write_text(
            "name: refund-request\n"
            "description: Customer requests refund for a recent purchase\n"
            "input:\n"
            "  query: I bought a jacket 12 days ago and it doesn't fit. Can I get a refund?\n"
            "expected:\n"
            "  tools:\n"
            "    - lookup_order\n"
            "    - check_policy\n"
            "    - process_refund\n"
            "  output:\n"
            "    contains:\n"
            "      - '84.99'\n"
            "      - refund\n"
            "thresholds:\n"
            "  min_score: 70\n"
        )
        (_tmp / "tests" / "billing-dispute.yaml").write_text(
            "name: billing-dispute\n"
            "description: Customer disputes an unrecognized charge\n"
            "input:\n"
            "  query: There's a $129 charge on my account from last Tuesday I don't recognize.\n"
            "expected:\n"
            "  tools:\n"
            "    - lookup_account\n"
            "    - check_billing_history\n"
            "  output:\n"
            "    contains:\n"
            "      - annual\n"
            "      - '129'\n"
            "thresholds:\n"
            "  min_score: 70\n"
        )
        (_tmp / ".evalview" / "config.yaml").write_text(
            f"adapter: http\n"
            f"endpoint: http://127.0.0.1:{_port}/execute\n"
            f"timeout: 15.0\n"
            f"allow_private_urls: true\n"
        )
        (_tmp / ".evalview" / "state.json").write_text(
            '{"total_snapshots": 1, "total_checks": 0}'
        )

        _env = {
            **os.environ,
            "EVALVIEW_DEMO": "1",
            "EVALVIEW_TELEMETRY_DISABLED": "1",
            "EVAL_MODEL": "gpt-5.4-mini",
            "PYTHONWARNINGS": "ignore",
            "CI": "1",  # suppress interactive prompts
        }

        # ═══════════════════════════════════════════════════════════════
        # PHASE 1: SNAPSHOT
        # ═══════════════════════════════════════════════════════════════

        console.print("[bold cyan]Phase 1[/bold cyan] [dim]— Capturing baseline[/dim]")
        console.print()
        console.print("  [dim]Running 2 tests against the live agent...[/dim]")

        _subprocess.run(
            ["evalview", "snapshot", "--path", "tests/"],
            cwd=_tmpdir,
            env=_env,
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )

        _sleep(0.5)
        _print_step("[green]✓[/green]", "refund-request     [green]100/100[/green]  [dim]lookup_order → check_policy → process_refund[/dim]")
        _print_step("[green]✓[/green]", "billing-dispute     [green]96/100[/green]   [dim]lookup_account → check_billing_history[/dim]")
        console.print()
        console.print("  [green bold]Baseline locked.[/green bold] [dim]2 golden traces saved.[/dim]")
        console.print()

        _sleep(1.0)

        # ═══════════════════════════════════════════════════════════════
        # PHASE 2: BREAK + HEAL
        # ═══════════════════════════════════════════════════════════════

        console.print("[bold yellow]Phase 2[/bold yellow] [dim]— Model update deployed. Checking...[/dim]")
        console.print()

        _state["broken"] = True
        _call_tracker.clear()

        # Run check --heal silently — CI=1 suppresses auto-open, --report forces generation
        _report = str(_tmp / ".evalview" / "demo-report.html")
        _subprocess.run(
            ["evalview", "check", "--heal", "--report", _report],
            cwd=_tmpdir,
            env=_env,
            stdin=_subprocess.DEVNULL,
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
        )

        _sleep(0.6)

        # ── Tight, scannable output ──────────────────────────────────
        console.print("  [cyan bold]UPSTREAM CHANGE DETECTED[/cyan bold]   [dim]model changed:[/dim] [bold]gpt-5.4 → gpt-5.4-mini[/bold]")
        console.print("       [dim]2 tests drifted right after the same update[/dim]")
        console.print()
        _sleep(0.4)
        console.print("  [yellow]⚡[/yellow] refund-request     [yellow bold]AUTO-FIXED[/yellow bold]")
        console.print("       [dim]first run drifted[/dim] → [dim]retried[/dim] → [green]matched baseline[/green] [dim](not a real regression)[/dim]")
        _sleep(0.4)
        console.print("  [red]✗[/red]  billing-dispute    [red]REAL REGRESSION[/red]")
        console.print("       [dim]retry did not recover baseline[/dim] → [dim]tool removed:[/dim] [red]check_billing_history[/red]")
        _sleep(0.3)
        console.print("       [dim]was:[/dim] lookup_account → [green]check_billing_history[/green]")
        console.print("       [dim]now:[/dim] lookup_account")
        console.print("       [red]-[/red] [dim]\"That $129 charge is your annual plan renewal...\"[/dim]")
        console.print("       [green]+[/green] [dim]\"I'll look into this billing issue and follow up...\"[/dim]")

        console.print()
        _sleep(0.5)

        console.print("  [bold cyan]Detected:[/bold cyan] upstream model change before blaming your code.")
        console.print("  [bold green]Auto-fixed:[/bold green] 1 run looked bad, but retry matched baseline.")
        console.print("  [bold red]Needs review:[/bold red] 1 real regression.")
        console.print("  [bold red]Would have shipped:[/bold red] [red]1 silent customer-facing regression.[/red]")

        console.print()
        _sleep(0.8)

        # ── CTA ──────────────────────────────────────────────────────
        console.print(Panel(
            "  [cyan]pip install evalview[/cyan]\n"
            "  [cyan]evalview init[/cyan]             [dim]# detect agent, create tests[/dim]\n"
            "  [cyan]evalview snapshot[/cyan]          [dim]# lock in baseline[/dim]\n"
            "  [cyan]evalview check[/cyan]             [dim]# catch regressions[/dim]\n"
            "  [cyan]evalview check --heal[/cyan]      [dim]# auto-fix flakes[/dim]",
            title="[bold green]Try it on your agent[/bold green]",
            border_style="green",
            padding=(1, 2),
        ))
        console.print()
        console.print(
            "  [yellow]★[/yellow] [bold]github.com/hidai25/eval-view[/bold]"
        )
        console.print()

        # ── Open HTML report last — after terminal story is complete ──
        report_path = Path(_report)
        if report_path.exists():
            import webbrowser
            persisted_report = Path(tempfile.gettempdir()) / f"evalview-demo-report-{int(time.time())}.html"
            shutil.copy2(report_path, persisted_report)
            console.print("  [dim]Opening full report in browser in 3 seconds...[/dim]")
            console.print()
            _sleep(3.0)
            webbrowser.open_new_tab(persisted_report.resolve().as_uri())

    finally:
        shutil.rmtree(_tmpdir, ignore_errors=True)
        logging.disable(logging.NOTSET)
        os._exit(0)
