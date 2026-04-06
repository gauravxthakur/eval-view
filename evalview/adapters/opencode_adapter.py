"""OpenCode CLI adapter for EvalView.

OpenCode (https://github.com/sst/opencode) is an open-source terminal AI coding
agent supporting 75+ LLM providers. This adapter runs OpenCode non-interactively
and parses its JSON event stream into an ExecutionTrace.

Requirements:
    - OpenCode installed: npm install -g opencode-ai
    - Model registered in ~/.config/opencode/opencode.json

Usage in YAML test cases:
    adapter: opencode
    adapter_config:
      model: ollama/gemma4:e4b   # or ollama/qwen3.5:9b, claude-sonnet-4-6, etc.

    input:
      query: "Fix the bug in buggy.py"
      context:
        cwd: "demo/fixtures"
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from evalview.adapters.base import AgentAdapter
from evalview.core.types import (
    ExecutionMetrics,
    ExecutionTrace,
    SpanKind,
    StepMetrics,
    StepTrace,
    TokenUsage,
)
from evalview.core.tracing import Tracer

logger = logging.getLogger(__name__)

# Map OpenCode tool names to canonical EvalView tool names
_TOOL_NAME_MAP = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "glob": "glob",
    "bash": "bash",
    "grep": "grep",
    "ls": "list_directory",
    "patch": "patch_file",
}


class OpenCodeAdapter(AgentAdapter):
    """Adapter for the OpenCode AI coding agent.

    Runs ``opencode run`` non-interactively with ``--format json`` to capture
    a structured NDJSON event stream, then parses it into an ExecutionTrace.

    The model is swappable per test, making this adapter the right tool for
    multi-model comparisons: same tasks, same agent shell, different backbone.
    """

    def __init__(
        self,
        endpoint: str = "",  # Not used for CLI, kept for registry compatibility
        timeout: float = 300.0,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        opencode_path: str = "opencode",
        **kwargs: Any,
    ):
        """Initialise OpenCode adapter.

        Args:
            endpoint: Unused (OpenCode runs locally).
            timeout: Max execution time in seconds (default 300).
            model: Model string in OpenCode format, e.g. ``ollama/gemma4:e4b``
                   or ``claude-sonnet-4-6``.
            cwd: Working directory. OpenCode can only access files under here.
            opencode_path: Path to the opencode binary (default: ``opencode``).
        """
        self.timeout = timeout
        self.model = model
        self.cwd = cwd
        self.opencode_path = opencode_path
        self._last_raw_output: Optional[str] = None

    @property
    def name(self) -> str:
        return "opencode"

    async def execute(
        self, query: str, context: Optional[Dict[str, Any]] = None
    ) -> ExecutionTrace:
        """Run a task with OpenCode and return a parsed ExecutionTrace.

        Args:
            query: Natural-language task description.
            context: Optional dict with:
                - ``cwd``: Override working directory.
                - ``model``: Override model string.
                - ``files``: List of files to attach (``-f`` flags).

        Returns:
            ExecutionTrace with tool call steps, final output, and metrics.
        """
        context = context or {}

        model = context.get("model", self.model)
        if not model:
            raise ValueError(
                "OpenCodeAdapter requires a model. Set it in adapter_config.model "
                "or pass it via context.model. Example: ollama/gemma4:e4b"
            )

        cwd = context.get("cwd", self.cwd)
        if cwd:
            cwd = os.path.abspath(os.path.expanduser(cwd))

        cmd = self._build_command(query, model, context, cwd=cwd)
        logger.info("OpenCode command: %s (cwd=%s)", " ".join(cmd), cwd)

        start_time = datetime.now()
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    timeout=self.timeout,
                    env=self._build_env(),
                ),
            )
            end_time = datetime.now()
            self._last_raw_output = result.stdout

            if result.stderr:
                logger.debug("OpenCode stderr: %s", result.stderr)

            return self._parse_ndjson(result.stdout, result.returncode, start_time, end_time)

        except subprocess.TimeoutExpired:
            end_time = datetime.now()
            return self._error_trace(
                f"OpenCode timed out after {self.timeout}s", start_time, end_time
            )
        except FileNotFoundError:
            end_time = datetime.now()
            return self._error_trace(
                "OpenCode not found. Install with: npm install -g opencode-ai",
                start_time,
                end_time,
            )
        except Exception as exc:
            end_time = datetime.now()
            return self._error_trace(str(exc), start_time, end_time)

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _build_command(
        self, query: str, model: str, context: Dict[str, Any], cwd: Optional[str] = None
    ) -> List[str]:
        cmd = [
            self.opencode_path,
            "run",
            query,
            "--model", model,
            "--format", "json",
        ]
        if cwd:
            cmd.extend(["--dir", cwd])
        for f in context.get("files", []):
            cmd.extend(["-f", f])
        return cmd

    # ------------------------------------------------------------------
    # Environment
    # ------------------------------------------------------------------

    def _build_env(self) -> Dict[str, str]:
        """Build subprocess environment, merging .env.local if present."""
        env = os.environ.copy()
        for candidate in [
            Path(".env.local"),
            Path(".env"),
        ]:
            if candidate.exists():
                with open(candidate) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            env.setdefault(k.strip(), v.strip())
                break
        return env

    # ------------------------------------------------------------------
    # NDJSON parsing
    # ------------------------------------------------------------------

    def _parse_ndjson(
        self,
        stdout: str,
        returncode: int,
        start_time: datetime,
        end_time: datetime,
    ) -> ExecutionTrace:
        """Parse OpenCode's NDJSON event stream into an ExecutionTrace.

        OpenCode emits one JSON object per line (NDJSON).  Relevant event types:

        * ``tool_use``   — a tool was invoked (glob, read, edit, bash, …)
        * ``text``       — the assistant's final response text
        * ``step_finish`` — end of a reasoning step; carries cumulative token counts
        * ``error``      — a fatal error occurred
        """
        steps: List[StepTrace] = []
        final_output = ""
        session_id = f"opencode-{uuid.uuid4().hex[:8]}"
        total_tokens: Optional[TokenUsage] = None
        total_cost = 0.0
        fatal_error: Optional[str] = None

        # Strip ANSI codes that may appear on the same line as JSON
        ansi = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

        for line in stdout.splitlines():
            line = ansi.sub("", line).strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type")
            session_id = event.get("sessionID", session_id)
            part = event.get("part", {})

            if event_type == "tool_use":
                step = self._parse_tool_use(part, len(steps))
                if step:
                    steps.append(step)

            elif event_type == "text":
                text = part.get("text", "")
                if text:
                    final_output = text  # Last text wins

            elif event_type == "step_finish":
                tokens = part.get("tokens", {})
                if tokens and part.get("reason") == "stop":
                    total_tokens = TokenUsage(
                        input_tokens=tokens.get("input", 0),
                        output_tokens=tokens.get("output", 0),
                        cached_tokens=tokens.get("cache", {}).get("read", 0),
                    )
                step_cost = part.get("cost", 0.0) or 0.0
                total_cost += step_cost

            elif event_type == "error":
                err = event.get("error", {})
                fatal_error = err.get("data", {}).get("message") or err.get("name", "unknown error")

        if fatal_error and not final_output:
            final_output = fatal_error

        if returncode != 0 and not final_output:
            final_output = f"OpenCode exited with code {returncode}"

        tracer = Tracer()
        with tracer.start_span("OpenCode Execution", SpanKind.AGENT):
            for step in steps:
                tracer.record_tool_call(
                    tool_name=step.tool_name,
                    parameters=step.parameters,
                    result=step.output,
                    error=step.error,
                    duration_ms=step.metrics.latency if step.metrics else 0.0,
                )

        return ExecutionTrace(
            session_id=session_id,
            start_time=start_time,
            end_time=end_time,
            steps=steps,
            final_output=final_output,
            metrics=ExecutionMetrics(
                total_cost=total_cost,
                total_latency=(end_time - start_time).total_seconds() * 1000,
                total_tokens=total_tokens,
            ),
            trace_context=tracer.build_trace_context(),
        )

    def _parse_tool_use(self, part: Dict[str, Any], index: int) -> Optional[StepTrace]:
        """Convert a ``tool_use`` part into a StepTrace."""
        raw_tool = part.get("tool", "unknown")
        tool_name = _TOOL_NAME_MAP.get(raw_tool, raw_tool)
        call_id = part.get("callID", f"call-{index}")
        state = part.get("state", {})
        status = state.get("status", "completed")

        parameters = state.get("input", {})
        output = state.get("output", "")

        # For edit_file, embed the diff in the output — great for the demo
        metadata = state.get("metadata", {})
        if raw_tool == "edit" and metadata.get("diff"):
            output = metadata["diff"]

        error: Optional[str] = None
        success = status == "completed"
        if not success:
            error = state.get("error", f"Tool {tool_name} failed")

        # Per-tool latency from OpenCode's own timing
        timing = state.get("time", {})
        latency_ms = 0.0
        if timing.get("start") and timing.get("end"):
            latency_ms = float(timing["end"] - timing["start"])

        return StepTrace(
            step_id=call_id,
            step_name=f"{tool_name} ({index + 1})",
            tool_name=tool_name,
            parameters=parameters,
            output=str(output) if output else "",
            success=success,
            error=error,
            metrics=StepMetrics(latency=latency_ms, cost=0.0),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _error_trace(
        self, message: str, start_time: datetime, end_time: datetime
    ) -> ExecutionTrace:
        return ExecutionTrace(
            session_id=f"opencode-error-{uuid.uuid4().hex[:8]}",
            start_time=start_time,
            end_time=end_time,
            steps=[
                StepTrace(
                    step_id="error",
                    step_name="Error",
                    tool_name="error",
                    parameters={},
                    output=message,
                    success=False,
                    error=message,
                    metrics=StepMetrics(),
                )
            ],
            final_output=message,
            metrics=ExecutionMetrics(
                total_cost=0.0,
                total_latency=(end_time - start_time).total_seconds() * 1000,
                total_tokens=None,
            ),
        )

    async def health_check(self) -> bool:
        """Return True if the opencode binary is reachable."""
        try:
            result = subprocess.run(
                [self.opencode_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.warning("OpenCode health check failed: %s", exc)
            return False
