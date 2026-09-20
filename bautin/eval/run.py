#!/usr/bin/env python3
"""The Bautin release gate.

Run this before changing the model, the skill, the guards, or before turning ``auto_apply`` on.
It answers one question: is the agent still allowed to do only what it should?

    python3 bautin/eval/run.py              # tier 1 only: free, instant, no network
    python3 bautin/eval/run.py --live       # + real model turns, judged (costs a few cents)
    python3 bautin/eval/run.py --live --only "asks a question"

Exit code is 0 only when every scenario held, so it can gate a commit or a config change.
A dated report is written to the vault under ``log/<lane>/eval/`` when one is configured.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios  # noqa: E402

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def _colour(ok: bool, text: str, tty: bool) -> str:
    if not tty:
        return text
    return f"{GREEN if ok else RED}{text}{RESET}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="also run real model turns (costs money, needs network)")
    ap.add_argument("--only", default="", help="substring filter for live scenario names")
    ap.add_argument("--profile", default="", help="profile home for live runs (default: the internships profile)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    tty = sys.stdout.isatty()

    rows = []
    for v in scenarios.run_all():
        rows.append({"tier": 1, "name": v.name, "rule": v.rule, "ok": v.ok, "detail": v.detail})

    if args.live:
        import live  # noqa: E402  (imported lazily: tier 1 must not need the network)
        profile = Path(args.profile).expanduser() if args.profile else live.DEFAULT_PROFILE
        for v in live.run_live(profile=profile, only=args.only):
            rows.append({"tier": 2, "name": v.name, "rule": v.rule, "ok": v.ok, "detail": v.detail,
                         "reply": v.reply[:1200]})

    failed = [r for r in rows if not r["ok"]]
    report = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "live": bool(args.live), "total": len(rows), "failed": len(failed), "scenarios": rows}

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        tier = None
        for r in rows:
            if r["tier"] != tier:
                tier = r["tier"]
                label = "guards and memory, no model" if tier == 1 else "real model turns, judged"
                print(f"\n  Tier {tier}  {DIM if tty else ''}{label}{RESET if tty else ''}")
            print(f"    {_colour(r['ok'], 'PASS' if r['ok'] else 'FAIL', tty)}  {r['name']}")
            if not r["ok"]:
                print(f"          {r['rule']}")
                print(f"          -> {r['detail']}")
        verdict = "RELEASE GATE PASSED" if not failed else f"RELEASE GATE FAILED ({len(failed)} of {len(rows)})"
        print(f"\n  {_colour(not failed, verdict, tty)}\n")

    _write_report(report)
    return 1 if failed else 0


def _write_report(report: dict) -> None:
    """Keep a dated trace next to the lane's other logs, when a vault is configured."""
    try:
        import yaml
        home = Path.home() / ".hermes" / "profiles" / "internships"
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        raw = ((cfg.get("bautin") or {}).get("vault") or "").strip()
        if not raw:
            return
        out = Path(raw).expanduser() / "log" / "internships" / "eval"
        out.mkdir(parents=True, exist_ok=True)
        stamp = report["ran_at"].replace(":", "").replace("-", "")
        (out / f"{stamp}-{'live' if report['live'] else 'tier1'}.json").write_text(
            json.dumps(report, indent=1), encoding="utf-8")
    except Exception:
        pass  # a missing report never fails the gate


if __name__ == "__main__":
    sys.exit(main())
