#!/usr/bin/env python3
"""Build the Instagram session file from cookies copied out of a normal browser (no automated login).

Put these lines in $HERMES_HOME/.env after logging in with the SEPARATE account in Chrome or Safari
(DevTools > Application > Cookies > https://www.instagram.com):

    INSTAGRAM_SESSIONID=<value of sessionid>
    INSTAGRAM_CSRFTOKEN=<value of csrftoken>
    INSTAGRAM_DS_USER_ID=<value of ds_user_id>

Then run:  python3 bautin/scripts/internships/ig_session.py
Writes $HERMES_HOME/state/instagram-storage.json (chmod 600) in Playwright storage-state format.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def read_env(home: Path) -> dict:
    vals = {k: os.environ.get(k, "").strip() for k in ("INSTAGRAM_SESSIONID", "INSTAGRAM_CSRFTOKEN", "INSTAGRAM_DS_USER_ID")}
    env = home / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            for k in vals:
                if line.startswith(k + "=") and not vals[k]:
                    vals[k] = line.split("=", 1)[1].strip().strip("'\"")
    return vals


def build_state(vals: dict) -> dict:
    exp = int(time.time()) + 365 * 24 * 3600
    cookies = []
    for name, key in (("sessionid", "INSTAGRAM_SESSIONID"), ("csrftoken", "INSTAGRAM_CSRFTOKEN"), ("ds_user_id", "INSTAGRAM_DS_USER_ID")):
        if vals.get(key):
            cookies.append({"name": name, "value": vals[key], "domain": ".instagram.com", "path": "/", "expires": exp,
                            "httpOnly": name == "sessionid", "secure": True, "sameSite": "Lax"})
    return {"cookies": cookies, "origins": []}


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes" / "profiles" / "internships")
    vals = read_env(home)
    if not vals["INSTAGRAM_SESSIONID"]:
        print("INSTAGRAM_SESSIONID missing in", home / ".env"); return 1
    state = home / "state" / "instagram-storage.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(build_state(vals), indent=1) + "\n", encoding="utf-8")
    os.chmod(state, 0o600)
    print(f"wrote {state} with {len(build_state(vals)['cookies'])} cookies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
