"""Lane rule sets for the Bautin guards. Pure functions over the vault; no Hermes imports,
so they unit-test without the runtime."""
from __future__ import annotations

import json
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


class InternshipsRules(LaneRules):
    lane = "internships"
    APPLY_RE = re.compile(r"^\s*apply\b[:\s]*(.*)$", re.I | re.S)
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

    def approved(self, qid: str) -> Optional[dict]:
        f = self.approvals / f"{qid}.json"
        try:
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except (OSError, json.JSONDecodeError):
            return None

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
        m = self.APPLY_RE.match(text or "")
        if not m:
            return None
        ids, is_all = self.parse_ids(m.group(1))
        rows = self.queue_rows()
        if is_all:
            ids = {k for k in rows if not self.approved(k)}
        if not ids:
            return "[guard] No queue ids found in that reply. Say e.g. `apply q17, q18` or `apply all`."
        self.approvals.mkdir(parents=True, exist_ok=True)
        recorded, unknown = [], []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for qid in sorted(ids, key=lambda s: int(s[1:])):
            row = rows.get(qid)
            if not row:
                unknown.append(qid)
                continue
            marker = {"id": qid, "url": row["url"], "company": row["company"], "role": row["role"],
                      "approved_at": now, "by": user_id, "message": text.strip()[:300]}
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

    # ── block unapproved submissions ─────────────────────────────────────────
    def before_tool(self, tool_name: str, args: dict) -> Optional[str]:
        if tool_name != "terminal":
            return None
        cmd = str(args.get("command") or "")
        if "submit.py" not in cmd or not re.search(r"(^|\s)--submit(\s|$)", cmd):
            return None
        m = re.search(r"--id\s+(q\d+)\b", cmd)
        if not m:
            return "[guard] Blocked: a real submission needs `--id q<N>` naming an approved queue row."
        qid = m.group(1)
        marker = self.approved(qid)
        if not marker:
            return (f"[guard] Blocked: no approval on file for {qid}. Austin has not replied `apply {qid}` in this chat. "
                    "Ask for approval; do not retry with a different id.")
        return None

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
