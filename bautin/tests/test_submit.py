"""Unit tests for submit.py's pure parts (no browser), plus an opt-in browser test that runs the
real Playwright flow against tests/fixtures/greenhouse_form.html. Set BAUTIN_BROWSER_TESTS=1 to
enable the browser test (it needs Playwright + Chromium, i.e. the bautin-sandbox image)."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("submit", ROOT / "scripts" / "internships" / "submit.py")
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)
FIX = ROOT / "tests" / "fixtures"


def make_vault(td: str, url: str) -> Path:
    v = Path(td) / "vault"
    (v / "state" / "internships" / "drafts").mkdir(parents=True)
    (v / "state" / "internships" / "approvals").mkdir()
    refs = v / "skills" / "internships" / "internship-pipeline" / "references"; refs.mkdir(parents=True)
    (refs / "resume.pdf").write_bytes(b"%PDF-1.4 fake resume for tests\n")
    (v / "state" / "internships" / "queue.md").write_text(
        "| id | found | company | role | location | link | score | reason |\n|---|---|---|---|---|---|---|---|\n"
        f"| q1 | 2026-09-18 | Robinhood | Software Engineer Intern - Backend | Menlo Park, CA | {url} | 4 | faang |\n"
        "| q2 | 2026-09-18 | Visa | Software Engineer Intern | Austin, TX | https://visa.wd5.myworkdayjobs.com/x |  | known |\n")
    (v / "state" / "internships" / "tracker.md").write_text("| date | company | role | location | link | status | confirmation | follow-up | notes |\n|---|---|---|---|---|---|---|---|---|\n")
    shutil.copy(FIX / "draft_q1.md", v / "state" / "internships" / "drafts" / "q1.md")
    return v


class PureTests(unittest.TestCase):
    def test_family_detection(self):
        self.assertEqual(submit.detect_family("https://boards.greenhouse.io/robinhood/jobs/1"), "greenhouse")
        self.assertEqual(submit.detect_family("https://job-boards.greenhouse.io/x/jobs/1"), "greenhouse")
        self.assertEqual(submit.detect_family("https://jobs.lever.co/x/uuid/apply"), "lever")
        self.assertEqual(submit.detect_family("https://jobs.ashbyhq.com/x/uuid/application"), "ashby")
        self.assertIsNone(submit.detect_family("https://visa.wd5.myworkdayjobs.com/x"))

    def test_draft_parsing_and_answers(self):
        d = submit.parse_draft((FIX / "draft_q1.md").read_text())
        self.assertEqual(d["fields"]["full_name"], "Austin Han")
        self.assertEqual(submit.answer_for("First Name *", d), ("Austin", "first_name"))
        self.assertEqual(submit.answer_for("Email", d), ("austin@example.com", "email"))
        self.assertEqual(submit.answer_for("Are you legally authorized to work in the United States? *", d), ("yes", "work_authorization"))
        self.assertEqual(submit.answer_for("Will you now or in the future require sponsorship for employment visa status?", d), ("no", "sponsorship"))
        self.assertEqual(submit.answer_for("Gender", d), ("decline", "eeo_gender"))          # no answer in draft -> decline
        d2 = submit.parse_draft("---\neeo_gender: male\neeo_veteran: not a veteran\nus_citizen: yes\n---\n")
        self.assertEqual(submit.answer_for("Gender", d2), ("male", "eeo_gender"))
        self.assertEqual(submit.answer_for("Veteran Status", d2), ("not a veteran", "eeo_veteran"))
        self.assertEqual(submit.answer_for("Are you a U.S. citizen?", d2), ("yes", "us_citizen"))
        self.assertEqual(submit.answer_for("Are you at least 18 years of age?", d2), ("yes", "over_18"))
        self.assertEqual(submit.answer_for("Do you have any relatives employed by Robinhood?", d2), ("no", "relatives_employed"))
        d3 = submit.parse_draft("---\nstreet_address: 889 Francisco St\nzip: 90017\nbirth_date: 2007-08-28\nsalary_if_required: 30\npreferred_locations: Menlo Park; San Francisco; New York\n---\n")
        self.assertEqual(submit.answer_for("Street Address", d3), ("889 Francisco St", "street_address"))
        self.assertEqual(submit.answer_for("Zip / Postal Code", d3), ("90017", "zip"))
        self.assertEqual(submit.answer_for("Date of Birth", d3), ("2007-08-28", "birth_date"))
        self.assertEqual(submit.answer_for("Desired salary", d3), (None, "salary"))            # blank unless required
        self.assertEqual(submit.answer_for("Preferred office location", d3)[1], "preferred_locations")
        self.assertEqual(submit.answer_for("Are you willing to complete an online assessment?", d3), ("yes", "assessment"))
        self.assertEqual(submit.answer_for("Do you require any accommodations for the interview?", d3), ("no", "accommodation"))
        self.assertEqual(submit.answer_for("Why do you want to work at Robinhood? *", d)[1], "custom")
        self.assertIn("agent that reads job boards", submit.answer_for("Tell us about a project you're proud of", d)[0])
        self.assertEqual(submit.answer_for("What is your favorite dessert?", d), (None, ""))
        self.assertEqual(submit.answer_for("School", d), ("University of Wisconsin-Madison", "school"))

    def test_run_blocks_rows_already_done_in_notion(self):
        with tempfile.TemporaryDirectory() as td:
            url = "https://boards.greenhouse.io/robinhood/jobs/1"
            v = make_vault(td, url)
            sys.path.insert(0, str(ROOT / "scripts" / "internships")); import notion_sync
            (v / "state" / "internships" / "notion-applied.json").write_text(json.dumps({"rows": [
                {"url_key": notion_sync.norm_url(url + "?utm_source=x"), "key": "robinhood other role", "status": "Applied"},
                {"url_key": "https://elsewhere.example/1", "key": notion_sync.norm_key("Visa", "Software Engineer Intern"), "status": "Skipped"}]}))
            r = submit.run(v, "q1", False)
            self.assertEqual(r["status"], "blocked"); self.assertIn("already Applied", r["message"])
            self.assertEqual(submit.run(v, "q2", False)["status"], "blocked")           # matched by company+role, before family detection
            (v / "state" / "internships" / "notion-applied.json").write_text(json.dumps({"rows": [
                {"url_key": notion_sync.norm_url(url), "key": "x", "status": "Queued"}]}))
            self.assertEqual(submit.run(v, "q2", False)["status"], "unsupported")       # Queued is not done

    def test_run_refuses_without_row_draft_or_approval(self):
        with tempfile.TemporaryDirectory() as td:
            v = make_vault(td, "https://boards.greenhouse.io/robinhood/jobs/1")
            self.assertEqual(submit.run(v, "q9", False)["status"], "error")
            self.assertEqual(submit.run(v, "q2", False)["status"], "unsupported")
            (v / "state" / "internships" / "drafts" / "q1.md").unlink()
            self.assertIn("draft missing", submit.run(v, "q1", False)["message"])
            shutil.copy(FIX / "draft_q1.md", v / "state" / "internships" / "drafts" / "q1.md")
            self.assertEqual(submit.run(v, "q1", True)["status"], "blocked")


@unittest.skipUnless(os.environ.get("BAUTIN_BROWSER_TESTS") == "1", "set BAUTIN_BROWSER_TESTS=1 (needs Playwright + Chromium)")
class BrowserTests(unittest.TestCase):
    def test_dry_run_then_submit_on_fixture_form(self):
        with tempfile.TemporaryDirectory() as td:
            url = (FIX / "greenhouse_form.html").resolve().as_uri()
            v = make_vault(td, url)
            r = submit.run(v, "q1", False, family="greenhouse")
            self.assertEqual(r["status"], "dry-run", r)
            labels = {f["label"] for f in r["filled"]}
            for want in ("First Name *", "Email *", "Resume/CV *", "School *", "Graduation Year", "LinkedIn Profile",
                         "Why do you want to work at Robinhood? *", "Gender"):
                self.assertIn(want, labels, r)
            self.assertEqual([u["label"] for u in r["unfilled_required"]], ["What is your favorite dessert? *"])
            filled = {f["label"]: f["value"] for f in r["filled"]}
            self.assertEqual(filled["Desired hourly rate *"], "30")                    # required -> salary_if_required
            self.assertEqual(filled["Referral name (optional)"], "(left blank on purpose)")
            self.assertTrue((v / r["screenshot"]).exists())
            self.assertFalse(r["captcha"])
            # submit path: blocked without marker (checked before the browser opens), then refuses on unfilled required
            (v / "state" / "internships" / "approvals" / "q1.json").write_text(json.dumps({"id": "q1", "url": url}))
            r2 = submit.run(v, "q1", True, family="greenhouse")
            self.assertEqual(r2["status"], "incomplete", r2)
            # answer the last question in the draft and submit for real against the fixture
            with (v / "state" / "internships" / "drafts" / "q1.md").open("a") as fh:
                fh.write("\n## What is your favorite dessert?\nTiramisu\n")
            r3 = submit.run(v, "q1", True, family="greenhouse")
            self.assertEqual(r3["status"], "submitted", r3)
            self.assertIn("Thank you for applying", r3["confirmation"])
            self.assertIn("| Robinhood | Software Engineer Intern - Backend |", (v / "state" / "internships" / "tracker.md").read_text())
            self.assertTrue((v / "state" / "internships" / "drafts" / "q1-after-submit.png").exists())


if __name__ == "__main__":
    unittest.main()
