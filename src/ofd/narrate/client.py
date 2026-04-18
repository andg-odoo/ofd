"""Narrate backends.

Abstracted so the tool is agnostic to whether the LLM call goes through
the Claude Code CLI (`claude -p`) or the Anthropic SDK. The CLI backend
is the default because it piggy-backs on an existing Max plan and needs
zero auth setup.
"""

from __future__ import annotations

import json
import subprocess
from typing import Protocol


class NarrateError(RuntimeError):
    pass


class NarrateBackend(Protocol):
    def narrate(self, system: str, user: str) -> str: ...


class ClaudeCodeCLIBackend:
    """Default backend: shells out to `claude -p --output-format json`.

    Combines the system prompt and the user prompt into a single `-p`
    string (the CLI does not have a separate system-prompt slot the way
    the API does). The harness of Claude Code itself adds its own
    system prompt on top; for a narrow narrate task that has negligible
    cost.
    """

    def __init__(self, binary: str = "claude", timeout: int = 120):
        self.binary = binary
        self.timeout = timeout

    def narrate(self, system: str, user: str) -> str:
        prompt = (
            f"<system-context>\n{system.strip()}\n</system-context>\n\n"
            f"{user.strip()}\n"
        )
        try:
            result = subprocess.run(
                [self.binary, "-p", "--output-format", "json", prompt],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except FileNotFoundError as e:
            raise NarrateError(
                f"`{self.binary}` not found; install Claude Code or switch "
                "narrate.backend to anthropic"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise NarrateError(
                f"`{self.binary} -p` timed out after {self.timeout}s"
            ) from e

        if result.returncode != 0:
            raise NarrateError(
                f"`{self.binary} -p` exited {result.returncode}: "
                f"{(result.stderr or '').strip()}"
            )

        return _extract_text_from_cc_json(result.stdout)


def _extract_text_from_cc_json(raw: str) -> str:
    """Claude Code --output-format json returns a JSON envelope whose
    exact shape has shifted across versions. Be lenient: accept any of
    the known keys that have held the assistant output text.
    """
    raw = raw.strip()
    if not raw:
        raise NarrateError("`claude -p` produced no output")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise NarrateError(f"could not parse claude -p output as JSON: {e}") from e

    for key in ("result", "text", "content", "output"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    messages = data.get("messages")
    if isinstance(messages, list) and messages:
        # Take the last message's content if it's a string or a list of blocks.
        last = messages[-1]
        content = last.get("content") if isinstance(last, dict) else None
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts = [
                block.get("text", "") for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "".join(texts).strip()
            if joined:
                return joined

    raise NarrateError(
        f"unexpected claude -p JSON shape; keys={list(data.keys())}"
    )


class AnthropicBackend:
    """Opt-in backend using the anthropic SDK. Requires ANTHROPIC_API_KEY
    and an explicit `pip install anthropic`. Prefer this when you want
    explicit prompt-cache control or parallel narrate runs."""

    def __init__(self, model: str, max_tokens: int = 512):
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise NarrateError(
                "anthropic package not installed; run `pip install ofd[anthropic]`"
            ) from e
        self.client = Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def narrate(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
            ],
            messages=[{"role": "user", "content": user}],
        )
        parts: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        combined = "".join(parts).strip()
        if not combined:
            raise NarrateError("Anthropic returned an empty content block")
        return combined


def build_backend(config_backend: str, model: str) -> NarrateBackend:
    if config_backend == "claude_code":
        return ClaudeCodeCLIBackend()
    if config_backend == "anthropic":
        return AnthropicBackend(model=model)
    raise NarrateError(f"unknown narrate backend: {config_backend!r}")
