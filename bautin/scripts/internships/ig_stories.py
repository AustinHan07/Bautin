#!/usr/bin/env python3
"""Bautin internships lane: read an Instagram account's current stories with a saved session (headless).

    ig_stories.py --user zero2sudo --state /ig/instagram-storage.json [--vault /vault] [--max 40]

Writes new story frames to <vault>/state/internships/ig/<YYYY-MM-DD>/<story_id>.png and records them in
<vault>/state/internships/ig/seen.json (id -> {date, frame, links}). Prints one JSON line:
{"new": [{"id","frame","links"}...], "total_seen": n} or {"error": "session expired"} (exit 2).
Runs inside the Bautin sandbox image (Playwright + Chromium); the session file is mounted read-only.
Never bypasses checkpoints: any login/challenge page is reported and the run stops.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
STORY_ID_RE = re.compile(r"/stories/[^/]+/(\d+)/?")


def story_id(url: str) -> str:
    m = STORY_ID_RE.search(url or "")
    return m.group(1) if m else ""


def external_links(hrefs: list[str]) -> list[str]:
    out = []
    for h in hrefs:
        if not h or h.startswith("#"):
            continue
        if re.search(r"instagram\.com|facebook\.com|threads\.net|/accounts/|/direct/|/explore/|/reels?/", h):
            # Instagram wraps link stickers as https://l.instagram.com/?u=<encoded>; unwrap those
            m = re.search(r"[?&]u=([^&]+)", h)
            if "l.instagram.com" in h and m:
                import urllib.parse
                out.append(urllib.parse.unquote(m.group(1)))
            continue
        out.append(h)
    return list(dict.fromkeys(out))


def load_seen(vault: Path) -> dict:
    f = vault / "state" / "internships" / "ig" / "seen.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_seen(vault: Path, seen: dict) -> None:
    f = vault / "state" / "internships" / "ig" / "seen.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def run(user: str, state: Path, vault: Path, max_stories: int) -> dict:
    from playwright.sync_api import sync_playwright
    seen = load_seen(vault)
    today = date.today().isoformat()
    outdir = vault / "state" / "internships" / "ig" / today
    new: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(storage_state=str(state), user_agent=UA, viewport={"width": 1100, "height": 900}, locale="en-US")
        page = ctx.new_page()
        try:
            page.goto(f"https://www.instagram.com/stories/{user}/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            if re.search(r"/accounts/login|/challenge/|/checkpoint/", page.url):
                return {"error": "session expired", "url": page.url}
            btn = page.get_by_role("button", name=re.compile(r"view story", re.I))
            if btn.count():
                btn.first.click()
                try:
                    page.wait_for_url(re.compile(r"/stories/[^/]+/\d+"), timeout=10000)
                except Exception:
                    page.wait_for_timeout(2500)
            in_viewer = bool(story_id(page.url)) or page.locator("[role=button]:has-text('Next'), [aria-label='Next']").count() > 0
            if not in_viewer:
                # no active stories: Instagram bounces to the profile
                return {"new": [], "total_seen": len(seen), "note": "no active stories", "url": page.url}
            last = ""
            for idx in range(max_stories):
                sid = story_id(page.url) or f"{user}-{today}-{idx}"
                if sid == last:
                    break
                last = sid
                page.wait_for_timeout(1200)
                hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                links = external_links(hrefs)
                if sid not in seen:
                    outdir.mkdir(parents=True, exist_ok=True)
                    frame = outdir / f"{sid}.png"
                    page.screenshot(path=str(frame))
                    rel = str(frame.relative_to(vault))
                    seen[sid] = {"date": today, "frame": rel, "links": links, "user": user}
                    new.append({"id": sid, "frame": rel, "links": links})
                nxt = page.locator("[aria-label='Next'], [role=button]:has-text('Next')").first
                if nxt.count():
                    nxt.click()
                else:
                    page.keyboard.press("ArrowRight")
                page.wait_for_timeout(1200)
                if not re.search(r"/stories/", page.url):
                    break
        finally:
            browser.close()
    save_seen(vault, seen)
    return {"new": new, "total_seen": len(seen)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", default="zero2sudo"); ap.add_argument("--state", required=True)
    ap.add_argument("--vault", default="/vault"); ap.add_argument("--max", type=int, default=40)
    a = ap.parse_args(argv)
    state = Path(a.state)
    if not state.exists():
        print(json.dumps({"error": f"session file missing: {state}"})); return 2
    try:
        out = run(a.user, state, Path(a.vault), a.max)
    except Exception as exc:  # report, never crash the cron pre-check
        out = {"error": str(exc)[:300]}
    print(json.dumps(out))
    return 2 if "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())
