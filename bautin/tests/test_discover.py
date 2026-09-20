"""Unit tests for bautin/scripts/internships/discover.py (stdlib unittest, no network)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("discover", ROOT / "scripts" / "internships" / "discover.py")
discover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discover)

FIXTURE = """
# Summer 2027 Tech Internships

## 💻 Software Engineering Internship Roles

<table><tbody>
<tr>
<td>🔥 <strong><a href="https://simplify.jobs/c/Tesla?utm_source=GHList">Tesla</a></strong></td>
<td>Internship - Software Engineering - Summer 2027</td>
<td>Palo Alto, CA</td>
<td><div align="center"><a href="https://www.tesla.com/careers/job/1?utm_source=Simplify&ref=Simplify"><img alt="Apply"></a> <a href="https://simplify.jobs/p/abc">S</a></div></td>
<td>0d</td>
</tr>
<tr>
<td>↳</td>
<td>Data Scientist Intern</td>
<td>Austin, TX</td>
<td><a href="https://www.tesla.com/careers/job/2?utm_source=Simplify">Apply</a></td>
<td>3d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Shopify">Shopify</a></strong></td>
<td>Backend Developer Intern</td>
<td>Toronto, ON, Canada</td>
<td><a href="https://shopify.com/careers/3">Apply</a></td>
<td>1d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Acme">Acme</a></strong></td>
<td>Software Engineer Intern 🔒</td>
<td>NYC</td>
<td><a href="https://acme.com/jobs/4">Apply</a></td>
<td>2d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Old">OldCo</a></strong></td>
<td>Software Engineer Intern</td>
<td><details><summary><strong>2 locations</strong></summary>Seattle, WA<br>NYC</details></td>
<td><a href="https://old.co/jobs/5?gh_jid=77&utm_medium=x">Apply</a></td>
<td>2mo</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Lab">LabCo</a></strong></td>
<td>ML Engineer Intern 🎓</td>
<td>Remote in USA</td>
<td><a href="https://lab.co/jobs/6">Apply</a></td>
<td>1d</td>
</tr>
</tbody></table>

## 📈 Quantitative Finance Internship Roles

<table><tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/HRT">HRT</a></strong></td>
<td>Software Engineer Intern</td>
<td>NYC</td>
<td><a href="https://hrt.com/jobs/7">Apply</a></td>
<td>0d</td>
</tr>
</tbody></table>
"""


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(discover.DEFAULT_FILTERS)
        self.rows = discover.parse_simplify(FIXTURE, self.cfg)

    def test_parses_only_configured_sections(self):
        self.assertEqual({r["section"] for r in self.rows}, {"💻 Software Engineering Internship Roles"})
        self.assertEqual(len(self.rows), 6)

    def test_continuation_row_inherits_company_and_flags(self):
        tesla = [r for r in self.rows if r["company"] == "Tesla"]
        self.assertEqual(len(tesla), 2)
        self.assertTrue(tesla[0]["faang"])
        self.assertEqual(tesla[0]["url"], "https://www.tesla.com/careers/job/1")

    def test_flags_and_url_cleaning(self):
        acme = next(r for r in self.rows if r["company"] == "Acme")
        self.assertTrue(acme["closed"])
        self.assertEqual(acme["role"], "Software Engineer Intern")
        old = next(r for r in self.rows if r["company"] == "OldCo")
        self.assertEqual(old["url"], "https://old.co/jobs/5?gh_jid=77")
        self.assertEqual(old["age_days"], 60)
        self.assertEqual(old["location"], "Seattle, WA / NYC")

    def test_selection_rules(self):
        fresh, skipped = discover.select_new(self.rows, self.cfg, {}, "2026-09-18")
        kept = {r["company"] for r in fresh}
        self.assertEqual(kept, {"Tesla"})          # SWE, US, open, fresh, no advanced degree
        self.assertEqual(skipped, 5)               # data scientist, Canada, closed, too old, advanced degree

    def test_seen_dedupes_on_second_run(self):
        seen = {}
        discover.select_new(self.rows, self.cfg, seen, "2026-09-18")
        fresh, _ = discover.select_new(self.rows, self.cfg, seen, "2026-09-19")
        self.assertEqual(fresh, [])
        self.assertEqual(len(seen), 6)

    def test_rows_done_in_notion_are_not_queued(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "state" / "internships").mkdir(parents=True)
            (root / "state" / "internships" / "notion-applied.json").write_text(json.dumps({"rows": [
                {"url_key": "https://tesla.com/careers/job/1", "key": "tesla internship software engineering summer 2027", "status": "Applied"}]}))
            fresh, skipped = discover.select_new(self.rows, self.cfg, {}, "2026-09-18", root)
            self.assertEqual(fresh, [])
            self.assertEqual(skipped, 6)

    def test_extra_sources_flow_through_filters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "state" / "internships" / "sources").mkdir(parents=True)
            (root / "state" / "internships" / "sources" / "emploive.json").write_text(json.dumps({"rows": [
                {"company": "Stripe", "role": "Software Engineer Intern", "location": "SF", "url": "https://stripe.com/jobs/1?utm_source=x", "source": "emploive"},
                {"company": "Stripe", "role": "Data Scientist Intern", "location": "SF", "url": "https://stripe.com/jobs/2", "source": "emploive"},
                {"company": "Nobody Inc", "role": "Software Engineer Intern", "location": "NYC", "url": "https://nobody.com/1", "source": "emploive"}]}))
            rows = discover.load_extra_sources(root)
            self.assertEqual(len(rows), 3); self.assertEqual(rows[0]["url"], "https://stripe.com/jobs/1"); self.assertEqual(rows[0]["source"], "emploive")
            cfg = dict(self.cfg, known_companies=["Stripe"])
            fresh, _ = discover.select_new(rows, cfg, {}, "2026-09-18", root)
            self.assertEqual([(r["company"], r["role"]) for r in fresh], [("Stripe", "Software Engineer Intern")])

    def test_frontmatter_parser(self):
        fm = discover.parse_frontmatter("---\nlane: x\nus_only: false\nmax_age_days: 7\ninclude_roles: [a, b]\nexclude_roles:\n  - c\n  - d\n---\nbody")
        self.assertEqual(fm, {"lane": "x", "us_only": False, "max_age_days": 7, "include_roles": ["a", "b"], "exclude_roles": ["c", "d"]})

    def test_company_tier(self):
        cfg = dict(self.cfg, known_companies=["GE", "Wells Fargo", "D. E. Shaw"])
        self.assertEqual(discover.company_tier("Tesla", True, cfg), "faang")
        self.assertEqual(discover.company_tier("GE Aerospace", False, cfg), "known")
        self.assertEqual(discover.company_tier("Geneva Trading", False, cfg), "unknown")
        self.assertEqual(discover.company_tier("DE Shaw", False, cfg), "known")
        self.assertEqual(discover.company_tier("wells fargo bank", False, cfg), "known")

    def test_unknown_tier_held_back_and_requeued(self):
        cfg = dict(self.cfg, known_companies=[])
        seen = {}
        fresh, _ = discover.select_new(self.rows, cfg, seen, "2026-09-18")
        self.assertEqual({r["company"] for r in fresh}, {"Tesla"})      # faang passes; nothing else known
        rows2 = discover.parse_simplify(FIXTURE.replace("2mo", "1d"), cfg)   # OldCo now fresh but unknown tier
        seen2 = {}
        fresh, _ = discover.select_new(rows2, cfg, seen2, "2026-09-18")
        self.assertEqual({r["company"] for r in fresh}, {"Tesla"})
        held = [v for v in seen2.values() if v["tier"] == "unknown" and v["fit"] and not v["queued"]]
        self.assertEqual([v["company"] for v in held], ["OldCo"])

    def test_location_rules(self):
        self.assertTrue(discover.location_ok("SF", self.cfg))
        self.assertTrue(discover.location_ok("Remote in USA", self.cfg))
        self.assertTrue(discover.location_ok("London, UK / Austin, TX", self.cfg))
        self.assertFalse(discover.location_ok("London, UK", self.cfg))
        self.assertFalse(discover.location_ok("Toronto, ON, Canada", self.cfg))

    def test_end_to_end_writes_queue_and_seen(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root / "state" / "internships").mkdir(parents=True)
            (root / "state" / "internships" / "queue.md").write_text("| id | found | company |\n|---|---|---|\n| q4 | 2026-09-01 | X |\n")
            discover.fetch = lambda url, timeout=30: FIXTURE   # no network
            rc = discover.main(["--vault", td, "--source", "simplify"])
            self.assertEqual(rc, 0)
            q = (root / "state" / "internships" / "queue.md").read_text()
            # main() stamps `found` with the real today, so pin it to today, never to a literal date
            self.assertIn(f"| q5 | {date.today().isoformat()} | Tesla | Internship - Software Engineering - Summer 2027 |", q)
            self.assertIn("faang", q)
            seen = json.loads((root / "state" / "internships" / "seen.json").read_text())
            self.assertEqual(len(seen), 6)
            rc = discover.main(["--vault", td, "--source", "simplify", "--monitor"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
