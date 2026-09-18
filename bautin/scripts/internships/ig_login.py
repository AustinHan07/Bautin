#!/usr/bin/env python3
"""One-time Instagram login for the Bautin story checker (run on the Mac, visible window).

    ~/.hermes/profiles/internships/tools/pw/bin/python bautin/scripts/internships/ig_login.py

Opens a real Chromium window on instagram.com. Log in with the SEPARATE account (never the main one),
answer any verification prompt, and wait for the home feed. The script then saves the session to
$HERMES_HOME/state/instagram-storage.json (chmod 600) and exits. Re-run whenever the checker reports
"session expired". Nothing is typed by the script; no password is stored anywhere.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    from playwright.sync_api import sync_playwright
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes" / "profiles" / "internships")
    state = Path(sys.argv[1]) if len(sys.argv) > 1 else home / "state" / "instagram-storage.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1100, "height": 900}, locale="en-US")
        page = ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        print("Log in with the separate account in the window. Waiting up to 10 minutes for the session cookie...")
        deadline = time.time() + 600
        ok = False
        while time.time() < deadline:
            if any(c["name"] == "sessionid" and c["value"] for c in ctx.cookies("https://www.instagram.com")):
                ok = True
                break
            time.sleep(2)
        if not ok:
            print("No session detected; nothing saved.")
            browser.close()
            return 1
        time.sleep(5)  # let post-login redirects settle
        ctx.storage_state(path=str(state))
        os.chmod(state, 0o600)
        browser.close()
    print(f"Session saved to {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
