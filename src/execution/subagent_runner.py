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
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ValidationError

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
    """
    if not agent_md_path.exists():
        msg = f"agent skeleton not found: {agent_md_path}"
        raise SubagentError(msg)
    binary = shutil.which(claude_bin)
    if binary is None:
        msg = f"`{claude_bin}` not on PATH; install Claude Code CLI to run cron"
        raise SubagentError(msg)

    cmd = _build_cmd(
        binary=binary,
        agent_md_path=agent_md_path,
        doctrine_path=doctrine_path,
        output_model=output_model,
        task=task,
        mcp_config_path=mcp_config_path,
        allowed_mcp_tools=allowed_mcp_tools,
        max_budget_usd=max_budget_usd,
    )

    agent_name = agent_md_path.stem
    logger.info("subagent_invoking", agent=agent_name, timeout_s=timeout_s)
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv is constructed locally, no shell.
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"subagent {agent_name} timed out after {timeout_s}s"
        logger.warning("subagent_timeout", agent=agent_name, timeout_s=timeout_s)
        raise SubagentError(msg) from exc
    latency_ms = int((time.monotonic() - started) * 1000)

    if proc.returncode != 0:
        tail = (proc.stderr or "")[-STDERR_TAIL_CHARS:]
        msg = f"subagent {agent_name} exited {proc.returncode}: {tail}"
        if _stderr_indicates_budget_exhaustion(tail):
            logger.warning("subagent_budget_exhausted", agent=agent_name, code=proc.returncode)
            raise SubagentBudgetError(msg)
        logger.warning("subagent_nonzero_exit", agent=agent_name, code=proc.returncode)
        raise SubagentError(msg)

    payload, envelope = _extract_payload(stdout=proc.stdout, agent_name=agent_name)
    try:
        validated = output_model.model_validate_json(payload)
    except ValidationError as exc:
        logger.warning("subagent_schema_mismatch", agent=agent_name)
        msg = f"subagent {agent_name} output failed validation: {exc}"
        raise SubagentError(msg) from exc

    logger.info(
        "subagent_completed",
        agent=agent_name,
        latency_ms=latency_ms,
        cost_usd=envelope.get(ENVELOPE_COST_KEY),
        usage=envelope.get(ENVELOPE_USAGE_KEY),
    )
    return validated


def _stderr_indicates_budget_exhaustion(stderr_tail: str) -> bool:
    lowered = stderr_tail.lower()
    return any(pat in lowered for pat in _BUDGET_STDERR_PATTERNS)


def _build_cmd(
    *,
    binary: str,
    agent_md_path: Path,
    doctrine_path: Path | None,
    output_model: type[BaseModel],
    task: BaseModel,
    mcp_config_path: Path | None,
    allowed_mcp_tools: tuple[str, ...],
    max_budget_usd: float | None,
) -> list[str]:
    system_prompt = _build_system_prompt(
        agent_md_path=agent_md_path,
        doctrine_path=doctrine_path,
        output_model=output_model,
    )
    cmd = [
        binary,
        "-p",
        task.model_dump_json(),
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


def _strip_yaml_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:  # noqa: PLR2004 — three parts: empty, frontmatter, body.
        return text
    return parts[2].lstrip()


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
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.removesuffix("```")
        text = text.strip()
    start = text.find("{")
    if start < 0:
        return text
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        return text
    return text[start : start + end]
