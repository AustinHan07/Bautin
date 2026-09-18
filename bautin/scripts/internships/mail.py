#!/usr/bin/env python3
"""Bautin internships lane: Gmail reader over IMAP (app password), host side.

    mail.py --verify --domain corteva.com [--since-min 30]   newest verification link/code from that sender domain
    mail.py --emploive [--since-hours 3]                       parse Emploive alert emails into source rows
    mail.py --list [--since-hours 24]                          debug: subjects of recent mail

Credentials: GMAIL_ADDRESS and GMAIL_APP_PASSWORD from the environment, else $HERMES_HOME/.env.
Spaces in the app password are ignored. Output is one JSON object on stdout. Standard library only.
Emploive rows are written to <vault>/state/internships/sources/emploive.json for discover.py.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from typing import Any, Optional

IMAP_HOST = "imap.gmail.com"
VERIFY_WORDS = re.compile(r"verif|confirm|activate|one[- ]time|passcode|security code|magic link|sign[- ]in link", re.I)
CODE_RE = re.compile(r"(?<![\w-])(\d{4,8})(?![\w-])")
LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)


def creds() -> tuple[str, str]:
    addr, pw = os.environ.get("GMAIL_ADDRESS", "").strip(), os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (addr and pw):
        env = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("GMAIL_ADDRESS=") and not addr:
                    addr = line.split("=", 1)[1].strip().strip("'\"")
                if line.startswith("GMAIL_APP_PASSWORD=") and not pw:
                    pw = line.split("=", 1)[1].strip().strip("'\"")
    pw = pw.replace(" ", "")
    if not (addr and pw):
        sys.exit("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set")
    return addr, pw


def connect(addr: str, pw: str) -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(IMAP_HOST)
    m.login(addr, pw)
    m.select("INBOX", readonly=True)
    return m


def search_recent(m: imaplib.IMAP4_SSL, since: datetime, extra: str = "") -> list[bytes]:
    crit = f'(SINCE "{since.strftime("%d-%b-%Y")}"{(" " + extra) if extra else ""})'
    typ, data = m.search(None, crit)
    return data[0].split() if typ == "OK" and data and data[0] else []


def fetch(m: imaplib.IMAP4_SSL, uid: bytes) -> EmailMessage:
    typ, data = m.fetch(uid, "(RFC822)")
    raw = next((d[1] for d in data if isinstance(d, tuple)), b"")
    return email.message_from_bytes(raw, policy=email.policy.default)


def bodies(msg: EmailMessage) -> tuple[str, str]:
    """(plain text, html) of a message."""
    text, html = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            payload = part.get_content() if ct.startswith("text/") else ""
        except Exception:
            payload = ""
        if ct == "text/plain":
            text += payload
        elif ct == "text/html":
            html += payload
    return text, html


def html_to_text(h: str) -> str:
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", h, flags=re.I)
    return unescape(re.sub(r"<[^>]+>", " ", h))


def msg_date(msg: EmailMessage) -> datetime:
    try:
        d = email.utils.parsedate_to_datetime(msg["Date"])
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


# ── verification links / codes ───────────────────────────────────────────────

def extract_verification(msg: EmailMessage) -> dict:
    text, html = bodies(msg)
    subject = str(msg.get("Subject", ""))
    corpus = text + "\n" + html_to_text(html)
    links = []
    for u in LINK_RE.findall(html or text):
        u = unescape(u.rstrip(".,;"))
        if re.search(r"unsubscribe|privacy|terms|preferences|facebook|twitter|linkedin\.com/company|instagram", u, re.I):
            continue
        score = 2 if VERIFY_WORDS.search(u) else (1 if re.search(r"token|code|key=|otp|auth|confirm", u, re.I) else 0)
        links.append((score, u))
    links.sort(key=lambda s: -s[0])
    code = None
    for m in CODE_RE.finditer(corpus):
        window = corpus[max(0, m.start() - 80):m.end() + 20]
        if VERIFY_WORDS.search(window):
            code = m.group(1); break
    return {"subject": subject, "from": str(msg.get("From", "")), "date": msg_date(msg).isoformat(timespec="seconds"),
            "link": links[0][1] if links else None, "links": [u for _, u in links[:5]], "code": code,
            "looks_like_verification": bool(VERIFY_WORDS.search(subject) or VERIFY_WORDS.search(corpus[:2000]))}


def find_verification(m: imaplib.IMAP4_SSL, domain: str, since_min: int) -> Optional[dict]:
    since = datetime.now(timezone.utc) - timedelta(minutes=since_min)
    uids = search_recent(m, since - timedelta(days=1), f'FROM "{domain}"') if domain else search_recent(m, since - timedelta(days=1))
    best = None
    for uid in reversed(uids[-30:]):
        msg = fetch(m, uid)
        if msg_date(msg) < since:
            continue
        v = extract_verification(msg)
        if v["looks_like_verification"] and (v["link"] or v["code"]):
            if best is None or v["date"] > best["date"]:
                best = v
    return best


# ── Emploive alerts ──────────────────────────────────────────────────────────

def resolve_link(url: str, cache: Optional[dict] = None) -> str:
    """Follow tracking redirects (Emploive/SendGrid) to the real posting URL; cached per tracking URL."""
    if cache is not None and url in cache:
        return cache[url]
    final = url
    try:
        import urllib.request
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
    except Exception:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                final = r.geturl()
        except Exception:
            final = url
    if cache is not None:
        cache[url] = final
    return final


def parse_emploive(msg: EmailMessage, resolve: bool = True, cache: Optional[dict] = None) -> list[dict]:
    """Rows from one Emploive "N jobs matched your trackers" alert.
    Layout: a role link ending in "↗", then a line "Company · City, State, Country"."""
    text, html = bodies(msg)
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    rows: list[dict] = []
    for a in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S | re.I):
        href, inner = unescape(a.group(1)), html_to_text(a.group(2)).strip()
        role = inner.replace("↗", "").strip()
        if not role or re.search(r"open trackers|change how often|pause alerts|unsubscribe|^emploive\.com$|see all", inner, re.I):
            continue
        after = html_to_text(body[a.end():a.end() + 1500])
        nxt = next((ln.strip() for ln in after.splitlines() if ln.strip()), "")
        if "·" not in nxt:
            continue
        company, _, location = (x.strip() for x in nxt.partition("·"))
        url = resolve_link(href, cache) if resolve else href
        rows.append({"company": company[:80], "role": role[:120], "location": location[:100], "url": url, "tracking_url": href,
                     "age_days": 0, "source": "emploive", "faang": False, "closed": False, "no_sponsor": False,
                     "citizenship": False, "adv_degree": False, "section": "emploive-email", "subject": str(msg.get("Subject", ""))[:80]})
    seen = set(); out = []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"]); out.append(r)
    return out


def collect_emploive(m: imaplib.IMAP4_SSL, since_hours: int, vault: Optional[Path] = None) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    cache_file = (vault / "state" / "internships" / "sources" / "emploive-links.json") if vault else None
    cache: dict = {}
    if cache_file and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    rows = []
    for uid in reversed(search_recent(m, since - timedelta(days=1), 'FROM "emploive"')[-40:]):
        msg = fetch(m, uid)
        if msg_date(msg) >= since:
            rows += parse_emploive(msg, cache=cache)
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, indent=0) + "\n", encoding="utf-8")
    return rows


def write_source(vault: Path, name: str, rows: list[dict]) -> Path:
    d = vault / "state" / "internships" / "sources"; d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.json"
    f.write_text(json.dumps({"written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rows": rows}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return f


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true"); g.add_argument("--emploive", action="store_true"); g.add_argument("--list", action="store_true")
    ap.add_argument("--domain", default=""); ap.add_argument("--since-min", type=int, default=30); ap.add_argument("--since-hours", type=int, default=3)
    ap.add_argument("--vault", default=os.environ.get("BAUTIN_VAULT", "/vault"))
    args = ap.parse_args(argv)
    addr, pw = creds()
    try:
        m = connect(addr, pw)
    except imaplib.IMAP4.error as e:
        print(json.dumps({"error": f"imap login failed: {e}"})); return 1
    try:
        if args.verify:
            v = find_verification(m, args.domain, args.since_min)
            print(json.dumps(v or {"found": False, "domain": args.domain, "since_min": args.since_min}))
        elif args.emploive:
            rows = collect_emploive(m, args.since_hours, Path(args.vault))
            f = write_source(Path(args.vault), "emploive", rows)
            print(json.dumps({"rows": len(rows), "file": str(f)}))
        else:
            since = datetime.now(timezone.utc) - timedelta(hours=args.since_hours)
            out = []
            for uid in reversed(search_recent(m, since - timedelta(days=1))[-40:]):
                msg = fetch(m, uid)
                if msg_date(msg) >= since:
                    out.append({"from": str(msg.get("From", ""))[:60], "subject": str(msg.get("Subject", ""))[:90], "date": msg_date(msg).isoformat(timespec="minutes")})
            print(json.dumps(out, indent=1))
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
