"""Production wiring for Claude subagents via headless ``claude -p``.

The trading-team Lead is deterministic Python (`execution.lead_bootstrap`).
Subagents (scanner-reviewer, trading-agent, risk-execution) run as fresh
Claude sessions per cycle: this module spawns one subprocess per call, hands
over the agent's system prompt + the task payload as JSON, and parses the
JSON response back into the corresponding Pydantic output model.

The output contract is enforced via a JSON-schema reminder appended to the
system prompt. Failures (missing CLI, timeout, non-zero exit, bad JSON,
schema mismatch) all surface as ``SubagentError`` so the Lead can fail the
cycle cleanly. API-side budget exhaustion (out-of-credits, rate-limit,
insufficient quota) maps to the dedicated ``SubagentBudgetError`` subclass
so the Lead can abort the cycle without persisting partial decisions.

Each call also records a row in ``subagent_runs`` (Phase 6c). The persist
is best-effort: if the audit DB hiccups, the subagent's parsed output is
still returned to the Lead unchanged. The runner propagates ``CYCLE_ID``,
``AGENT_NAME``, ``RUN_ID`` and ``CORRELATION_ID`` to the subprocess
environment so the MCP web_search tool can emit linked rows in
``web_search_calls``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy import text as sql_text

from shared.db import get_session
from shared.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

DEFAULT_CLAUDE_BIN: Final = "claude"
ENVELOPE_RESULT_KEY: Final = "result"
ENVELOPE_IS_ERROR_KEY: Final = "is_error"
ENVELOPE_API_ERROR_STATUS_KEY: Final = "api_error_status"
ENVELOPE_COST_KEY: Final = "total_cost_usd"
ENVELOPE_USAGE_KEY: Final = "usage"
STDERR_TAIL_CHARS: Final = 500
RAW_STDOUT_MAX_CHARS: Final = 256 * 1024  # 256 KiB cap on persisted stdout.

# api_error_status values that mean "budget exhausted, do not retry blindly".
# Anthropic surfaces these strings via the Claude Code CLI envelope.
_BUDGET_API_ERROR_STATUSES: Final = frozenset(
    {
        "insufficient_quota",
        "rate_limit_error",
        "credit_balance_too_low",
        "billing_error",
        "overloaded_error",
    },
)

# stderr substrings that indicate a budget/quota failure when the CLI exits
# non-zero. Lower-cased before matching. Conservative on purpose: only
# patterns that unambiguously mean "the API refused due to billing/quota".
_BUDGET_STDERR_PATTERNS: Final = (
    "credit balance",
    "credit_balance_too_low",
    "insufficient_quota",
    "insufficient quota",
    "rate_limit",
    "rate limit",
    "billing",
    "402",
    "429",
)

_INSERT_SUBAGENT_RUN: Final = sql_text(
    """
    INSERT INTO subagent_runs (
        cycle_id, agent_name, run_id, correlation_id, prompt_sha,
        started_at, finished_at, latency_ms, cost_usd, usage,
        task_payload, raw_stdout, envelope, error, error_class
    ) VALUES (
        :cycle_id, :agent_name, :run_id, :correlation_id, :prompt_sha,
        :started_at, :finished_at, :latency_ms, :cost_usd,
        CAST(:usage AS jsonb),
        CAST(:task_payload AS jsonb), :raw_stdout,
        CAST(:envelope AS jsonb), :error, :error_class
    )
    """
)


class SubagentError(RuntimeError):
    """Raised when a subagent call fails (CLI missing, timeout, bad output)."""


class SubagentBudgetError(SubagentError):
    """Raised when the API call was refused for billing/quota/rate-limit reasons.

    Distinct from ``SubagentError`` so the Lead can abort the cycle gracefully
    without persisting partial decisions or trades.
    """


def run_subagent[T: BaseModel](
    *,
    agent_md_path: Path,
    task: BaseModel,
    output_model: type[T],
    timeout_s: int,
    cycle_id: str,
    correlation_id: str | None = None,
    doctrine_path: Path | None = None,
    mcp_config_path: Path | None = None,
    allowed_mcp_tools: tuple[str, ...] = (),
    claude_bin: str = DEFAULT_CLAUDE_BIN,
    max_budget_usd: float | None = None,
) -> T:
    """Invoke a Claude subagent in headless mode and return a validated output.

    ``agent_md_path`` is the canonical agent skeleton (e.g.
    ``.claude/agents/scanner-reviewer.md``). ``doctrine_path`` is an optional
    follow-on prompt file from ``research/prompts/`` that deepens the agent's
    strategy doctrine; concatenated after the skeleton body.

    ``mcp_config_path`` and ``allowed_mcp_tools`` wire MCP servers into the
    subprocess (Phase 6b PR 2). Only the trading-agent gets the
    ``research`` MCP server today; scanner-reviewer and risk-execution are
    deterministic and pass these as ``None``/``()``.

    ``max_budget_usd`` (when set) is forwarded to ``claude --max-budget-usd``,
    which causes the CLI to abort the call once the in-flight cost would
    exceed the cap. Acts as a hard upper bound on per-agent spend.

    ``cycle_id`` and ``correlation_id`` are recorded in ``subagent_runs``
    and propagated to the subprocess environment so the MCP web_search tool
    can join its rows back to this invocation via ``run_id``.
    """
    if not agent_md_path.exists():
        msg = f"agent skeleton not found: {agent_md_path}"
        raise SubagentError(msg)
    binary = shutil.which(claude_bin)
    if binary is None:
        msg = f"`{claude_bin}` not on PATH; install Claude Code CLI to run cron"
        raise SubagentError(msg)

    agent_name = agent_md_path.stem
    run_id = str(uuid.uuid4())
    system_prompt = _build_system_prompt(
        agent_md_path=agent_md_path,
        doctrine_path=doctrine_path,
        output_model=output_model,
    )
    prompt_sha = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    task_payload_json = task.model_dump_json()

    cmd = _build_cmd(
        binary=binary,
        system_prompt=system_prompt,
        task_payload_json=task_payload_json,
        mcp_config_path=mcp_config_path,
        allowed_mcp_tools=allowed_mcp_tools,
        max_budget_usd=max_budget_usd,
    )
    env = _build_subprocess_env(
        cycle_id=cycle_id,
        agent_name=agent_name,
        run_id=run_id,
        correlation_id=correlation_id,
    )

    logger.info("subagent_invoking", agent=agent_name, run_id=run_id, timeout_s=timeout_s)
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()

    state: _RunState = _RunState()
    try:
        validated = _invoke_and_parse(
            cmd=cmd,
            env=env,
            timeout_s=timeout_s,
            agent_name=agent_name,
            run_id=run_id,
            output_model=output_model,
            state=state,
        )
        state.latency_ms = state.latency_ms or int((time.monotonic() - started_monotonic) * 1000)
        logger.info(
            "subagent_completed",
            agent=agent_name,
            run_id=run_id,
            latency_ms=state.latency_ms,
            cost_usd=state.cost_usd,
            usage=state.usage,
        )
        return validated
    finally:
        if state.latency_ms is None:
            state.latency_ms = int((time.monotonic() - started_monotonic) * 1000)
        _persist_subagent_run(
            cycle_id=cycle_id,
            agent_name=agent_name,
            run_id=run_id,
            correlation_id=correlation_id,
            prompt_sha=prompt_sha,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            latency_ms=state.latency_ms,
            cost_usd=state.cost_usd,
            usage=state.usage,
            task_payload=task_payload_json,
            raw_stdout=state.raw_stdout,
            envelope=state.envelope,
            error=state.error,
            error_class=state.error_class,
        )


class _RunState:
    """Mutable scratchpad collected during a subagent invocation.

    Populated incrementally as we go through subprocess → returncode check
    → JSON extraction → Pydantic validation, so the ``finally`` block in
    :func:`run_subagent` can persist a single ``subagent_runs`` row no
    matter where the call exits.
    """

    __slots__ = (
        "cost_usd",
        "envelope",
        "error",
        "error_class",
        "latency_ms",
        "raw_stdout",
        "usage",
    )

    def __init__(self) -> None:
        self.raw_stdout: str | None = None
        self.envelope: dict[str, Any] | None = None
        self.cost_usd: float | None = None
        self.usage: dict[str, Any] | None = None
        self.error: str | None = None
        self.error_class: str | None = None
        self.latency_ms: int | None = None


def _invoke_and_parse[T: BaseModel](
    *,
    cmd: list[str],
    env: dict[str, str],
    timeout_s: int,
    agent_name: str,
    run_id: str,
    output_model: type[T],
    state: _RunState,
) -> T:
    """Run the subprocess and parse its output, mutating ``state`` along the way."""
    started_monotonic = time.monotonic()
    proc = _run_subprocess(
        cmd=cmd,
        env=env,
        timeout_s=timeout_s,
        agent_name=agent_name,
        run_id=run_id,
        state=state,
        started_monotonic=started_monotonic,
    )
    state.latency_ms = int((time.monotonic() - started_monotonic) * 1000)
    state.raw_stdout = _truncate(proc.stdout)

    if proc.returncode != 0:
        _raise_for_returncode(proc=proc, agent_name=agent_name, run_id=run_id, state=state)

    payload, envelope = _extract_payload(stdout=proc.stdout, agent_name=agent_name)
    state.envelope = envelope
    state.cost_usd = envelope.get(ENVELOPE_COST_KEY)
    state.usage = envelope.get(ENVELOPE_USAGE_KEY)

    return _validate_payload(
        payload=payload,
        output_model=output_model,
        agent_name=agent_name,
        run_id=run_id,
        state=state,
    )


def _run_subprocess(
    *,
    cmd: list[str],
    env: dict[str, str],
    timeout_s: int,
    agent_name: str,
    run_id: str,
    state: _RunState,
    started_monotonic: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 — argv is constructed locally, no shell.
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        state.latency_ms = int((time.monotonic() - started_monotonic) * 1000)
        state.error = f"subagent {agent_name} timed out after {timeout_s}s"
        state.error_class = "SubagentError"
        logger.warning("subagent_timeout", agent=agent_name, run_id=run_id, timeout_s=timeout_s)
        raise SubagentError(state.error) from exc


def _raise_for_returncode(
    *,
    proc: subprocess.CompletedProcess[str],
    agent_name: str,
    run_id: str,
    state: _RunState,
) -> None:
    tail = (proc.stderr or "")[-STDERR_TAIL_CHARS:]
    state.error = f"subagent {agent_name} exited {proc.returncode}: {tail}"
    if _stderr_indicates_budget_exhaustion(tail):
        state.error_class = "SubagentBudgetError"
        logger.warning("subagent_budget_exhausted", agent=agent_name, run_id=run_id, code=proc.returncode)
        raise SubagentBudgetError(state.error)
    state.error_class = "SubagentError"
    logger.warning("subagent_nonzero_exit", agent=agent_name, run_id=run_id, code=proc.returncode)
    raise SubagentError(state.error)


def _validate_payload[T: BaseModel](
    *,
    payload: str,
    output_model: type[T],
    agent_name: str,
    run_id: str,
    state: _RunState,
) -> T:
    try:
        return output_model.model_validate_json(payload)
    except ValidationError as exc:
        logger.warning("subagent_schema_mismatch", agent=agent_name, run_id=run_id)
        state.error = f"subagent {agent_name} output failed validation: {exc}"
        state.error_class = "SubagentError"
        raise SubagentError(state.error) from exc


def _stderr_indicates_budget_exhaustion(stderr_tail: str) -> bool:
    lowered = stderr_tail.lower()
    return any(pat in lowered for pat in _BUDGET_STDERR_PATTERNS)


def _build_subprocess_env(
    *,
    cycle_id: str,
    agent_name: str,
    run_id: str,
    correlation_id: str | None,
) -> dict[str, str]:
    """Subprocess env with audit-trail keys for the MCP web_search tool.

    The MCP server (research.skills.web_search) reads these to populate the
    matching ``web_search_calls`` row, which joins back to ``subagent_runs``
    via the loose ``run_id`` key.
    """
    return {
        **os.environ,
        "CYCLE_ID": cycle_id,
        "AGENT_NAME": agent_name,
        "RUN_ID": run_id,
        "CORRELATION_ID": correlation_id or "",
    }


def _build_cmd(
    *,
    binary: str,
    system_prompt: str,
    task_payload_json: str,
    mcp_config_path: Path | None,
    allowed_mcp_tools: tuple[str, ...],
    max_budget_usd: float | None,
) -> list[str]:
    cmd = [
        binary,
        "-p",
        task_payload_json,
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "json",
    ]
    if mcp_config_path is not None:
        if not mcp_config_path.exists():
            msg = f"mcp config not found: {mcp_config_path}"
            raise SubagentError(msg)
        cmd.extend(["--mcp-config", str(mcp_config_path)])
        if allowed_mcp_tools:
            cmd.extend(["--allowed-tools", ",".join(allowed_mcp_tools)])
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    return cmd


def _build_system_prompt(
    *,
    agent_md_path: Path,
    doctrine_path: Path | None,
    output_model: type[BaseModel],
) -> str:
    body = _strip_yaml_frontmatter(agent_md_path.read_text(encoding="utf-8"))
    if doctrine_path is not None and doctrine_path.exists():
        body += "\n\n" + doctrine_path.read_text(encoding="utf-8")
    schema = json.dumps(output_model.model_json_schema(), indent=2)
    return (
        f"{body}\n\n# Output format\n"
        "Respond with a single JSON object (no Markdown fences, no prose) that validates "
        f"against the schema below.\n\n```json\n{schema}\n```"
    )


def _strip_yaml_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    if len(parts) < 3:  # noqa: PLR2004 — three parts: empty, frontmatter, body.
        return content
    return parts[2].lstrip()


def _truncate(content: str | None) -> str | None:
    if content is None:
        return None
    if len(content) <= RAW_STDOUT_MAX_CHARS:
        return content
    truncated = content[:RAW_STDOUT_MAX_CHARS]
    return f"{truncated}\n…[truncated at {RAW_STDOUT_MAX_CHARS} chars]"


def _persist_subagent_run(
    *,
    cycle_id: str,
    agent_name: str,
    run_id: str,
    correlation_id: str | None,
    prompt_sha: str,
    started_at: datetime,
    finished_at: datetime,
    latency_ms: int | None,
    cost_usd: float | None,
    usage: dict[str, Any] | None,
    task_payload: str,
    raw_stdout: str | None,
    envelope: dict[str, Any] | None,
    error: str | None,
    error_class: str | None,
) -> None:
    """Best-effort INSERT into ``subagent_runs``.

    Audit failures must never poison a healthy cycle. On any DB error we log
    ``audit_persist_failed`` and return; the caller continues unchanged.
    """
    try:
        with get_session("trading_cycle") as session:
            session.execute(
                _INSERT_SUBAGENT_RUN,
                {
                    "cycle_id": cycle_id,
                    "agent_name": agent_name,
                    "run_id": run_id,
                    "correlation_id": correlation_id or None,
                    "prompt_sha": prompt_sha,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "latency_ms": latency_ms,
                    "cost_usd": cost_usd,
                    "usage": json.dumps(usage) if usage is not None else None,
                    "task_payload": task_payload,
                    "raw_stdout": raw_stdout,
                    "envelope": json.dumps(envelope) if envelope is not None else None,
                    "error": error,
                    "error_class": error_class,
                },
            )
    except Exception:
        logger.warning(
            "audit_persist_failed",
            agent=agent_name,
            run_id=run_id,
            cycle_id=cycle_id,
            exc_info=True,
        )


def _extract_payload(*, stdout: str, agent_name: str) -> tuple[str, dict[str, Any]]:
    """Pull the assistant's text out of the headless ``--output-format json`` envelope.

    Returns ``(result_text, envelope_dict)`` so the caller can also surface
    cost/usage telemetry from the same payload. Raises ``SubagentBudgetError``
    when the envelope reports a billing/quota/rate-limit ``api_error_status``,
    otherwise ``SubagentError`` for malformed/error envelopes.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        msg = f"subagent {agent_name} returned non-JSON envelope: {exc}"
        raise SubagentError(msg) from exc
    if not isinstance(envelope, dict):
        msg = f"subagent {agent_name} envelope was not an object"
        raise SubagentError(msg)
    if envelope.get(ENVELOPE_IS_ERROR_KEY) is True:
        status = str(envelope.get(ENVELOPE_API_ERROR_STATUS_KEY) or "").lower()
        msg = f"subagent {agent_name} envelope reports api_error_status={status!r}"
        if status in _BUDGET_API_ERROR_STATUSES:
            raise SubagentBudgetError(msg)
        raise SubagentError(msg)
    raw = envelope.get(ENVELOPE_RESULT_KEY)
    if not isinstance(raw, str):
        msg = f"subagent {agent_name} envelope missing 'result' string"
        raise SubagentError(msg)
    return _strip_markdown_fences(raw), envelope


def _strip_markdown_fences(raw: str) -> str:
    """Best-effort: extract the JSON object from a Markdown-laced response.

    The ``--append-system-prompt`` reminder asks the agent for a single JSON
    object with no prose, but compliance is not always perfect — agents
    sometimes wrap the JSON in ```json``` fences or prefix it with a few
    paragraphs of narration. This handles both shapes:

    1. Strip surrounding ``` fences if present.
    2. Otherwise, locate the first ``{`` and use ``json.JSONDecoder.raw_decode``
       to find the longest valid JSON object starting there. Anything before
       or after that object is discarded.

    Returns the original text unchanged if neither shape matches; the caller
    will then surface the Pydantic ``ValidationError`` with the raw input.
    """
    content = raw.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        content = content.removesuffix("```")
        content = content.strip()
    start = content.find("{")
    if start < 0:
        return content
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(content[start:])
    except json.JSONDecodeError:
        return content
    return content[start : start + end]
