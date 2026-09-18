#!/usr/bin/env python3
"""Bautin internships lane: rules-based approval markers (deterministic, no model).

Runs after discovery in the cron pre-check. When filters.md has `auto_apply: true`, every queue row
that passes all of Austin's rules gets an approval marker identical to the one his "apply q<N>"
reply would create, with `by: rules`. The submit guard accepts either. With `auto_apply: false`
(default) this script only reports what it would approve.

Rules (all must hold): tier is faang or known; role track is SWE/AI/ML/SWE-adj by title (never data
science); found within max_age_days; not Applied/Skipped in Notion; no existing marker; fewer than
`auto_apply_daily_cap` markers already written today.

    auto_approve.py [--vault /vault] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover import load_filters, parse_frontmatter, role_ok, vault_root  # noqa: E402
from notion_sync import done_index, norm_key, norm_url  # noqa: E402


def queue_rows(root: Path) -> list[dict]:
    f = root / "state" / "internships" / "queue.md"
    rows = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else []:
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) >= 8 and re.fullmatch(r"q\d+", c[0]):
            flags = c[7].split()
            rows.append({"id": c[0], "found": c[1], "company": c[2], "role": c[3], "location": c[4], "url": c[5],
                         "score": c[6], "tier": next((t for t in flags if t in ("faang", "known", "unknown")), "unknown")})
    return rows


def decide(root: Path, cfg: dict, today: date) -> dict:
    approvals = root / "state" / "internships" / "approvals"
    approvals.mkdir(parents=True, exist_ok=True)
    existing = {p.stem for p in approvals.glob("q*.json")}
    written_today = 0
    for p in approvals.glob("q*.json"):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("approved_at", "").startswith(today.isoformat()):
                written_today += 1
        except (OSError, json.JSONDecodeError):
            pass
    done_urls, done_keys = done_index(root)
    cap = int(cfg.get("auto_apply_daily_cap", 15) or 15)
    max_age = int(cfg.get("max_age_days", 1) or 1)
    out = {"auto_apply": bool(cfg.get("auto_apply", False)), "cap": cap, "written_today": written_today, "approve": [], "hold": []}
    for r in queue_rows(root):
        reasons = []
        if r["id"] in existing:
            continue
        if r["tier"] not in ("faang", "known"):
            reasons.append("tier")
        if not role_ok(r["role"], cfg):
            reasons.append("role")
        try:
            age = (today - date.fromisoformat(r["found"])).days
        except ValueError:
            age = 999
        if age > max_age:
            reasons.append(f"age {age}d")
        if norm_url(r["url"]) in done_urls or norm_key(r["company"], r["role"]) in done_keys:
            reasons.append("in notion")
        if reasons:
            out["hold"].append({"id": r["id"], "company": r["company"], "why": ", ".join(reasons)})
        elif written_today + len(out["approve"]) >= cap:
            out["hold"].append({"id": r["id"], "company": r["company"], "why": "daily cap"})
        else:
            out["approve"].append(r)
    return out


def write_markers(root: Path, rows: list[dict]) -> list[str]:
    approvals = root / "state" / "internships" / "approvals"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = []
    for r in rows:
        (approvals / f"{r['id']}.json").write_text(json.dumps({"id": r["id"], "url": r["url"], "company": r["company"], "role": r["role"],
                                                              "approved_at": now, "by": "rules", "message": "auto_apply rules"}, indent=1) + "\n", encoding="utf-8")
        ids.append(r["id"])
    log = root / "log" / "internships"; log.mkdir(parents=True, exist_ok=True)
    with (log / "approvals.log").open("a", encoding="utf-8") as fh:
        for i in ids:
            fh.write(f"{now}\trules\t{i}\tauto_apply\n")
    return ids


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault"); ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = vault_root(args.vault)
    cfg = load_filters(root)
    cfg.update({k: v for k, v in parse_frontmatter((root / "state" / "internships" / "filters.md").read_text(encoding="utf-8")).items()
                if k in ("auto_apply", "auto_apply_daily_cap")}) if (root / "state" / "internships" / "filters.md").exists() else None
    d = decide(root, cfg, date.today())
    written = write_markers(root, d["approve"]) if (d["auto_apply"] and not args.dry_run) else []
    print(json.dumps({"auto_apply": d["auto_apply"], "would_approve": [r["id"] for r in d["approve"]], "approved": written, "held": d["hold"][:20]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
