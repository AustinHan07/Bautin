#!/usr/bin/env python3
"""Bautin internships lane: two-way sync with Austin's Notion "Applications" tracker (official API, no MCP).

    notion_sync.py --pull                 read every row -> <vault>/state/internships/notion-applied.json
    notion_sync.py --push <result.json>   after a real submission: upsert the row (by Apply URL), then refresh
                                          the "Applications sent: N" paragraph on the parent page
    notion_sync.py --push-queued          log queue rows not yet in Notion as Status=Queued (explicit, opt-in)

Mapping and ids come from <vault>/state/internships/notion.md frontmatter. The token is NOTION_TOKEN from the
environment, else from $HERMES_HOME/.env (host side only; never forwarded into the sandbox).
Standard library only. Exit 0 with a one-line JSON report, 1 on API/config error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Optional

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
UA = "bautin-notion-sync/1.0"

DONE_DEFAULT = ["Applied", "Applying", "OA", "Interview", "Offer", "Rejected", "Skipped"]


# ── config / vault ───────────────────────────────────────────────────────────

def vault_root(arg: Optional[str]) -> Path:
    root = Path(arg or os.environ.get("BAUTIN_VAULT") or "/vault")
    if not (root / "state" / "internships").is_dir():
        sys.exit(f"vault not found or missing state/internships: {root}")
    return root


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    out: dict = {}
    if not m:
        return out
    key = None
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if re.match(r"^\s+-\s", line) and key:
            out.setdefault(key, []).append(line.split("-", 1)[1].strip().strip("'\""))
            continue
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not km:
            continue
        key, val = km.group(1), km.group(2).strip()
        if val == "":
            out[key] = []
        elif val.startswith("[") and val.endswith("]"):
            out[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            out[key] = val.strip("'\"")
    return out


def load_cfg(root: Path) -> dict:
    f = root / "state" / "internships" / "notion.md"
    if not f.exists():
        sys.exit(f"missing {f}; add the Notion mapping frontmatter")
    cfg = parse_frontmatter(f.read_text(encoding="utf-8"))
    for k in ("database_id", "page_id"):
        if not cfg.get(k):
            sys.exit(f"notion.md: {k} is required")
    cfg.setdefault("status_done", DONE_DEFAULT)
    cfg.setdefault("status_queued", "Queued"); cfg.setdefault("status_applied", "Applied"); cfg.setdefault("status_uncertain", "Applying")
    cfg.setdefault("counter_prefix", "Applications sent:")
    for p in ("company", "role", "location", "url", "posted", "applied", "status", "portal", "source", "track", "company_type", "referral", "notes"):
        cfg.setdefault(f"prop_{p}", {"company": "Company", "role": "Role", "location": "Location", "url": "Apply URL", "posted": "Posted",
                                     "applied": "Applied", "status": "Status", "portal": "Portal", "source": "Source", "track": "Track",
                                     "company_type": "Company type", "referral": "Referral", "notes": "Notes"}[p])
    return cfg


def token() -> str:
    t = os.environ.get("NOTION_TOKEN", "").strip()
    if t:
        return t
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    env = home / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("NOTION_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("NOTION_TOKEN not set (environment or $HERMES_HOME/.env)")


# ── http ─────────────────────────────────────────────────────────────────────

def request(method: str, path: str, body: Optional[dict] = None, tok: Optional[str] = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {tok or token()}", "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"notion {method} {path} -> {e.code}: {detail}") from None


# ── helpers ──────────────────────────────────────────────────────────────────

def norm_url(u: str) -> str:
    p = urllib.parse.urlsplit((u or "").strip().lower())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not k.startswith("utm_") and k != "ref"]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.replace("www.", ""), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))


def norm_key(company: str, role: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", f"{company} {role}".lower()).strip()


def prop_value(p: dict) -> Any:
    t = p.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in p["title"])
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in p["rich_text"])
    if t == "select":
        return (p.get("select") or {}).get("name")
    if t == "status":
        return (p.get("status") or {}).get("name")
    if t == "date":
        return (p.get("date") or {}).get("start")
    if t == "url":
        return p.get("url")
    if t == "checkbox":
        return p.get("checkbox")
    return None


def row_from_page(page: dict, cfg: dict) -> dict:
    props = page.get("properties", {})
    g = lambda k: prop_value(props.get(cfg[f"prop_{k}"], {})) if cfg[f"prop_{k}"] in props else None
    return {"page_id": page.get("id"), "company": g("company") or "", "role": g("role") or "", "location": g("location") or "",
            "url": g("url") or "", "status": g("status") or "", "posted": g("posted"), "applied": g("applied"),
            "portal": g("portal"), "source": g("source"), "track": g("track"), "company_type": g("company_type"),
            "notes": (g("notes") or "")[:300], "url_key": norm_url(g("url") or ""), "key": norm_key(g("company") or "", g("role") or "")}


def classify_track(role: str) -> str:
    r = role.lower()
    if re.search(r"\bai\b|agent|llm|applied ai|generative", r):
        return "AI"
    if re.search(r"machine learning|\bml\b", r):
        return "ML"
    if re.search(r"software|swe|sde|backend|back-end|frontend|front-end|full.?stack|mobile|ios|android|platform|infrastructure|developer", r):
        return "SWE"
    return "SWE-adj"


def classify_portal(url: str, family: Optional[str]) -> str:
    u = (url or "").lower()
    if family in ("greenhouse", "lever", "ashby"):
        return family.capitalize()
    if "myworkdayjobs" in u or "workday" in u:
        return "Workday"
    if "linkedin.com" in u:
        return "LinkedIn"
    if "greenhouse" in u:
        return "Greenhouse"
    if "lever.co" in u:
        return "Lever"
    if "ashbyhq" in u:
        return "Ashby"
    return "Company"


def classify_source(source: str) -> str:
    s = (source or "").lower()
    if s == "simplify":
        return "Simplify"
    if "zero2sudo" in s or "instagram" in s:
        return "Zero2Sudo"
    if s in ("greenhouse", "lever", "ashby", "board"):
        return "Company site"
    return "Other"


def classify_company_type(tier: str) -> str:
    return {"faang": "Big Tech", "known": "Strong mid"}.get((tier or "").lower(), "Other")


# ── pull ─────────────────────────────────────────────────────────────────────

def pull(root: Path, cfg: dict, tok: Optional[str] = None) -> dict:
    rows, cursor = [], None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = request("POST", f"/databases/{cfg['database_id']}/query", body, tok)
        rows += [row_from_page(p, cfg) for p in data.get("results", [])]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    out = {"pulled_at": date.today().isoformat(), "count": len(rows), "rows": rows,
           "by_status": {s: sum(1 for r in rows if r["status"] == s) for s in sorted({r["status"] for r in rows})}}
    (root / "state" / "internships" / "notion-applied.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def load_applied(root: Path) -> dict:
    f = root / "state" / "internships" / "notion-applied.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"rows": []}


def done_index(root: Path, cfg: Optional[dict] = None) -> tuple[set[str], set[str]]:
    """(url_keys, company+role keys) for rows whose status means never queue/apply again."""
    done = set((cfg or {}).get("status_done") or DONE_DEFAULT)
    urls, keys = set(), set()
    for r in load_applied(root).get("rows", []):
        if r.get("status") in done:
            if r.get("url_key"):
                urls.add(r["url_key"])
            if r.get("key"):
                keys.add(r["key"])
    return urls, keys


# ── push ─────────────────────────────────────────────────────────────────────

def _rt(s: str) -> dict:
    return {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}


def build_properties(cfg: dict, row: dict, status: str, applied: Optional[str], notes: str) -> dict:
    props = {
        cfg["prop_company"]: {"title": [{"text": {"content": row.get("company", "")[:200]}}]},
        cfg["prop_role"]: _rt(row.get("role", "")),
        cfg["prop_location"]: _rt(row.get("location", "") or "TBD"),
        cfg["prop_url"]: {"url": row.get("url") or None},
        cfg["prop_status"]: {"select": {"name": status}},
        cfg["prop_portal"]: {"select": {"name": classify_portal(row.get("url", ""), row.get("family"))}},
        cfg["prop_source"]: {"select": {"name": classify_source(row.get("source", ""))}},
        cfg["prop_track"]: {"select": {"name": classify_track(row.get("role", ""))}},
        cfg["prop_company_type"]: {"select": {"name": classify_company_type(row.get("tier", ""))}},
        cfg["prop_referral"]: {"checkbox": False},
        cfg["prop_notes"]: _rt(notes),
    }
    if row.get("posted"):
        props[cfg["prop_posted"]] = {"date": {"start": row["posted"]}}
    if applied:
        props[cfg["prop_applied"]] = {"date": {"start": applied}}
    return props


def find_page(cfg: dict, url: str, tok: Optional[str] = None) -> Optional[str]:
    if not url:
        return None
    data = request("POST", f"/databases/{cfg['database_id']}/query",
                   {"page_size": 5, "filter": {"property": cfg["prop_url"], "url": {"equals": url}}}, tok)
    for p in data.get("results", []):
        return p["id"]
    return None


def upsert(cfg: dict, row: dict, status: str, applied: Optional[str], notes: str, tok: Optional[str] = None) -> dict:
    props = build_properties(cfg, row, status, applied, notes)
    page_id = find_page(cfg, row.get("url", ""), tok)
    if page_id:
        request("PATCH", f"/pages/{page_id}", {"properties": props}, tok)
        return {"action": "updated", "page_id": page_id}
    data = request("POST", "/pages", {"parent": {"database_id": cfg["database_id"]}, "properties": props}, tok)
    return {"action": "created", "page_id": data.get("id")}


def refresh_counter(cfg: dict, tok: Optional[str] = None) -> Optional[int]:
    """Count Applied rows and rewrite the 'Applications sent: N' paragraph on the parent page."""
    n, cursor = 0, None
    while True:
        body = {"page_size": 100, "filter": {"property": cfg["prop_status"], "select": {"equals": cfg["status_applied"]}}}
        if cursor:
            body["start_cursor"] = cursor
        data = request("POST", f"/databases/{cfg['database_id']}/query", body, tok)
        n += len(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    blocks = request("GET", f"/blocks/{cfg['page_id']}/children?page_size=100", None, tok).get("results", [])
    prefix = cfg["counter_prefix"]
    for b in blocks:
        if b.get("type") == "paragraph":
            text = "".join(x.get("plain_text", "") for x in b["paragraph"].get("rich_text", []))
            if text.startswith(prefix):
                request("PATCH", f"/blocks/{b['id']}", {"paragraph": {"rich_text": [{"text": {"content": f"{prefix} {n}"}}]}}, tok)
                return n
    return n


def push_result(root: Path, cfg: dict, result_path: Path, tok: Optional[str] = None) -> dict:
    res = json.loads(result_path.read_text(encoding="utf-8"))
    if res.get("status") not in ("submitted", "uncertain"):
        return {"pushed": False, "reason": f"status {res.get('status')} is not a submission"}
    today = date.today().isoformat()
    qrow = queue_row(root, res.get("id", ""))
    row = {"company": res.get("company") or (qrow or {}).get("company", ""), "role": res.get("role") or (qrow or {}).get("role", ""),
           "location": (qrow or {}).get("location", ""), "url": res.get("url") or (qrow or {}).get("url", ""),
           "family": res.get("family"), "source": (qrow or {}).get("source", "simplify"), "tier": (qrow or {}).get("tier", ""),
           "posted": (qrow or {}).get("found")}
    if res["status"] == "submitted":
        status, notes = cfg["status_applied"], f"Bautin {today}. Confirmation: {res.get('confirmation') or 'n/a'}"
    else:
        status, notes = cfg["status_uncertain"], f"Bautin {today}. Submit clicked, no confirmation seen; verify by hand. {res.get('message', '')}"
    out = upsert(cfg, row, status, today if res["status"] == "submitted" else None, notes, tok)
    out.update({"pushed": True, "status": status, "counter": refresh_counter(cfg, tok) if res["status"] == "submitted" else None})
    return out


def queue_row(root: Path, qid: str) -> Optional[dict]:
    f = root / "state" / "internships" / "queue.md"
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 8 and cells[0] == qid:
            flags = cells[7].split()
            tier = next((t for t in flags if t in ("faang", "known", "unknown")), "")
            return {"id": qid, "found": cells[1], "company": cells[2], "role": cells[3], "location": cells[4], "url": cells[5],
                    "tier": tier, "source": "simplify"}
    return None


def push_queued(root: Path, cfg: dict, tok: Optional[str] = None) -> dict:
    urls = {r["url_key"] for r in load_applied(root).get("rows", []) if r.get("url_key")}
    keys = {r["key"] for r in load_applied(root).get("rows", []) if r.get("key")}
    created = []
    f = root / "state" / "internships" / "queue.md"
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines() if f.exists() else []:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 8 or not re.fullmatch(r"q\d+", cells[0]):
            continue
        row = queue_row(root, cells[0]) or {}
        if norm_url(row.get("url", "")) in urls or norm_key(row.get("company", ""), row.get("role", "")) in keys:
            continue
        row["posted"] = row.get("found")
        upsert(cfg, row, cfg["status_queued"], None, f"Bautin queued {date.today().isoformat()} ({cells[0]})", tok)
        created.append(cells[0])
    return {"created": len(created), "ids": created}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pull", action="store_true")
    g.add_argument("--push", metavar="RESULT_JSON")
    g.add_argument("--push-queued", action="store_true")
    args = ap.parse_args(argv)
    root = vault_root(args.vault)
    cfg = load_cfg(root)
    try:
        if args.pull:
            out = pull(root, cfg)
            print(json.dumps({"pulled": out["count"], "by_status": out["by_status"]}))
        elif args.push:
            print(json.dumps(push_result(root, cfg, Path(args.push))))
        else:
            print(json.dumps(push_queued(root, cfg)))
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
