#!/usr/bin/env python3
"""Open a verification link in the sandbox browser, click a Verify/Confirm button if the page shows one, and report.

    open_link.py --url <link> [--id q17] [--vault /vault]

Prints JSON {"status": "opened"|"confirmed"|"error", "final_url", "title", "screenshot"}. Never fills forms.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--url", required=True); ap.add_argument("--id", default="link"); ap.add_argument("--vault", default="/vault")
    a = ap.parse_args(argv)
    out = {"status": "error", "url": a.url}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = b.new_page(viewport={"width": 1200, "height": 1600})
            page.goto(a.url, wait_until="domcontentloaded", timeout=45000); page.wait_for_timeout(2000)
            out["status"] = "opened"
            btn = page.locator("button:has-text('Verify'), button:has-text('Confirm'), a:has-text('Verify'), a:has-text('Confirm'), input[type=submit][value*=erif], input[type=submit][value*=onfirm]").first
            if btn.count():
                btn.click(); page.wait_for_timeout(2500); out["status"] = "confirmed"
            shots = Path(a.vault) / "state" / "internships" / "drafts"; shots.mkdir(parents=True, exist_ok=True)
            shot = shots / f"{a.id}-verify-{datetime.now(timezone.utc).strftime('%H%M%S')}.png"
            page.screenshot(path=str(shot), full_page=True)
            out.update({"final_url": page.url, "title": page.title()[:120], "screenshot": str(shot.relative_to(Path(a.vault))),
                        "text_hint": re.sub(r"\s+", " ", page.inner_text("body"))[:300]})
            b.close()
    except Exception as exc:
        out["message"] = str(exc)[:300]
    print(json.dumps(out)); return 0 if out["status"] != "error" else 1

if __name__ == "__main__":
    sys.exit(main())
