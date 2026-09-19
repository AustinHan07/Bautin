#!/usr/bin/env python3
"""Bautin internships lane: deterministic application form filler (Greenhouse, Lever, Ashby).

    submit.py --id q17            dry run: open the posting, fill every field it can, upload the resume,
                                  screenshot, report what is filled and what is not. Submits nothing.
    submit.py --id q17 --submit   real submission. Requires an approval marker written by the lane guard
                                  from Austin's own "apply q17" reply (and the guard blocks this flag otherwise).

Reads   <vault>/state/internships/queue.md            the posting url for the id
        <vault>/state/internships/drafts/<id>.md      answers: standard fields in frontmatter, custom
                                                      questions as "## <question>" sections
        <vault>/skills/internships/internship-pipeline/references/resume.pdf
Writes  <vault>/state/internships/drafts/<id>-<mode>.png       screenshot
        <vault>/state/internships/drafts/<id>-result.json      the same JSON printed to stdout
        <vault>/state/internships/tracker.md                   one row appended after a real submission

Never bypasses a captcha: if one is present the result is status "captcha" and the form is left to Austin.
Runs inside the Bautin sandbox image (Playwright + Chromium). Exit 0 with a JSON report, 2 on usage errors.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

FAMILIES = {
    "greenhouse": re.compile(r"greenhouse\.io", re.I),
    "lever": re.compile(r"jobs\.lever\.co", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com", re.I),
}
SUCCESS_RE = re.compile(r"thank you|application (?:has been )?(?:submitted|received)|received your application|successfully submitted", re.I)
CAPTCHA_SEL = "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], .h-captcha, .g-recaptcha, [data-sitekey]"
DECLINE_RE = re.compile(r"decline|prefer not|don.t wish|do not wish|not to (?:answer|say)|rather not", re.I)

# label pattern -> answer key in the draft frontmatter (first match wins; order matters)
LABEL_RULES: list[tuple[str, str]] = [
    (r"^first\s*name", "first_name"), (r"^last\s*name|surname|family name", "last_name"),
    (r"^(full\s*)?name$|^your name", "full_name"), (r"preferred (first )?name", "preferred_name"),
    (r"e-?mail", "email"), (r"phone|mobile", "phone"),
    (r"linkedin", "linkedin"), (r"github", "github"), (r"website|portfolio|personal site", "website"),
    (r"current (company|employer)|^company$|organization", "current_company"), (r"current (title|position|role)|job title", "current_title"),
    (r"date of birth|birth ?date|\bdob\b", "birth_date"), (r"zip|postal", "zip"), (r"street|address line|mailing address|home address", "street_address"),
    (r"salary|compensation|hourly|pay rate|wage|expected pay", "salary"),
    (r"preferred (office|location)|office (preference|location)|location preference|which (office|location)", "preferred_locations"),
    (r"time ?zone", "timezone"), (r"accommodat", "accommodation"), (r"assessment|coding (test|challenge)|online test", "assessment"),
    (r"export control|u\.?s\.? person", "us_person"), (r"referr(al|ed by)|employee referral", "referral"),
    (r"location|city|where (are you|do you) (based|live)|address", "location"),
    (r"school|university|college", "school"), (r"degree", "degree"), (r"major|discipline|field of study", "discipline"),
    (r"(graduation|end).*(month)", "grad_month"), (r"(graduation|end).*(year)|graduat", "grad_year"),
    (r"start date|available to start|availability", "start_date"),
    (r"sponsor", "sponsorship"), (r"authori[sz]ed|work authorization|legally", "work_authorization"),
    (r"how did you hear|referr|source", "how_heard"), (r"pronoun", "pronouns"),
    (r"hispanic|latin", "eeo_hispanic"), (r"sexual orientation|orientation", "eeo_orientation"),
    (r"gender|sex\b", "eeo_gender"), (r"race|ethnic", "eeo_race"), (r"veteran", "eeo_veteran"), (r"disabilit", "eeo_disability"),
    (r"18 years|age of 18|at least 18|over 18", "over_18"), (r"relative|family member.*(employ|work)", "relatives_employed"),
    (r"previously (worked|employed|applied)|former employee|worked (here|for us)", "previously_applied"),
    (r"security clearance|clearance", "clearance"), (r"non-?compete|restrictive covenant", "noncompete"),
    (r"background check|drug (screen|test)", "consent_background"), (r"(text|sms) messag", "consent_sms"),
    (r"relocat", "willing_to_relocate"), (r"citizen", "us_citizen"),
    (r"resume|cv\b", "resume"), (r"cover letter", "cover_letter"),
]
YES_KEYS = {"work_authorization", "over_18", "consent_background", "willing_to_relocate", "us_citizen", "assessment", "us_person"}
NO_KEYS = {"sponsorship", "relatives_employed", "previously_applied", "clearance", "noncompete", "accommodation"}
BLANK_UNLESS_REQUIRED = {"salary": "salary_if_required", "referral": None, "cover_letter": "cover_letter"}
# Demographic questions: use the draft's stated answer; fall back to declining only when no answer is given.
DECLINE_KEYS = {"eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability", "eeo_hispanic", "eeo_orientation"}


# ── vault io ─────────────────────────────────────────────────────────────────

def vault_root(arg: Optional[str]) -> Path:
    root = Path(arg or os.environ.get("BAUTIN_VAULT") or "/vault")
    if not (root / "state" / "internships").is_dir():
        sys.exit(f"vault not found or missing state/internships: {root}")
    return root


def queue_row(root: Path, qid: str) -> Optional[dict]:
    f = root / "state" / "internships" / "queue.md"
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6 and cells[0] == qid:
            return {"id": qid, "found": cells[1], "company": cells[2], "role": cells[3], "location": cells[4], "url": cells[5]}
    return None


def parse_draft(text: str) -> dict:
    """Frontmatter scalars -> fields; '## question' sections -> custom answers (normalized question -> answer)."""
    fields: dict[str, str] = {}
    custom: dict[str, str] = {}
    body = text
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                fields[k.strip().lower()] = v.strip().strip("'\"")
    for q, a in re.findall(r"^##\s+(.+?)\n(.*?)(?=^##\s|\Z)", body, re.S | re.M):
        if a.strip():
            custom[norm(q)] = a.strip()
    if "full_name" not in fields and fields.get("first_name") and fields.get("last_name"):
        fields["full_name"] = f"{fields['first_name']} {fields['last_name']}"
    return {"fields": fields, "custom": custom}


def norm(s: str) -> str:
    s = re.sub(r"\*|\(required\)|\(optional\)", " ", s or "", flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def detect_family(url: str) -> Optional[str]:
    return next((f for f, rx in FAMILIES.items() if rx.search(url or "")), None)


def notion_done_status(root: Path, row: dict) -> Optional[str]:
    """Status from the local Notion export when this posting is already Applied/Skipped/etc., else None.
    Deterministic replacement for the model reading notion-applied.json (105 KB) on every apply."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from notion_sync import DONE_DEFAULT, load_applied, norm_key, norm_url
    except Exception:
        return None
    url_key, key = norm_url(row.get("url", "")), norm_key(row.get("company", ""), row.get("role", ""))
    for r in load_applied(root).get("rows", []):
        if r.get("status") in DONE_DEFAULT and (r.get("url_key") == url_key or (key and r.get("key") == key)):
            return str(r["status"])
    return None


def approval(root: Path, qid: str) -> Optional[dict]:
    f = root / "state" / "internships" / "approvals" / f"{qid}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


# ── answer resolution ────────────────────────────────────────────────────────

def answer_for(label: str, draft: dict) -> tuple[Optional[str], str]:
    """(answer, key) for a form label; custom '## question' answers win over generic field rules."""
    nl = norm(label)
    if not nl:
        return None, ""
    custom = draft["custom"]
    if nl in custom:
        return custom[nl], "custom"
    for q, a in custom.items():
        if (q in nl or nl in q) and min(len(q), len(nl)) >= 12:
            return a, "custom"
    toks = set(nl.split())
    best = max(((len(toks & set(q.split())) / len(toks | set(q.split())), q) for q in custom), default=(0, ""))
    if best[0] >= 0.6:
        return custom[best[1]], "custom"
    for pattern, key in LABEL_RULES:
        if re.search(pattern, nl, re.I):
            f = draft["fields"]
            if key in YES_KEYS:
                return f.get(key, "yes"), key
            if key in NO_KEYS:
                return f.get(key, "no"), key
            if key in DECLINE_KEYS:
                return f.get(key) or "decline", key
            return f.get(key), key
    return None, ""


# ── browser driving (imported lazily so unit tests need no Playwright) ───────

def _control_label(page: Any, el: Any) -> str:
    """Best available label for a control: <label for>, aria-label/labelledby, placeholder, nearest heading."""
    try:
        return el.evaluate("""el => {
            const byFor = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            if (byFor && byFor.innerText.trim()) return byFor.innerText;
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            const lb = el.getAttribute('aria-labelledby');
            if (lb) { const t = lb.split(/\\s+/).map(i => (document.getElementById(i)||{}).innerText||'').join(' ').trim(); if (t) return t; }
            const wrap = el.closest('label'); if (wrap && wrap.innerText.trim()) return wrap.innerText;
            if (el.placeholder) return el.placeholder;
            let n = el; for (let i = 0; i < 6 && n; i++) { n = n.parentElement; if (!n) break;
              const lab = n.querySelector('label, legend, .application-label, .field-label, h3, h4, [class*=label]');
              if (lab && lab.innerText.trim()) return lab.innerText; }
            return el.name || '';
        }""") or ""
    except Exception:
        return ""


def _is_required(el: Any) -> bool:
    try:
        return bool(el.evaluate("el => el.required || el.getAttribute('aria-required') === 'true' || /\\*/.test((el.closest('label, .field, .application-question, div')||{}).innerText||'')"))
    except Exception:
        return False


def _fill_select(el: Any, answer: str) -> bool:
    options = el.evaluate("el => Array.from(el.options).map(o => [o.value, o.textContent.trim()])")
    want = answer.lower()
    pick = None
    for value, text in options:
        t = text.lower()
        if not value and not text:
            continue
        if want == t or want == value.lower():
            pick = value; break
    if pick is None:
        for value, text in options:
            t = text.lower()
            if (want and want in t) or (want == "decline" and DECLINE_RE.search(text)) \
               or (want in ("yes", "no") and t.startswith(want)) \
               or (want.startswith("not a") and ("not a" in t or t.startswith("i am not") or t.startswith("no,"))) \
               or (want == "male" and t in ("male", "man")) or (want == "female" and t in ("female", "woman")) \
               or (want in ("heterosexual", "straight") and ("heterosexual" in t or "straight" in t)):
                pick = value; break
    if pick is None:
        return False
    el.select_option(pick)
    return True


def fill_form(page: Any, draft: dict, resume: Optional[Path], report: dict) -> None:
    controls = page.locator("input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select")
    n = controls.count()
    seen_file = False
    for i in range(n):
        el = controls.nth(i)
        try:
            if not el.is_visible() and el.get_attribute("type") != "file":
                continue
            typ = (el.get_attribute("type") or ("select" if el.evaluate("e => e.tagName") == "SELECT" else "text")).lower()
            label = _control_label(page, el).strip()
            required = _is_required(el)
            entry = {"label": label[:80], "type": typ, "required": required}
            if typ == "file":
                if resume and resume.exists() and not seen_file and re.search(r"resume|cv|upload|attach", (label + " " + (el.get_attribute("name") or "")), re.I):
                    el.set_input_files(str(resume)); seen_file = True
                    report["filled"].append({**entry, "value": resume.name})
                elif required:
                    report["unfilled_required"].append(entry)
                continue
            if typ in ("checkbox",):
                if re.search(r"agree|consent|acknowledge|certify|confirm|privacy|terms", label, re.I):
                    el.check(); report["filled"].append({**entry, "value": "checked"})
                continue
            if typ == "radio":
                answer, key = answer_for(label, draft)
                val = (el.get_attribute("value") or "").lower()
                if answer and (val == answer.lower() or val.startswith(answer.lower()[:3])):
                    el.check(); report["filled"].append({**entry, "value": answer})
                continue
            answer, key = answer_for(label, draft)
            if key in BLANK_UNLESS_REQUIRED:
                fallback = BLANK_UNLESS_REQUIRED[key]
                answer = (draft["fields"].get(fallback) if fallback else None) if required else None
                if not required:
                    report["filled"].append({**entry, "value": "(left blank on purpose)"}); continue
            if not answer:
                (report["unfilled_required"] if required else report["unfilled_optional"]).append(entry)
                continue
            if typ == "select":
                ok = any(_fill_select(el, cand.strip()) for cand in answer.split(";") if cand.strip())
            else:
                el.fill(answer.split(";")[0].strip() if key == "preferred_locations" else answer); ok = True
                if el.get_attribute("role") == "combobox" or "react-select" in (el.get_attribute("class") or "") or (el.get_attribute("aria-autocomplete") or ""):
                    page.wait_for_timeout(600)
                    try:
                        page.keyboard.press("Enter")
                    except Exception:
                        pass
            if ok:
                report["filled"].append({**entry, "value": answer[:60]})
            else:
                (report["unfilled_required"] if required else report["unfilled_optional"]).append({**entry, "wanted": answer[:60]})
        except Exception as exc:  # one bad control never aborts the run
            report["errors"].append(f"control {i}: {exc}"[:200])


def run(root: Path, qid: str, submit: bool, headed: bool = False, family: Optional[str] = None) -> dict:
    row = queue_row(root, qid)
    if not row:
        return {"status": "error", "id": qid, "message": f"no queue row {qid} in queue.md"}
    done = notion_done_status(root, row)
    if done:
        return {"status": "blocked", "id": qid, "url": row["url"], "company": row["company"], "role": row["role"],
                "message": f"already {done} in Austin's Notion tracker; never apply twice. Tell Austin and skip {qid}."}
    family = family or detect_family(row["url"])
    if not family:
        return {"status": "unsupported", "id": qid, "url": row["url"],
                "message": "not a Greenhouse/Lever/Ashby form; give Austin the link and the prefilled answers"}
    draft_path = root / "state" / "internships" / "drafts" / f"{qid}.md"
    if not draft_path.exists():
        return {"status": "error", "id": qid, "message": f"draft missing: {draft_path.relative_to(root)}"}
    draft = parse_draft(draft_path.read_text(encoding="utf-8", errors="replace"))
    if submit and not approval(root, qid):
        return {"status": "blocked", "id": qid, "message": f"no approval marker for {qid}; Austin must reply 'apply {qid}'"}
    resume = root / "skills" / "internships" / "internship-pipeline" / "references" / "resume.pdf"
    mode = "submit" if submit else "dryrun"
    shots = root / "state" / "internships" / "drafts"
    report: dict[str, Any] = {"status": "dry-run", "id": qid, "family": family, "url": row["url"], "company": row["company"],
                              "role": row["role"], "mode": mode, "filled": [], "unfilled_required": [], "unfilled_optional": [],
                              "errors": [], "captcha": False, "resume": resume.exists(), "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1280, "height": 2000})
        try:
            page.goto(row["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            if family == "lever" and not re.search(r"/apply/?$", page.url):
                link = page.locator("a[href$='/apply'], a:has-text('Apply for this job')").first
                if link.count():
                    link.click(); page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(1000)
            if family == "greenhouse":
                btn = page.locator("a:has-text('Apply'), button:has-text('Apply')").first
                if btn.count() and page.locator("#first_name, input[name*=first_name]").count() == 0:
                    btn.click(); page.wait_for_timeout(1500)
            fill_form(page, draft, resume, report)
            report["captcha"] = page.locator(CAPTCHA_SEL).count() > 0
            shot = shots / f"{qid}-{mode}.png"
            page.screenshot(path=str(shot), full_page=True)
            report["screenshot"] = str(shot.relative_to(root))
            if submit:
                if report["captcha"]:
                    report["status"] = "captcha"
                    report["message"] = "a captcha guards this form; Austin must submit by hand with the prefilled answers"
                elif report["unfilled_required"]:
                    report["status"] = "incomplete"
                    report["message"] = "required fields could not be filled; not submitting"
                else:
                    btn = page.locator("button:has-text('Submit application'), button:has-text('Submit Application'), "
                                       "button[type=submit], input[type=submit], #btn-submit, button:has-text('Submit')").first
                    btn.click()
                    try:
                        page.wait_for_function("t => new RegExp(t, 'i').test(document.body.innerText)", arg=SUCCESS_RE.pattern, timeout=20000)
                        report["status"] = "submitted"
                        body = page.inner_text("body")
                        line = next((ln.strip() for ln in body.splitlines() if SUCCESS_RE.search(ln)), "")
                        report["confirmation"] = line[:160]
                    except Exception:
                        report["status"] = "uncertain"
                        report["message"] = "clicked submit but no confirmation text appeared; verify by hand before retrying"
                    page.wait_for_timeout(1000)
                    page.screenshot(path=str(shots / f"{qid}-after-submit.png"), full_page=True)
        except Exception as exc:
            report["status"] = "error"; report["message"] = str(exc)[:300]
        finally:
            browser.close()
    if submit and report["status"] in ("submitted", "uncertain"):
        with (root / "state" / "internships" / "tracker.md").open("a", encoding="utf-8") as fh:
            fh.write(f"| {date.today().isoformat()} | {row['company']} | {row['role']} | {row['location']} | {row['url']} | "
                     f"{report['status']} | {report.get('confirmation', '')} | {date.today().replace(day=min(28, date.today().day)).isoformat()} | {qid} |\n")
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--id", required=True, help="queue id, e.g. q17")
    ap.add_argument("--submit", action="store_true", help="really submit (needs an approval marker)")
    ap.add_argument("--vault", help="vault root (default $BAUTIN_VAULT or /vault)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--family", choices=sorted(FAMILIES), help="force the form family (tests and odd career-site URLs)")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"q\d+", args.id):
        ap.error("--id must look like q17")
    root = vault_root(args.vault)
    report = run(root, args.id, args.submit, args.headed, args.family)
    out = root / "state" / "internships" / "drafts" / f"{args.id}-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
