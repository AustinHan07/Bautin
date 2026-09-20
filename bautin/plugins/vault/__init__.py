"""Bautin vault memory provider for Hermes Agent.

Facts live as one markdown file each under ``<vault>/memory/<lane>/<kind>/<subject>.md``:

    ---
    lane: internships
    kind: portal
    subject: ibm
    date: 2026-09-19
    first_saved: 2026-09-19
    revision: 1
    source: agent | austin | <url>
    type: memory
    tags: [a, b]
    session: <session id>
    ---
    <the fact, one to three sentences>

Two decisions are kept apart, deliberately (the "gate" and the "pass"):

* the GATE decides WHETHER a turn produced anything durable. ``_gate()`` is a deterministic
  check: a save must declare a ``kind`` from ``KINDS`` and a ``subject``, and must not be
  session chatter (a queue id, a relative date, "we just ..."). A rejection explains what to
  write instead, so the model can fix it in one step rather than give up or retry blindly.
* the PASS decides WHAT IS KEPT. The path is derived from ``kind`` + ``subject``, so re-saving
  the same subject SUPERSEDES the old fact in place instead of accumulating near-duplicates.
  One subject, one file, with ``first_saved`` and ``revision`` preserved across rewrites.

The vault path is the single config value ``bautin.vault`` in the profile's config.yaml
(set by ``bautin/scripts/link-vault.sh``). If it is missing the provider is unavailable,
logs an error, exposes no tools, and says so in the system prompt.

Nothing is injected automatically: ``prefetch`` returns "" and the always-on block is one
short paragraph. The model retrieves facts with ``vault_memory_search`` and stores them
with ``vault_memory_save``. Search is ripgrep over the lane folder with a pure-Python
fallback; scoring is distinct-term hits, then recency.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger("bautin.vault_memory")

CONFIG_KEY = ("bautin", "vault")
MAX_FACT_CHARS = 1000
MIN_FACT_CHARS = 20
MAX_RETURN_CHARS = 600
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]{2,}")

# What earns a permanent slot. Anything outside this set belongs in the tracker, the queue,
# or the session log — not in long-term memory.
KINDS: Dict[str, str] = {
    "portal": "how a job portal behaves: what Apply does, whether it needs an account, what its form asks. "
              "subject = the portal or company, e.g. 'ibm' or 'workday'.",
    "company": "a durable recruiting pattern at a company: how it titles roles, which ATS it uses, its season, "
               "whether it sponsors. subject = the company.",
    "answer": "an answer or preference Austin gave once that should be reused on later applications. "
              "subject = the question topic, e.g. 'preferred-start-date'.",
    "outcome": "what happened with an application, with an absolute date: submitted, rejected, interviewed, or a "
               "portal that blocked us. subject = company plus role.",
}

# Session chatter, not durable facts.
_QUEUE_ID_RE = re.compile(r"\bq\d+\b", re.I)
_RELATIVE_TIME_RE = re.compile(
    r"\b(today|yesterday|tomorrow|just now|right now|this session|earlier today|last night|"
    r"this morning|this afternoon|a moment ago|a few minutes ago)\b", re.I)
_SESSION_VOICE_RE = re.compile(r"\b(i|we)\s+(just|will|am going to|are going to|have just)\b", re.I)


def _gate(fact: str, kind: str, subject: str) -> str:
    """Deterministic gate: "" when the fact earns a permanent slot, else why not + what to write instead."""
    if kind not in KINDS:
        return ("kind must be one of: " + ", ".join(f"{k} ({v.split(':')[0]})" for k, v in KINDS.items()) +
                ". Pick the one that fits, or do not save this at all.")
    if len(subject) < 2:
        return f"subject is required for kind '{kind}': {KINDS[kind].split('subject = ')[-1]}"
    if len(fact) < MIN_FACT_CHARS:
        return f"fact is {len(fact)} characters; a durable fact needs at least {MIN_FACT_CHARS}. Say what is true and why it matters."
    if len(fact) > MAX_FACT_CHARS:
        return f"fact is {len(fact)} characters; keep it under {MAX_FACT_CHARS}. Split it or shorten it."
    if _QUEUE_ID_RE.search(fact):
        return ("facts must not reference a queue id: queue rows are temporary and the id is reused by nobody. "
                "Name the company and role instead, and let the tracker hold the posting.")
    if _RELATIVE_TIME_RE.search(fact):
        return ("facts must not use a relative date ('today', 'just now'): they stop being true tomorrow. "
                "Write the absolute date, e.g. '2026-09-19'.")
    if _SESSION_VOICE_RE.search(fact):
        return ("that is session narration, not a durable fact. Write what is true of the portal, company, answer, "
                "or outcome so it still reads correctly in six months.")
    return ""

_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "vault_memory_search",
        "description": "Search this lane's long-term memory: dated, sourced facts saved in earlier sessions. "
                       "Check it BEFORE opening a portal, scoring a company, or asking Austin a question he may "
                       "have already answered.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for (company names, topics, dates)."},
            "kind": {"type": "string", "enum": sorted(KINDS), "description": "Optional: only this kind of fact."},
            "limit": {"type": "integer", "description": "Max facts to return, 1 to 20 (default 5)."}},
            "required": ["query"]},
    },
    {
        "name": "vault_memory_save",
        "description": "Save one durable fact to this lane's long-term memory. Only four kinds of thing earn a slot: "
                       + "; ".join(f"{k} = {v}" for k, v in KINDS.items()) +
                       " Re-saving the same kind+subject REPLACES the old fact, so correct a fact by saving it again. "
                       "Postings, queue rows and session narration are not memory.",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string", "description": "The fact, self-contained, 20 to 1000 characters, no relative dates."},
            "kind": {"type": "string", "enum": sorted(KINDS), "description": "Which kind of durable fact this is."},
            "subject": {"type": "string", "description": "What it is about: a company, portal, question topic, or company+role."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional short tags."},
            "source": {"type": "string", "description": "Where it came from: a URL, 'austin', or 'agent' (default)."}},
            "required": ["fact", "kind", "subject"]},
    },
]


def _read_vault_setting(hermes_home: str) -> str:
    """The configured vault path from <hermes_home>/config.yaml, or ""."""
    try:
        import yaml
        cfg = yaml.safe_load((Path(hermes_home) / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        return ""
    node: Any = cfg
    for key in CONFIG_KEY:
        node = node.get(key) if isinstance(node, dict) else None
    return str(node).strip() if node else ""


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:6]
    return "-".join(words) or "fact"


def _parse(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta: Dict[str, Any] = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            v = v.strip()
            if k.strip() == "tags":
                meta["tags"] = [t.strip().strip("'\"") for t in v.strip("[]").split(",") if t.strip()]
            elif k.strip():
                meta[k.strip()] = v.strip("'\"")
    return {"meta": meta, "body": body.strip()}


class VaultMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._hermes_home = ""
        self._vault: Optional[Path] = None
        self._lane = "default"
        self._session_id = ""
        self._reason = "not initialized"
        self._rg = shutil.which("rg")

    # ── identity / availability ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "vault"

    def _resolve(self, hermes_home: str) -> Optional[Path]:
        raw = _read_vault_setting(hermes_home)
        if not raw:
            self._reason = f"bautin.vault is not set in {hermes_home}/config.yaml (run bautin/scripts/link-vault.sh)"
            return None
        p = Path(raw).expanduser()
        if not p.is_dir():
            self._reason = f"vault path does not exist: {p}"
            return None
        self._reason = ""
        return p

    def is_available(self) -> bool:
        if self._vault is not None:
            return True
        try:
            from hermes_constants import get_hermes_home
            home = self._hermes_home or str(get_hermes_home())
        except Exception:  # pragma: no cover
            home = self._hermes_home
        ok = self._resolve(home) is not None
        if not ok:
            logger.error("vault memory unavailable: %s", self._reason)
        return ok

    def unavailable_reason(self) -> str:
        return self._reason

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id
        self._hermes_home = str(kwargs.get("hermes_home") or self._hermes_home or "")
        self._lane = re.sub(r"[^a-z0-9_-]", "", str(kwargs.get("agent_identity") or "default").lower()) or "default"
        self._vault = self._resolve(self._hermes_home)
        if self._vault is None:
            logger.error("vault memory unavailable for lane %s: %s", self._lane, self._reason)
            return
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def _dir(self) -> Path:
        assert self._vault is not None
        return self._vault / "memory" / self._lane

    def _files(self) -> List[Path]:
        return sorted(self._dir.rglob("*.md")) if self._vault else []

    def _count(self) -> int:
        return len(self._files())

    def _counts_by_kind(self) -> Dict[str, int]:
        out = {k: 0 for k in KINDS}
        for f in self._files():
            kind = f.parent.name
            if kind in out:
                out[kind] += 1
        return out

    # ── prompt surface: one paragraph, no automatic recall ─────────────────

    def system_prompt_block(self) -> str:
        if self._vault is None:
            return f"# Vault memory\nUNAVAILABLE: {self._reason}. Do not claim to remember earlier sessions."
        counts = self._counts_by_kind()
        held = ", ".join(f"{n} {k}" for k, n in counts.items() if n) or "nothing yet"
        return (f"# Vault memory\n{self._count()} durable facts for lane '{self._lane}' ({held}). Call "
                "vault_memory_search BEFORE opening a portal, scoring a company, or asking Austin something he may "
                "have answered already. Save with vault_memory_save: only portal, company, answer and outcome facts "
                "earn a slot, and re-saving the same kind+subject replaces the old one. Nothing is injected "
                "automatically.")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
        return None

    def backup_paths(self) -> List[str]:
        return []

    def identity_signature(self) -> Dict[str, Any]:
        return {"provider": "vault", "lane": self._lane, "vault": str(self._vault or "")}

    # ── tools ───────────────────────────────────────────────────────────────

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return json.loads(json.dumps(_SCHEMAS)) if self._vault is not None or self._hermes_home == "" else []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if self._vault is None:
            return tool_error(f"vault memory unavailable: {self._reason}")
        try:
            if tool_name == "vault_memory_search":
                return json.dumps(self._search(args))
            if tool_name == "vault_memory_save":
                return json.dumps(self._save(args))
        except (OSError, ValueError) as exc:
            return tool_error(f"vault memory error: {exc}")
        return tool_error(f"unknown tool: {tool_name}")

    # ── search ──────────────────────────────────────────────────────────────

    def _candidates(self, terms: List[str], kind: str = "") -> List[Path]:
        root = (self._dir / kind) if kind else self._dir
        if not root.is_dir():
            return []
        if self._rg:
            cmd = [self._rg, "-il", "--no-messages", "-g", "*.md"]
            for t in terms:
                cmd += ["-e", re.escape(t)]
            cmd.append(str(root))
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False).stdout
                return [Path(p) for p in out.splitlines() if p.strip()]
            except (OSError, subprocess.SubprocessError):
                pass
        hits = []
        for p in root.rglob("*.md"):
            text = p.read_text(encoding="utf-8", errors="replace").lower()
            if any(t in text for t in terms):
                hits.append(p)
        return hits

    def _search(self, args: Dict[str, Any]) -> Dict[str, Any] | str:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        try:
            limit = max(1, min(20, int(args.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        kind = str(args.get("kind") or "").strip().lower()
        if kind and kind not in KINDS:
            return tool_error(f"unknown kind '{kind}'; use one of: {', '.join(sorted(KINDS))}")
        terms = sorted(set(_TOKEN_RE.findall(query.lower()))) or [query.lower()]
        scored = []
        for p in self._candidates(terms, kind):
            rec = _parse(p)
            hay = (rec["body"] + " " + " ".join(rec["meta"].get("tags", [])) + " " +
                   rec["meta"].get("subject", "")).lower()
            hits = sum(1 for t in terms if t in hay)
            if hits:
                scored.append((hits, rec["meta"].get("date", ""), p, rec))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)  # most matched terms, then newest
        results = []
        for hits, _d, p, rec in scored[:limit]:
            body = rec["body"]
            results.append({"path": f"memory/{self._lane}/{p.parent.name}/{p.name}" if p.parent != self._dir
                                    else f"memory/{self._lane}/{p.name}",
                            "kind": rec["meta"].get("kind", ""), "subject": rec["meta"].get("subject", ""),
                            "date": rec["meta"].get("date", ""),
                            "source": rec["meta"].get("source", ""), "tags": rec["meta"].get("tags", []),
                            "fact": body[:MAX_RETURN_CHARS] + ("…" if len(body) > MAX_RETURN_CHARS else ""),
                            "matched_terms": hits})
        return {"lane": self._lane, "query": query, "kind": kind or "any", "count": len(results),
                "total_facts": self._count(), "results": results}

    # ── save ────────────────────────────────────────────────────────────────

    def _save(self, args: Dict[str, Any]) -> Dict[str, Any] | str:
        """Gate, then pass. The gate refuses anything that is not durable; the pass writes one file per
        kind+subject so a correction replaces the old fact instead of sitting next to it."""
        fact = re.sub(r"\s+", " ", str(args.get("fact") or "")).strip()
        kind = str(args.get("kind") or "").strip().lower()
        subject = re.sub(r"\s+", " ", str(args.get("subject") or "")).strip()
        if not fact:
            return tool_error("fact is required")
        problem = _gate(fact, kind, subject)
        if problem:
            return tool_error(f"not saved: {problem}")

        tags = [re.sub(r"[^a-z0-9_-]", "", str(t).lower()) for t in (args.get("tags") or []) if str(t).strip()][:8]
        source = str(args.get("source") or "agent").strip() or "agent"
        today = date.today().isoformat()
        kind_dir = self._dir / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        path = kind_dir / f"{_slug(subject)}.md"

        first_saved, revision, superseded = today, 1, ""
        if path.exists():
            prev = _parse(path)
            norm = lambda t: re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()
            if norm(prev["body"]) == norm(fact):
                return {"saved": False, "unchanged": True, "kind": kind, "subject": subject,
                        "path": f"memory/{self._lane}/{kind}/{path.name}"}
            first_saved = prev["meta"].get("first_saved") or prev["meta"].get("date") or today
            try:
                revision = int(str(prev["meta"].get("revision", 1)).strip()) + 1
            except (TypeError, ValueError):
                revision = 2
            superseded = prev["body"][:200]

        front = [f"lane: {self._lane}", f"kind: {kind}", f"subject: {subject}", f"date: {today}",
                 f"first_saved: {first_saved}", f"revision: {revision}", f"source: {source}", "type: memory",
                 f"tags: [{', '.join(tags)}]", f"session: {self._session_id}"]
        path.write_text("---\n" + "\n".join(front) + "\n---\n" + fact + "\n", encoding="utf-8")
        out: Dict[str, Any] = {"saved": True, "kind": kind, "subject": subject, "revision": revision,
                               "path": f"memory/{self._lane}/{kind}/{path.name}", "total_facts": self._count()}
        if superseded:
            out["superseded"] = superseded
        return out


def register(ctx: Any) -> None:
    ctx.register_memory_provider(VaultMemoryProvider())
