"""Bautin lane guards: deterministic rules that sit between the tools and the model.

Per-lane rule sets live in ``rules.py``. The active lane is the Hermes profile name; a
profile without rules gets no guards. The vault path is ``bautin.vault`` in the profile
config (set by ``bautin/scripts/link-vault.sh``); container paths ``/vault/...`` in tool
arguments are translated to the host vault path.

Hooks used (all fail-open on unexpected errors, logged):
  pre_gateway_dispatch    records approvals from the owner's own message ("apply q17, q18")
  pre_tool_call           blocks a real submission unless an approval marker exists
  transform_tool_result   appends a guard note to draft writes that contain unverified claims
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from . import rules as _rules

logger = logging.getLogger("bautin.lane_guards")


def _vault() -> Optional[Path]:
    try:
        from hermes_cli.config import load_config_readonly
        raw = ((load_config_readonly() or {}).get("bautin") or {}).get("vault")
    except Exception as exc:  # pragma: no cover
        logger.warning("lane_guards: cannot read config: %s", exc)
        return None
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    return p if p.is_dir() else None


def _lane() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:  # pragma: no cover
        return "default"


def _allowed_user_ids() -> set[str]:
    try:
        from agent.secret_scope import get_secret
        raw = get_secret("TELEGRAM_ALLOWED_USERS", "") or ""
    except Exception:  # pragma: no cover
        raw = ""
    return {x.strip() for x in raw.split(",") if x.strip()}


def _active_rules() -> Optional[_rules.LaneRules]:
    vault = _vault()
    if vault is None:
        return None
    cls = _rules.LANE_RULES.get(_lane())
    return cls(vault) if cls else None


def on_gateway_message(event: Any, **kwargs: Any) -> Optional[dict]:
    try:
        r = _active_rules()
        if r is None or getattr(event, "internal", False):
            return None
        uid = str(getattr(event, "user_id", "") or "")
        if uid and uid not in _allowed_user_ids():
            return None
        note = r.on_message(str(getattr(event, "text", "") or ""), uid)
        if note:
            return {"action": "rewrite", "text": f"{event.text}\n\n{note}"}
    except Exception as exc:
        logger.warning("lane_guards on_gateway_message failed open: %s", exc)
    return None


def before_tool(tool_name: str = "", args: Optional[dict] = None, **kwargs: Any) -> Optional[dict]:
    try:
        r = _active_rules()
        if r is None:
            return None
        msg = r.before_tool(tool_name, args or {})
        if msg:
            return {"action": "block", "message": msg}
    except Exception as exc:
        logger.warning("lane_guards before_tool failed open: %s", exc)
    return None


def after_tool(tool_name: str = "", args: Optional[dict] = None, result: Any = None, **kwargs: Any) -> Optional[str]:
    try:
        r = _active_rules()
        if r is None or not isinstance(result, str):
            return None
        return r.after_tool(tool_name, args or {}, result)
    except Exception as exc:
        logger.warning("lane_guards after_tool failed open: %s", exc)
        return None


def tool_done(tool_name: str = "", args: Optional[dict] = None, result: Any = None, **kwargs: Any) -> None:
    try:
        r = _active_rules()
        if r is not None:
            note = r.on_tool_done(tool_name, args or {}, result if isinstance(result, str) else "")
            if note:
                logger.info("lane_guards: %s", note[:200])
    except Exception as exc:
        logger.warning("lane_guards tool_done failed open: %s", exc)


MAIL_TOOL_SCHEMA = {"type": "object", "properties": {
    "domain": {"type": "string", "description": "Sender domain to look for, e.g. corteva.com or greenhouse-mail.io. Empty = any sender."},
    "since_min": {"type": "integer", "description": "Only consider mail newer than this many minutes (default 30)."}},
    "required": ["domain"]}


def mail_verification(args: Optional[dict] = None, **kwargs: Any) -> str:
    """Return the newest verification link or code from Austin's Gmail (host side; credentials never enter the sandbox)."""
    import json as _json, os, subprocess, sys
    args = args or {}
    script = Path(__file__).resolve().parents[2] / "scripts" / "internships" / "mail.py"
    env = dict(os.environ)
    try:
        from agent.secret_scope import get_secret
        for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
            v = get_secret(k, "")
            if v:
                env[k] = v
    except Exception:
        pass
    cmd = [sys.executable, str(script), "--verify", "--domain", str(args.get("domain") or ""), "--since-min", str(int(args.get("since_min") or 30))]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=env, check=False).stdout.strip()
        return out or _json.dumps({"found": False})
    except (OSError, subprocess.SubprocessError) as exc:
        return _json.dumps({"error": str(exc)[:200]})


def register(ctx: Any) -> None:
    ctx.register_tool("mail_verification", "bautin", MAIL_TOOL_SCHEMA, mail_verification,
                      description="Fetch the newest email-verification link or one-time code sent to Austin's Gmail by a job portal (give the sender domain).", emoji="📧")
    ctx.register_hook("pre_gateway_dispatch", on_gateway_message)
    ctx.register_hook("pre_tool_call", before_tool)
    ctx.register_hook("transform_tool_result", after_tool)
    ctx.register_hook("post_tool_call", tool_done)
