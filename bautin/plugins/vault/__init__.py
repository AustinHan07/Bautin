"""Bautin vault memory provider for Hermes Agent.

Facts live as one markdown file each under ``<vault>/memory/<lane>/``:

    ---
    lane: internships
    date: 2026-09-18
    source: agent | austin | <url>
    type: memory
    tags: [a, b]
    session: <session id>
    ---
    <the fact, one to three sentences>

The vault path is the single config value ``bautin.vault`` in the profile's config.yaml
(set by ``bautin/scripts/link-vault.sh``). If it is missing the provider is unavailable,
logs an error, exposes no tools, and says so in the system prompt.

Nothing is injected automatically: ``prefetch`` returns "" and the always-on block is one
short paragraph. The model retrieves facts with ``vault_memory_search`` and stores them
with ``vault_memory_save``. Search is ripgrep over the lane folder with a pure-Python
fallback; scoring is distinct-term hits, then recency.
"""
from __future__ import annotations

import hashlib
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
MAX_RETURN_CHARS = 600
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]{2,}")

_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "vault_memory_search",
        "description": "Search this lane's long-term memory: dated, sourced facts saved in earlier sessions. "
                       "Use before relying on memory of a company, decision, deadline, or preference.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for (company names, topics, dates)."},
            "limit": {"type": "integer", "description": "Max facts to return, 1 to 20 (default 5)."}},
            "required": ["query"]},
    },
    {
        "name": "vault_memory_save",
        "description": "Save one durable fact to this lane's long-term memory. One fact per call, one to three "
                       "sentences, with a source when it came from a page or a person. Do not save postings or chatter.",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string", "description": "The fact, self-contained, at most 1000 characters."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional short tags."},
            "source": {"type": "string", "description": "Where it came from: a URL, 'austin', or 'agent' (default)."}},
            "required": ["fact"]},
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

    def _count(self) -> int:
        return sum(1 for _ in self._dir.glob("*.md")) if self._vault else 0

    # ── prompt surface: one paragraph, no automatic recall ─────────────────

    def system_prompt_block(self) -> str:
        if self._vault is None:
            return f"# Vault memory\nUNAVAILABLE: {self._reason}. Do not claim to remember earlier sessions."
        return (f"# Vault memory\n{self._count()} dated facts for lane '{self._lane}'. Call vault_memory_search before "
                "relying on memory of a company, decision, deadline, or preference. Save a durable fact with "
                "vault_memory_save, one fact per call. Nothing from memory is injected automatically.")

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

    def _candidates(self, terms: List[str]) -> List[Path]:
        if self._rg:
            cmd = [self._rg, "-il", "--no-messages", "-g", "*.md"]
            for t in terms:
                cmd += ["-e", re.escape(t)]
            cmd.append(str(self._dir))
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False).stdout
                return [Path(p) for p in out.splitlines() if p.strip()]
            except (OSError, subprocess.SubprocessError):
                pass
        hits = []
        for p in self._dir.glob("*.md"):
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
        terms = sorted(set(_TOKEN_RE.findall(query.lower()))) or [query.lower()]
        scored = []
        for p in self._candidates(terms):
            rec = _parse(p)
            hay = (rec["body"] + " " + " ".join(rec["meta"].get("tags", []))).lower()
            hits = sum(1 for t in terms if t in hay)
            if hits:
                scored.append((hits, rec["meta"].get("date", ""), p, rec))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)  # most matched terms, then newest
        results = []
        for hits, _d, p, rec in scored[:limit]:
            body = rec["body"]
            results.append({"path": f"memory/{self._lane}/{p.name}", "date": rec["meta"].get("date", ""),
                            "source": rec["meta"].get("source", ""), "tags": rec["meta"].get("tags", []),
                            "fact": body[:MAX_RETURN_CHARS] + ("…" if len(body) > MAX_RETURN_CHARS else ""),
                            "matched_terms": hits})
        return {"lane": self._lane, "query": query, "count": len(results), "total_facts": self._count(), "results": results}

    # ── save ────────────────────────────────────────────────────────────────

    def _save(self, args: Dict[str, Any]) -> Dict[str, Any] | str:
        fact = re.sub(r"\s+", " ", str(args.get("fact") or "")).strip()
        if not fact:
            return tool_error("fact is required")
        if len(fact) > MAX_FACT_CHARS:
            return tool_error(f"fact is {len(fact)} characters; keep it under {MAX_FACT_CHARS}. Split it or shorten it.")
        tags = [re.sub(r"[^a-z0-9_-]", "", str(t).lower()) for t in (args.get("tags") or []) if str(t).strip()][:8]
        source = str(args.get("source") or "agent").strip() or "agent"
        norm = re.sub(r"[^a-z0-9]+", " ", fact.lower()).strip()
        for p in self._dir.glob("*.md"):
            if re.sub(r"[^a-z0-9]+", " ", _parse(p)["body"].lower()).strip() == norm:
                return {"saved": False, "duplicate": True, "path": f"memory/{self._lane}/{p.name}"}
        today = date.today().isoformat()
        digest = hashlib.sha1(norm.encode()).hexdigest()[:6]
        path = self._dir / f"{today}-{_slug(fact)}-{digest}.md"
        front = [f"lane: {self._lane}", f"date: {today}", f"source: {source}", "type: memory",
                 f"tags: [{', '.join(tags)}]", f"session: {self._session_id}"]
        path.write_text("---\n" + "\n".join(front) + "\n---\n" + fact + "\n", encoding="utf-8")
        return {"saved": True, "path": f"memory/{self._lane}/{path.name}", "total_facts": self._count()}


def register(ctx: Any) -> None:
    ctx.register_memory_provider(VaultMemoryProvider())
