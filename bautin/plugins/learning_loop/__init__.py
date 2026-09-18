"""Bautin learning loop tilt.

Hermes already ends its background review prompts with "If nothing is worth saving, say
'Nothing to save.' and stop", but the skill prompt also says not to default to that. Bautin
wants the opposite bias: lean memory, few skills. ``agent/background_review.py`` resolves each
prompt with ``getattr(agent, name, module_default)`` and ``AIAgent`` exposes them as class
attributes, so this plugin appends one paragraph to those class attributes. Idempotent.

Destinations are untouched: with ``memory.write_approval`` and ``skills.write_approval`` on,
review writes are staged under pending/ (linked to the vault's proposed/<lane>/).
"""
from __future__ import annotations

import logging
import sys
from typing import Any

logger = logging.getLogger("bautin.learning_loop")

PROMPT_ATTRS = ("_MEMORY_REVIEW_PROMPT", "_SKILL_REVIEW_PROMPT", "_COMBINED_REVIEW_PROMPT")
MARKER = "Bautin rule:"
SUFFIX = (
    "\n\nBautin rule: when in doubt, save nothing. Save a memory only if it would change a future "
    "answer in this lane and is not already in the lane's reference files. Create or edit a skill "
    "only when the same correction has happened at least twice. Never store postings, drafts, "
    "digests, or one-off task details. 'Nothing to save.' is the expected outcome of most sessions."
)


def apply_to(cls: Any) -> int:
    """Append SUFFIX to each review prompt attribute on cls (once). Returns how many were changed."""
    changed = 0
    for name in PROMPT_ATTRS:
        cur = getattr(cls, name, None)
        if isinstance(cur, str) and MARKER not in cur:
            setattr(cls, name, cur + SUFFIX)
            changed += 1
    return changed


def _apply() -> None:
    mod = sys.modules.get("run_agent")
    if mod is None:
        try:
            import run_agent as mod  # type: ignore
        except Exception as exc:  # pragma: no cover - only while run_agent is mid-import
            logger.debug("learning_loop: run_agent not importable yet (%s)", exc)
            return
    cls = getattr(mod, "AIAgent", None)
    if cls is not None and apply_to(cls):
        logger.info("learning_loop: review prompts tilted toward saving nothing")


def on_session_start(**kwargs: Any) -> None:
    _apply()


def register(ctx: Any) -> None:
    _apply()
    ctx.register_hook("on_session_start", on_session_start)
