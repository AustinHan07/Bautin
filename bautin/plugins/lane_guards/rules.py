"""Lane rule sets for the Bautin guards. Pure functions over the vault; no Hermes imports,
so they unit-test without the runtime."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CONTAINER_VAULT = "/vault"


def host_path(vault: Path, p: str) -> Path:
    """Translate a sandbox path (/vault/...) to the host vault path; other paths pass through."""
    if p == CONTAINER_VAULT or p.startswith(CONTAINER_VAULT + "/"):
        return vault / p[len(CONTAINER_VAULT):].lstrip("/")
    return Path(p)


# ── approval markers are signed with a host-only key ─────────────────────────
# The sandbox never sees the profile .env, so a marker the model writes itself cannot carry
# a valid signature. Only the Telegram guard (Austin's own `apply q<N>`) and the cron-side
# rules script hold the key.
SIG_FIELDS = ("id", "url", "company", "role", "approved_at", "by")


def marker_sig(marker: dict, key: str) -> str:
    msg = "\x1f".join(str(marker.get(k, "")) for k in SIG_FIELDS).encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def sign_marker(marker: dict, key: str) -> dict:
    signed = dict(marker)
    signed["sig"] = marker_sig(signed, key)
    return signed


def marker_valid(marker: object, key: str) -> bool:
    if not key or not isinstance(marker, dict):
        return False
    sig = marker.get("sig")
    return isinstance(sig, str) and hmac.compare_digest(sig, marker_sig(marker, key))


def read_secret(name: str) -> str:
    """Host-side secret: Hermes's scope/env first, else $HERMES_HOME/.env. Never runs inside the sandbox."""
    try:
        from agent.secret_scope import get_secret
        val = get_secret(name, "") or ""
        if val:
            return val
    except Exception:
        pass
    try:
        try:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
        except Exception:
            home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        env = home / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class LaneRules:
    lane = "base"

    def __init__(self, vault: Path) -> None:
        self.vault = vault

    def on_message(self, text: str, user_id: str) -> Optional[str]:
        return None

    def before_tool(self, tool_name: str, args: dict) -> Optional[str]:
        return None

    def after_tool(self, tool_name: str, args: dict, result: str) -> Optional[str]:
        return None

    def on_tool_done(self, tool_name: str, args: dict, result: str) -> Optional[str]:
        """Side effects after a tool ran (post_tool_call). Return a note for the log, or None."""
        return None


class InternshipsRules(LaneRules):
    lane = "internships"
    APPLY_RE = re.compile(r"^\s*apply\b[:\s]*(.*)$", re.I | re.S)
    SKIP_RE = re.compile(r"^\s*(?:skip|discard|drop)\b[:\s]*(.*)$", re.I | re.S)
    CLAIM_PATTERNS = [
        (r"\b(19|20)\d{2}\b", "year"),
        (r"\b\d\.\d{1,2}\b", "number"),
        (r"\b\d{1,3}(?:\.\d+)?\s?%", "percentage"),
        (r"\b(?:University|College|Institute|School) of [A-Z][\w&.-]+(?: [A-Z][\w&.-]+)*", "school"),
        (r"\b[A-Z][\w&.-]+ (?:University|College|Institute)\b", "school"),
        (r"\bGPA\b[^.\n]{0,20}", "gpa"),
    ]

    # ── paths ────────────────────────────────────────────────────────────────
    @property
    def state(self) -> Path:
        return self.vault / "state" / "internships"

    @property
    def approvals(self) -> Path:
        return self.state / "approvals"

    def facts_text(self) -> str:
        refs = self.vault / "skills" / "internships" / "internship-pipeline" / "references"
        out = []
        for name in ("facts.md", "answers.md"):
            f = refs / name
            if f.exists():
                out.append(f.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(out).lower()

    def queue_rows(self) -> dict[str, dict]:
        f = self.state / "queue.md"
        rows: dict[str, dict] = {}
        if not f.exists():
            return rows
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 6 and re.fullmatch(r"q\d+", cells[0]):
                rows[cells[0]] = {"id": cells[0], "found": cells[1], "company": cells[2], "role": cells[3],
                                  "location": cells[4], "url": cells[5]}
        return rows

    def approval_key(self) -> str:
        return self.secret("BAUTIN_APPROVAL_KEY") or self.secret("TELEGRAM_BOT_TOKEN")

    def marker_status(self, qid: str) -> tuple[Optional[dict], str]:
        """(marker, "") when a guard-signed marker exists for *qid*, else (None, reason)."""
        f = self.approvals / f"{qid}.json"
        key = self.approval_key()
        if not key:
            return None, ("the approval signing key is missing (set TELEGRAM_BOT_TOKEN or BAUTIN_APPROVAL_KEY "
                          "in the profile .env), so no approval can be verified.")
        if not f.exists():
            return None, f"no approval on file for {qid}. Austin has not replied `apply {qid}` in this chat."
        try:
            marker = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, f"the approval marker for {qid} is unreadable."
        if not marker_valid(marker, key):
            return None, (f"the approval marker for {qid} is not signed by the guard, so it was not written "
                          f"from Austin's `apply {qid}` reply.")
        return marker, ""

    def approved(self, qid: str) -> Optional[dict]:
        return self.marker_status(qid)[0]

    # ── approvals from the owner's message ───────────────────────────────────
    @staticmethod
    def parse_ids(spec: str) -> tuple[set[str], bool]:
        """'q17, 18 20-22' -> ({'q17','q18','q20','q21','q22'}, False); 'all' -> (set(), True)."""
        if re.search(r"\ball\b", spec, re.I):
            return set(), True
        ids: set[str] = set()
        for a, b in re.findall(r"\bq?(\d+)\s*-\s*q?(\d+)\b", spec, re.I):
            lo, hi = sorted((int(a), int(b)))
            ids.update(f"q{i}" for i in range(lo, min(hi, lo + 50) + 1))
        spec_wo_ranges = re.sub(r"\bq?\d+\s*-\s*q?\d+\b", " ", spec, flags=re.I)
        ids.update(f"q{n}" for n in re.findall(r"\bq?(\d+)\b", spec_wo_ranges, re.I))
        return ids, False

    def on_message(self, text: str, user_id: str) -> Optional[str]:
        sm = self.SKIP_RE.match(text or "")
        if sm:
            return self.skip_ids(sm.group(1), user_id, text)
        m = self.APPLY_RE.match(text or "")
        if not m:
            return None
        ids, is_all = self.parse_ids(m.group(1))
        rows = self.queue_rows()
        if is_all:
            ids = {k for k in rows if not self.approved(k)}
        if not ids:
            return "[guard] No queue ids found in that reply. Say e.g. `apply q17, q18` or `apply all`."
        key = self.approval_key()
        if not key:
            return ("[guard] Cannot record approvals: no signing key (TELEGRAM_BOT_TOKEN or BAUTIN_APPROVAL_KEY) "
                    "in the profile .env. Nothing approved.")
        self.approvals.mkdir(parents=True, exist_ok=True)
        recorded, unknown = [], []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for qid in sorted(ids, key=lambda s: int(s[1:])):
            row = rows.get(qid)
            if not row:
                unknown.append(qid)
                continue
            marker = sign_marker({"id": qid, "url": row["url"], "company": row["company"], "role": row["role"],
                                  "approved_at": now, "by": user_id, "message": text.strip()[:300]}, key)
            (self.approvals / f"{qid}.json").write_text(json.dumps(marker, indent=1) + "\n", encoding="utf-8")
            recorded.append(f"{qid} {row['company']} — {row['role']}")
        log = self.vault / "log" / "internships"
        log.mkdir(parents=True, exist_ok=True)
        with (log / "approvals.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{now}\t{user_id}\t{','.join(sorted(ids))}\t{text.strip()[:200]}\n")
        parts = []
        if recorded:
            parts.append("[guard] Approval recorded for: " + "; ".join(recorded) + ". Submission may proceed for these ids only.")
        if unknown:
            parts.append("[guard] No queue row for: " + ", ".join(unknown) + ". Not approved.")
        return "\n".join(parts)

    def skip_ids(self, spec: str, user_id: str, text: str) -> Optional[str]:
        ids, _ = self.parse_ids(spec)
        if not ids:
            return "[guard] No queue ids found. Say e.g. `skip q4, q41`."
        f = self.state / "queue.md"
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else []
        kept, removed = [], []
        for line in lines:
            c = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(c) >= 6 and c[0] in ids:
                removed.append(c[0]); continue
            kept.append(line)
        if removed:
            f.write_text("\n".join(kept) + "\n", encoding="utf-8")
            marker_dir = self.approvals
            for qid in removed:
                mk = marker_dir / f"{qid}.json"
                if mk.exists():
                    mk.unlink()
            self.notion_skip(removed)
        log = self.vault / "log" / "internships"; log.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with (log / "approvals.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{now}\t{user_id}\tskip:{','.join(sorted(ids))}\t{text.strip()[:200]}\n")
        unknown = sorted(ids - set(removed), key=lambda s: int(s[1:]))
        parts = []
        if removed:
            parts.append("[guard] Skipped and removed from the queue: " + ", ".join(sorted(removed, key=lambda s: int(s[1:]))) + " (marked Skipped in Notion).")
        if unknown:
            parts.append("[guard] Not in the queue: " + ", ".join(unknown) + ".")
        return "\n".join(parts)

    def notion_skip(self, ids: list[str]) -> Optional[str]:
        import os, subprocess, sys
        script = Path(__file__).resolve().parents[2] / "scripts" / "internships" / "notion_sync.py"
        env = dict(os.environ)
        tok = self.secret("NOTION_TOKEN")
        if tok:
            env["NOTION_TOKEN"] = tok
        try:
            return subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--skip", *ids],
                                  capture_output=True, text=True, timeout=90, env=env, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            return json.dumps({"error": str(exc)[:200]})

    # ── block unapproved submissions and any model-side marker writes ────────
    APPROVALS_PATH_RE = re.compile(r"internships[/\\]+approvals(?:[/\\]|\s|$)")
    TERMINAL_WRITE_RE = re.compile(
        r">|\btee\b|\b(?:cp|mv|touch|rm|mkdir|rmdir|chmod|chown|ln|dd|install|sed|perl|python3?|node|ruby|sh|bash|zsh|truncate)\b")
    MARKER_RULE = ("[guard] Blocked: approval markers under state/internships/approvals are written only when Austin replies "
                   "`apply q<N>` on Telegram, or by the rules script during the cron pre-check. A marker written any other way "
                   "is unsigned and rejected. Never create, edit, or delete markers. If an id has no approval, tell Austin and stop.")

    def before_tool(self, tool_name: str, args: dict) -> Optional[str]:
        if tool_name in ("write_file", "patch"):
            return self.MARKER_RULE if self.APPROVALS_PATH_RE.search(str(args.get("path") or "")) else None
        if tool_name != "terminal":
            return None
        cmd = str(args.get("command") or "")
        if "auto_approve.py" in cmd:
            return self.MARKER_RULE
        if self.APPROVALS_PATH_RE.search(cmd) and self.TERMINAL_WRITE_RE.search(cmd):
            return self.MARKER_RULE
        if "submit.py" not in cmd or not re.search(r"(^|\s)--submit(\s|$)", cmd):
            return None
        m = re.search(r"--id\s+(q\d+)\b", cmd)
        if not m:
            return "[guard] Blocked: a real submission needs `--id q<N>` naming an approved queue row."
        marker, problem = self.marker_status(m.group(1))
        if marker is None:
            return f"[guard] Blocked: {problem} Ask for approval; do not retry with a different id and do not write markers yourself."
        return None

    # ── after a real submission: log the row in Notion (host side) ───────────
    def on_tool_done(self, tool_name: str, args: dict, result: str) -> Optional[str]:
        if tool_name != "terminal":
            return None
        cmd = str(args.get("command") or "")
        m = re.search(r"--id\s+(q\d+)\b", cmd)
        if "submit.py" not in cmd or not re.search(r"(^|\s)--submit(\s|$)", cmd) or not m:
            return None
        res = self.state / "drafts" / f"{m.group(1)}-result.json"
        if not res.exists():
            return None
        try:
            status = json.loads(res.read_text(encoding="utf-8")).get("status")
        except (OSError, json.JSONDecodeError):
            return None
        if status not in ("submitted", "uncertain"):
            return None
        return self.push_notion(res)

    def push_notion(self, result_path: Path) -> Optional[str]:
        import os, subprocess, sys
        script = Path(__file__).resolve().parents[2] / "scripts" / "internships" / "notion_sync.py"
        env = dict(os.environ)
        tok = self.secret("NOTION_TOKEN")
        if tok:
            env["NOTION_TOKEN"] = tok
        try:
            out = subprocess.run([sys.executable, str(script), "--vault", str(self.vault), "--push", str(result_path)],
                                 capture_output=True, text=True, timeout=60, env=env, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            out = json.dumps({"error": str(exc)})
        log = self.vault / "log" / "internships"; log.mkdir(parents=True, exist_ok=True)
        with (log / "notion.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{result_path.name}\t{out[:300]}\n")
        return out

    def secret(self, name: str) -> str:
        """Host-side secret lookup (see read_secret). Overridable in tests."""
        return read_secret(name)

    # ── flag unverified claims in drafts ─────────────────────────────────────
    def after_tool(self, tool_name: str, args: dict, result: str) -> Optional[str]:
        if tool_name not in ("write_file", "patch"):
            return None
        path = str(args.get("path") or "")
        if "/state/internships/drafts/" not in path:
            return None
        content = str(args.get("content") or "")
        if not content:
            hp = host_path(self.vault, path)
            content = hp.read_text(encoding="utf-8", errors="replace") if hp.exists() else ""
        if not content:
            return None
        missing = self.unverified_claims(content)
        if not missing:
            return result + "\n[guard] Draft claims check: every year, number, and school in the draft appears in facts.md or answers.md."
        return (result + "\n[guard] Draft contains claims NOT found in facts.md or answers.md: "
                + "; ".join(missing) + ". Remove them or ask Austin before this draft is used.")

    def unverified_claims(self, draft: str) -> list[str]:
        facts = self.facts_text()
        missing: list[str] = []
        seen: set[str] = set()
        for pattern, kind in self.CLAIM_PATTERNS:
            for m in re.finditer(pattern, draft):
                claim = m.group(0).strip()
                key = claim.lower()
                if key in seen:
                    continue
                seen.add(key)
                if key not in facts:
                    missing.append(f"{kind} '{claim}'")
        return missing


LANE_RULES: dict[str, type[LaneRules]] = {"internships": InternshipsRules}
