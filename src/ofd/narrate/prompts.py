"""Prompt templates for the narrate pass.

Versioned with the code so narrations are reproducible. When you evolve
a prompt, bump PROMPT_VERSION so the narrate command can re-run over
entries whose stored prompt version is older.
"""

from __future__ import annotations

from dataclasses import dataclass

from ofd.aggregate import Primitive, select_definition_commit
from ofd.events.record import Kind

PROMPT_VERSION = 1

SYSTEM_PROMPT = """\
You are documenting a newly-introduced Odoo framework primitive for an
engineering-audience "What's new in Odoo" talk.

Given the introducing commit and 2-3 example rollouts (before/after code
pairs), write a 2-3 sentence paragraph explaining:
  - why the previous pattern was problematic, and
  - what the new primitive does differently.

Constraints:
  - Focus on concrete technical reasons (performance, correctness,
    ergonomics, migration safety). Avoid marketing language.
  - Use present tense.
  - Never mention the PR author or commit SHA.
  - Never include bullet points, headers, or a preamble.
  - Output only the paragraph as plain markdown.
"""


@dataclass
class UserPromptInput:
    symbol: str
    kind: Kind
    active_version: str
    definition_subject: str
    definition_body: str
    rollout_examples: list[tuple[str, str, str]]  # (file, before, after)


def build_user_prompt_from(prim: Primitive, key_devs: list[str]) -> UserPromptInput:
    definition = select_definition_commit(prim)
    examples: list[tuple[str, str, str]] = []
    # Keep up to three rollouts; prefer key-dev authored ones first.
    key_rs = [r for r in prim.rollouts if r.commit.author_email in (key_devs or [])]
    other_rs = [r for r in prim.rollouts if r not in key_rs]
    for r in (key_rs + other_rs)[:3]:
        examples.append((r.file, r.before_snippet or "", r.after_snippet or ""))
    return UserPromptInput(
        symbol=prim.symbol,
        kind=prim.kind,
        active_version=prim.active_version,
        definition_subject=definition.subject if definition else "",
        definition_body="",  # not currently stored per-definition on Primitive
        rollout_examples=examples,
    )


def render_user_prompt(data: UserPromptInput) -> str:
    lines: list[str] = []
    lines.append(f"Symbol: `{data.symbol}`")
    lines.append(f"Kind: {data.kind.value}")
    lines.append(f"Active version: {data.active_version}")
    lines.append("")
    lines.append(f"Introducing commit subject: {data.definition_subject}")
    if data.definition_body:
        lines.append("")
        lines.append("Introducing commit body:")
        lines.append(data.definition_body)
    lines.append("")
    if data.rollout_examples:
        lines.append("Rollout examples:")
        lines.append("")
        for i, (file, before, after) in enumerate(data.rollout_examples, start=1):
            lines.append(f"Example {i} - `{file}`:")
            lines.append("")
            lines.append("Before:")
            lines.append("```")
            lines.append(before.strip() or "(none)")
            lines.append("```")
            lines.append("")
            lines.append("After:")
            lines.append("```")
            lines.append(after.strip() or "(none)")
            lines.append("```")
            lines.append("")
    lines.append("Write the paragraph now.")
    return "\n".join(lines)
