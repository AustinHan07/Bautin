#!/usr/bin/env python3
"""Bautin internships lane: deterministic discovery of new internship postings.

Sources
  1. SimplifyJobs Summer 2027 README (public HTML tables).
  2. Company boards on Greenhouse, Lever, Ashby listed in <vault>/state/internships/boards.md.

Reads   <vault>/state/internships/filters.md   (frontmatter: role/company/location rules)
        <vault>/state/internships/seen.json    (dedupe store)
Writes  <vault>/state/internships/queue.md     (appends new rows, score left blank)
        <vault>/state/internships/seen.json

Standard library only; runs inside the Hermes docker sandbox where the vault is /vault.
Exit 0 on success (even when nothing is new), 1 on a fetch or vault error.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md"
UA = "bautin-discover/1.0 (+https://github.com/AustinHan07/Bautin)"

FLAG_CLOSED, FLAG_NO_SPONSOR, FLAG_CITIZEN, FLAG_ADV_DEGREE, FLAG_FAANG = "🔒", "🛂", "🇺🇸", "🎓", "🔥"

DEFAULT_FILTERS = {
    "sections": ["Software Engineering", "Data Science, AI & Machine Learning"],
    "include_roles": ["software", "swe", "sde", "frontend", "front-end", "front end", "backend", "back-end",
                      "back end", "full stack", "full-stack", "fullstack", "platform", "infrastructure",
                      "mobile", "ios", "android", "ai engineer", "applied ai", "agent", "llm",
                      "machine learning", "ml engineer", "ml intern", "developer", "engineering intern"],
    "exclude_roles": ["data scientist", "data science", "analyst", "hardware", "electrical", "mechanical",
                      "quant", "product manager", "designer", "design intern", "phd", "research scientist",
                      "sales", "marketing", "finance", "security clearance", "mba"],
    "exclude_companies": [],
    "us_only": True,
    "max_age_days": 14,
    "skip_advanced_degree": True,
    "queue_unknown_tier": False,
    "known_companies": [],
}

US_TOKENS = {"sf", "nyc", "la", "remote", "remote in usa", "usa", "united states", "us", "bay area",
             "silicon valley", "dc", "washington, dc"}
NON_US_MARKERS = ["canada", "united kingdom", " uk", "london", "india", "germany", "ireland", "dublin",
                  "singapore", "israel", "australia", "france", "paris", "netherlands", "amsterdam", "japan",
                  "china", "toronto", "vancouver", "montreal", "mexico", "brazil", "poland", "spain", "sweden",
                  "switzerland", "zurich", "remote in canada", "remote in europe", "bangalore", "bengaluru"]


# ── vault helpers ─────────────────────────────────────────────────────────────

def vault_root(arg: str | None) -> Path:
    root = Path(arg or os.environ.get("BAUTIN_VAULT") or "/vault")
    if not (root / "state" / "internships").is_dir():
        sys.exit(f"vault not found or missing state/internships: {root}")
    return root


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML-ish frontmatter: scalars, inline lists [a, b], block lists (- item)."""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out: dict = {}
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
        elif val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        elif re.fullmatch(r"-?\d+", val):
            out[key] = int(val)
        else:
            out[key] = val.strip("'\"")
    return out


def load_filters(root: Path) -> dict:
    f = root / "state" / "internships" / "filters.md"
    cfg = dict(DEFAULT_FILTERS)
    if f.exists():
        cfg.update({k: v for k, v in parse_frontmatter(f.read_text(encoding="utf-8")).items() if k in cfg})
    return cfg


def load_seen(root: Path) -> dict:
    f = root / "state" / "internships" / "seen.json"
    try:
        return json.loads(f.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def save_seen(root: Path, seen: dict) -> None:
    f = root / "state" / "internships" / "seen.json"
    f.write_text(json.dumps(seen, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def next_queue_id(root: Path) -> int:
    f = root / "state" / "internships" / "queue.md"
    ids = [int(m) for m in re.findall(r"^\|\s*q(\d+)\s*\|", f.read_text(encoding="utf-8"), re.M)] if f.exists() else []
    return (max(ids) + 1) if ids else 1


def append_queue(root: Path, rows: list[dict]) -> None:
    f = root / "state" / "internships" / "queue.md"
    lines = []
    qid = next_queue_id(root)
    for r in rows:
        r["id"] = f"q{qid}"; qid += 1
        flags = " ".join(x for x in (r.get("tier"), r.get("citizenship") and "citizenship",
                                     r.get("no_sponsor") and "no-sponsor") if x)
        loc = r["location"].replace("|", "/")
        lines.append(f"| {r['id']} | {r['found']} | {r['company']} | {r['role']} | {loc} | {r['url']} |  | {flags} |")
    with f.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ── fetching ──────────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed public URLs)
        return resp.read().decode("utf-8", errors="replace")


def clean_url(url: str) -> str:
    p = urllib.parse.urlsplit(html.unescape(url))
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() != "ref"]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


# ── filtering ─────────────────────────────────────────────────────────────────

def strip_tags(s: str) -> str:
    s = re.sub(r"<summary>.*?</summary>", "", s, flags=re.S)  # "<N locations>" toggles
    s = re.sub(r"<br\s*/?>", " / ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def parse_age_days(s: str) -> int:
    m = re.search(r"(\d+)\s*(h|d|w|mo|m|y)", s.strip().lower())
    if not m:
        return 0
    n, unit = int(m.group(1)), m.group(2)
    return {"h": 0, "d": n, "w": 7 * n, "mo": 30 * n, "m": 30 * n, "y": 365 * n}[unit]


def role_ok(role: str, cfg: dict) -> bool:
    r = role.lower()
    if any(x in r for x in cfg["exclude_roles"]):
        return False
    return any(x in r for x in cfg["include_roles"])


def location_ok(location: str, cfg: dict) -> bool:
    if not cfg.get("us_only", True):
        return True
    tokens = [t.strip() for t in re.split(r"\s*/\s*|\s*;\s*", location) if t.strip()]
    if not tokens:
        return True  # unknown location: let the model judge
    for t in tokens:
        tl = t.lower()
        if any(mk in f" {tl}" for mk in NON_US_MARKERS):
            continue
        if tl in US_TOKENS or re.search(r",\s*[A-Z]{2}\b", t) or "usa" in tl or "united states" in tl or "remote" in tl:
            return True
    return False


def key_for(company: str, role: str, location: str) -> str:
    return "|".join(x.lower().strip() for x in (company, role, location.split(" / ")[0]))


def _norm(s: str) -> str:
    toks = re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
    out: list[str] = []
    for t in toks:                       # merge runs of single letters: "d e shaw" -> "de shaw"
        if len(t) == 1 and out and len(out[-1]) <= 2 and out[-1].isalpha() and (len(out[-1]) == 1 or out[-1] in _MERGED):
            out[-1] += t; _MERGED.add(out[-1])
        else:
            out.append(t)
    return " ".join(out)


_MERGED: set[str] = set()


def company_tier(company: str, faang: bool, cfg: dict) -> str:
    """'faang' (list flag), 'known' (whole-word match in known_companies), else 'unknown'."""
    if faang:
        return "faang"
    c = f" {_norm(company)} "
    for k in cfg.get("known_companies", []):
        kn = _norm(k)
        if kn and f" {kn} " in c:
            return "known"
    return "unknown"


# ── source 1: SimplifyJobs README ─────────────────────────────────────────────

def parse_simplify(md: str, cfg: dict) -> list[dict]:
    """Yield candidate rows from the sections named in cfg['sections']."""
    rows: list[dict] = []
    sections = re.split(r"^## ", md, flags=re.M)
    for sec in sections[1:]:
        title = strip_tags(sec.splitlines()[0])
        if not any(s.lower() in title.lower() for s in cfg["sections"]):
            continue
        last_company = ""
        for tr in re.findall(r"<tr>(.*?)</tr>", sec, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 5:
                continue
            c_raw, role_raw, loc_raw, app_raw, age_raw = tds[:5]
            company = strip_tags(c_raw).replace(FLAG_FAANG, "").strip()
            if company.startswith("↳") or company == "":
                company = last_company
            else:
                last_company = company
            role = strip_tags(role_raw)
            flags = set(re.findall("|".join(map(re.escape, (FLAG_CLOSED, FLAG_NO_SPONSOR, FLAG_CITIZEN,
                                                             FLAG_ADV_DEGREE, FLAG_FAANG))), c_raw + role_raw))
            for fl in (FLAG_CLOSED, FLAG_NO_SPONSOR, FLAG_CITIZEN, FLAG_ADV_DEGREE, FLAG_FAANG):
                role = role.replace(fl, "").strip()
            hrefs = [h for h in re.findall(r'href="([^"]+)"', app_raw) if "simplify.jobs/p/" not in h]
            url = clean_url(hrefs[0]) if hrefs else ""
            rows.append({
                "company": company, "role": role, "location": strip_tags(loc_raw), "url": url,
                "age_days": parse_age_days(strip_tags(age_raw)), "section": title,
                "closed": FLAG_CLOSED in flags, "no_sponsor": FLAG_NO_SPONSOR in flags,
                "citizenship": FLAG_CITIZEN in flags, "adv_degree": FLAG_ADV_DEGREE in flags,
                "faang": FLAG_FAANG in flags, "source": "simplify",
            })
    return rows


# ── source 2: company boards ──────────────────────────────────────────────────

def load_boards(root: Path) -> list[tuple[str, str, str]]:
    f = root / "state" / "internships" / "boards.md"
    out = []
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[1].lower() in ("greenhouse", "lever", "ashby") and cells[2] and not cells[2].startswith("-"):
            out.append((cells[0], cells[1].lower(), cells[2]))
    return out


def fetch_board(company: str, family: str, slug: str) -> list[dict]:
    rows = []
    if family == "greenhouse":
        data = json.loads(fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"))
        for j in data.get("jobs", []):
            rows.append({"company": company, "role": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
                         "url": clean_url(j.get("absolute_url", "")), "age_days": 0})
    elif family == "lever":
        data = json.loads(fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json"))
        for j in data:
            rows.append({"company": company, "role": j.get("text", ""), "location": (j.get("categories") or {}).get("location", ""),
                         "url": clean_url(j.get("hostedUrl", "")), "age_days": 0})
    elif family == "ashby":
        data = json.loads(fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}"))
        for j in data.get("jobs", []):
            rows.append({"company": company, "role": j.get("title", ""), "location": j.get("location", ""),
                         "url": clean_url(j.get("jobUrl", "")), "age_days": 0})
    for r in rows:
        r.update({"section": f"board:{family}", "closed": False, "no_sponsor": False, "citizenship": False,
                  "adv_degree": False, "faang": False, "source": family})
    return [r for r in rows if "intern" in r["role"].lower()]


# ── main ──────────────────────────────────────────────────────────────────────

def select_new(rows: list[dict], cfg: dict, seen: dict, today: str) -> tuple[list[dict], int]:
    """Return (new rows to queue, count marked seen-but-skipped)."""
    fresh, skipped = [], 0
    excl = {c.lower() for c in cfg.get("exclude_companies", [])}
    for r in rows:
        k = key_for(r["company"], r["role"], r["location"])
        if k in seen:
            continue
        r["tier"] = company_tier(r["company"], r["faang"], cfg)
        tier_ok = r["tier"] != "unknown" or cfg.get("queue_unknown_tier", False)
        ok = (not r["closed"] and r["url"] and role_ok(r["role"], cfg) and location_ok(r["location"], cfg)
              and r["company"].lower() not in excl and not (cfg.get("skip_advanced_degree", True) and r["adv_degree"])
              and r["age_days"] <= int(cfg.get("max_age_days", 14)))
        seen[k] = {"first_seen": today, "company": r["company"], "role": r["role"], "url": r["url"],
                   "location": r["location"], "queued": bool(ok and tier_ok), "tier": r["tier"],
                   "source": r["source"], "fit": bool(ok)}
        if ok and not tier_ok:
            skipped += 1
            continue
        if ok:
            r["found"] = today
            fresh.append(r)
        else:
            skipped += 1
    return fresh, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", help="vault root (default: $BAUTIN_VAULT or /vault)")
    ap.add_argument("--source", choices=["simplify", "boards", "all"], default="all")
    ap.add_argument("--monitor", action="store_true", help="print JSON {wakeAgent,...} for hermes cron --monitor-script")
    ap.add_argument("--dry-run", action="store_true", help="fetch and filter, write nothing")
    ap.add_argument("--bootstrap", action="store_true", help="mark everything currently listed as seen without queuing")
    ap.add_argument("--list-unknown", action="store_true", help="print unknown-tier companies with fitting roles that were held back")
    ap.add_argument("--requeue", metavar="COMPANY", action="append", default=[],
                    help="queue held-back postings from COMPANY (after adding it to known_companies); repeatable")
    args = ap.parse_args(argv)

    root = vault_root(args.vault)
    cfg = load_filters(root)
    seen = load_seen(root)
    today = date.today().isoformat()
    rows: list[dict] = []
    errors: list[str] = []

    if args.list_unknown:
        held: dict[str, int] = {}
        for v in seen.values():
            if v.get("tier") == "unknown" and v.get("fit") and not v.get("queued"):
                held[v["company"]] = held.get(v["company"], 0) + 1
        for c, n in sorted(held.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"{n:3d}  {c}")
        print(f"{len(held)} unknown-tier companies held back; add names to known_companies in filters.md, then --requeue NAME")
        return 0
    if args.requeue:
        wanted = {_norm(c) for c in args.requeue}
        fresh = []
        for v in seen.values():
            if _norm(v.get("company", "")) in wanted and v.get("fit") and not v.get("queued"):
                v["queued"] = True
                v["tier"] = company_tier(v["company"], False, cfg)
                fresh.append({"found": today, "company": v["company"], "role": v["role"], "location": v.get("location", ""),
                              "url": v["url"], "tier": v["tier"], "citizenship": False, "no_sponsor": False})
        if fresh and not args.dry_run:
            append_queue(root, fresh); save_seen(root, seen)
        print(f"{len(fresh)} postings requeued")
        return 0

    if args.source in ("simplify", "all"):
        try:
            rows += parse_simplify(fetch(SIMPLIFY_URL), cfg)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            errors.append(f"simplify: {e}")
    if args.source in ("boards", "all"):
        for company, family, slug in load_boards(root):
            try:
                rows += fetch_board(company, family, slug)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                errors.append(f"{family}/{slug}: {e}")

    if args.bootstrap:
        for r in rows:
            seen.setdefault(key_for(r["company"], r["role"], r["location"]),
                            {"first_seen": today, "company": r["company"], "role": r["role"], "url": r["url"],
                             "queued": False, "source": r["source"], "bootstrap": True})
        fresh, skipped = [], len(rows)
    else:
        fresh, skipped = select_new(rows, cfg, seen, today)

    if not args.dry_run:
        if fresh:
            append_queue(root, fresh)
        save_seen(root, seen)

    summary = f"{len(rows)} listed, {len(fresh)} new queued, {skipped} skipped" + (f", errors: {'; '.join(errors)}" if errors else "")
    if args.monitor:
        print(json.dumps({"wakeAgent": bool(fresh), "new": len(fresh), "summary": summary,
                          "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
    else:
        print(summary)
        for r in fresh[:50]:
            print(f"  + {r['company']} — {r['role']} — {r['location']} — {r['url']}")
    return 1 if (errors and not rows) else 0


if __name__ == "__main__":
    sys.exit(main())
