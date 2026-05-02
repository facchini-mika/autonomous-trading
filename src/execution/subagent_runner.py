"""Production wiring for Claude subagents via headless ``claude -p``.

The trading-team Lead is deterministic Python (`execution.lead_bootstrap`).
Subagents (scanner-reviewer, trading-agent, risk-execution) run as fresh
Claude sessions per cycle: this module spawns one subprocess per call, hands
over the agent's system prompt + the task payload as JSON, and parses the
JSON response back into the corresponding Pydantic output model.

The output contract is enforced via a JSON-schema reminder appended to the
system prompt. Failures (missing CLI, timeout, non-zero exit, bad JSON,
schema mismatch) all surface as ``SubagentError`` so the Lead can fail the
cycle cleanly.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ValidationError

from shared.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

DEFAULT_CLAUDE_BIN: Final = "claude"
ENVELOPE_RESULT_KEY: Final = "result"
STDERR_TAIL_CHARS: Final = 500


class SubagentError(RuntimeError):
    """Raised when a subagent call fails (CLI missing, timeout, bad output)."""


def run_subagent[T: BaseModel](
    *,
    agent_md_path: Path,
    task: BaseModel,
    output_model: type[T],
    timeout_s: int,
    doctrine_path: Path | None = None,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> T:
    """Invoke a Claude subagent in headless mode and return a validated output.

    ``agent_md_path`` is the canonical agent skeleton (e.g.
    ``.claude/agents/scanner-reviewer.md``). ``doctrine_path`` is an optional
    follow-on prompt file from ``research/prompts/`` that deepens the agent's
    strategy doctrine; concatenated after the skeleton body.
    """
    if not agent_md_path.exists():
        msg = f"agent skeleton not found: {agent_md_path}"
        raise SubagentError(msg)
    binary = shutil.which(claude_bin)
    if binary is None:
        msg = f"`{claude_bin}` not on PATH; install Claude Code CLI to run cron"
        raise SubagentError(msg)

    system_prompt = _build_system_prompt(
        agent_md_path=agent_md_path,
        doctrine_path=doctrine_path,
        output_model=output_model,
    )
    task_json = task.model_dump_json()
    cmd = [
        binary,
        "-p",
        task_json,
        "--append-system-prompt",
        system_prompt,
        "--output-format",
        "json",
    ]

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
        logger.warning("subagent_nonzero_exit", agent=agent_name, code=proc.returncode)
        raise SubagentError(msg)

    payload = _extract_payload(stdout=proc.stdout, agent_name=agent_name)
    try:
        validated = output_model.model_validate_json(payload)
    except ValidationError as exc:
        logger.warning("subagent_schema_mismatch", agent=agent_name)
        msg = f"subagent {agent_name} output failed validation: {exc}"
        raise SubagentError(msg) from exc

    logger.info("subagent_completed", agent=agent_name, latency_ms=latency_ms)
    return validated


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


def _extract_payload(*, stdout: str, agent_name: str) -> str:
    """Pull the assistant's text out of the headless ``--output-format json`` envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        msg = f"subagent {agent_name} returned non-JSON envelope: {exc}"
        raise SubagentError(msg) from exc
    if not isinstance(envelope, dict):
        msg = f"subagent {agent_name} envelope was not an object"
        raise SubagentError(msg)
    raw = envelope.get(ENVELOPE_RESULT_KEY)
    if not isinstance(raw, str):
        msg = f"subagent {agent_name} envelope missing 'result' string"
        raise SubagentError(msg)
    return _strip_markdown_fences(raw)


def _strip_markdown_fences(raw: str) -> str:
    """Best-effort: drop surrounding ```json``` fences if the model emitted them."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.removesuffix("```")
    return text.strip()
