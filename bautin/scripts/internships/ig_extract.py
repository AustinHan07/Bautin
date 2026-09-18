#!/usr/bin/env python3
"""Bautin internships lane: turn new Instagram story frames into posting rows with the cheap vision model (host side).

    ig_extract.py [--vault /path] [--model gpt-5.6-luna]

Reads <vault>/state/internships/ig/seen.json, sends every frame not yet extracted to the OpenAI Responses API
(OPENAI_API_KEY from the environment or $HERMES_HOME/.env), and appends rows to
<vault>/state/internships/sources/zero2sudo.json (url = a link sticker when present, else "") and
leads without a URL to <vault>/state/internships/leads.md for the agent to resolve. Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROMPT = ("This image is an Instagram story from a recruiting-tips account that announces internship application openings. "
          "Extract every internship posting it mentions. Return ONLY JSON of the form "
          '{"postings":[{"company":"","role":"","location":"","url":"","note":""}]} '
          "with url empty unless a URL is legible in the image, role empty if only the company is named, and "
          '{"postings":[]} when the story is not about a specific internship posting. Never invent companies.')


def api_key() -> str:
    k = os.environ.get("OPENAI_API_KEY", "").strip()
    if k:
        return k
    env = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("OPENAI_API_KEY not set")


def call_vision(png_bytes: bytes, model: str, key: str) -> dict:
    b64 = base64.b64encode(png_bytes).decode()
    body = {"model": model, "input": [{"role": "user", "content": [
        {"type": "input_text", "text": PROMPT},
        {"type": "input_image", "image_url": f"data:image/png;base64,{b64}", "detail": "low"}]}],
        "max_output_tokens": 400}
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def response_text(resp: dict) -> str:
    if resp.get("output_text"):
        return resp["output_text"]
    parts = []
    for item in resp.get("output", []):
        for c in item.get("content", []) if isinstance(item, dict) else []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts)


def parse_postings(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for p in data.get("postings", []) if isinstance(data, dict) else []:
        if isinstance(p, dict) and (p.get("company") or "").strip():
            out.append({k: str(p.get(k, "") or "").strip() for k in ("company", "role", "location", "url", "note")})
    return out


def merge_rows(existing: list[dict], new: list[dict]) -> list[dict]:
    keyset = {(r.get("company", "").lower(), r.get("role", "").lower(), r.get("url", "")) for r in existing}
    out = list(existing)
    for r in new:
        k = (r.get("company", "").lower(), r.get("role", "").lower(), r.get("url", ""))
        if k not in keyset:
            keyset.add(k); out.append(r)
    return out


def extract(vault: Path, model: str, key: Optional[str], caller=call_vision) -> dict:
    seen_f = vault / "state" / "internships" / "ig" / "seen.json"
    seen = json.loads(seen_f.read_text(encoding="utf-8")) if seen_f.exists() else {}
    src_f = vault / "state" / "internships" / "sources" / "zero2sudo.json"
    src = json.loads(src_f.read_text(encoding="utf-8")) if src_f.exists() else {"rows": []}
    leads_f = vault / "state" / "internships" / "leads.md"
    rows_new, leads, done = [], [], 0
    for sid, rec in sorted(seen.items()):
        if rec.get("extracted"):
            continue
        frame = vault / rec["frame"]
        if not frame.exists():
            rec["extracted"] = "missing"; continue
        try:
            postings = parse_postings(response_text(caller(frame.read_bytes(), model, key or "")))
        except Exception as exc:
            rec["extracted_error"] = str(exc)[:200]; continue
        for p in postings:
            url = p["url"] or (rec.get("links") or [""])[0]
            row = {"company": p["company"], "role": p["role"] or "Internship (see story)", "location": p["location"] or "United States",
                   "url": url, "age_days": 0, "source": "zero2sudo", "faang": False, "closed": False, "no_sponsor": False,
                   "citizenship": False, "adv_degree": False, "section": "zero2sudo-story", "story": sid, "note": p["note"], "date": rec.get("date")}
            rows_new.append(row)
            if not url:
                leads.append(row)
        rec["extracted"] = datetime.now(timezone.utc).isoformat(timespec="seconds"); rec["postings"] = len(postings); done += 1
    src["rows"] = merge_rows(src.get("rows", []), rows_new)
    src["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    src_f.parent.mkdir(parents=True, exist_ok=True)
    src_f.write_text(json.dumps(src, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    seen_f.parent.mkdir(parents=True, exist_ok=True)
    seen_f.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if leads:
        if not leads_f.exists():
            leads_f.write_text("---\nlane: internships\ndate: %s\nsource: agent\ntype: state\n---\n# Leads without a link (find the posting on the company board)\n\n| date | company | role | note | story |\n|---|---|---|---|---|\n" % date.today().isoformat(), encoding="utf-8")
        with leads_f.open("a", encoding="utf-8") as fh:
            for r in leads:
                fh.write(f"| {r['date']} | {r['company']} | {r['role']} | {r['note'].replace('|', '/')} | {r['story']} |\n")
    return {"frames": done, "rows": len(rows_new), "leads": len(leads)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", default=os.environ.get("BAUTIN_VAULT", "/vault")); ap.add_argument("--model", default="gpt-5.6-luna")
    a = ap.parse_args(argv)
    print(json.dumps(extract(Path(a.vault), a.model, api_key())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
