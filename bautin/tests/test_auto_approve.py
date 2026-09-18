import importlib.util, json, sys, tempfile, unittest
from datetime import date
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "internships"))
spec = importlib.util.spec_from_file_location("auto_approve", ROOT / "scripts" / "internships" / "auto_approve.py")
aa = importlib.util.module_from_spec(spec); spec.loader.exec_module(aa)

QUEUE = """| id | found | company | role | location | link | score | reason |
|---|---|---|---|---|---|---|---|
| q1 | {t} | Robinhood | Software Engineer Intern - Backend | Menlo Park, CA | https://boards.greenhouse.io/robinhood/jobs/1 |  | faang |
| q2 | {t} | Ragle Inc | Software Engineer Intern | TX | https://ragle.com/2 |  | unknown |
| q3 | {old} | Visa | Software Engineer Intern | Austin, TX | https://visa.wd5.myworkdayjobs.com/3 |  | known |
| q4 | {t} | Waymo | Scenes Intern | Mountain View, CA | https://careers.withwaymo.com/jobs?gh_jid=8210170 |  | faang |
| q5 | {t} | Duolingo | Data Scientist Intern | Pittsburgh, PA | https://duo.com/5 |  | known |
| q6 | {t} | Stripe | Software Engineer Intern | SF | https://stripe.com/6 |  | known |
"""


class AutoApproveTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.v = Path(self.td.name)
        (self.v / "state" / "internships").mkdir(parents=True)
        t = date.today().isoformat(); old = date.fromordinal(date.today().toordinal() - 3).isoformat()
        (self.v / "state" / "internships" / "queue.md").write_text(QUEUE.format(t=t, old=old))
        (self.v / "state" / "internships" / "notion-applied.json").write_text(json.dumps({"rows": [
            {"url_key": "https://careers.withwaymo.com/jobs?gh_jid=8210170", "key": "waymo scenes intern", "status": "Skipped"}]}))
        self.cfg = dict(aa.load_filters(self.v), auto_apply=True, auto_apply_daily_cap=15, max_age_days=1)

    def tearDown(self):
        self.td.cleanup()

    def test_rules(self):
        d = aa.decide(self.v, self.cfg, date.today())
        self.assertEqual([r["id"] for r in d["approve"]], ["q1", "q6"])
        why = {h["id"]: h["why"] for h in d["hold"]}
        self.assertEqual(why["q2"], "tier"); self.assertIn("age", why["q3"]); self.assertIn("in notion", why["q4"]); self.assertEqual(why["q5"], "role")

    def test_daily_cap_and_markers(self):
        d = aa.decide(self.v, dict(self.cfg, auto_apply_daily_cap=1), date.today())
        self.assertEqual([r["id"] for r in d["approve"]], ["q1"]); self.assertIn("daily cap", {h["id"]: h["why"] for h in d["hold"]}["q6"])
        ids = aa.write_markers(self.v, d["approve"], "test-bot-token")
        m = json.loads((self.v / "state" / "internships" / "approvals" / "q1.json").read_text())
        self.assertEqual((ids, m["by"], m["company"]), (["q1"], "rules", "Robinhood"))
        self.assertTrue(aa.marker_valid(m, "test-bot-token")); self.assertFalse(aa.marker_valid(m, "other"))
        d2 = aa.decide(self.v, dict(self.cfg, auto_apply_daily_cap=1), date.today())
        self.assertEqual(d2["approve"], [])           # q1 already has a marker; cap reached for today

    def test_no_key_writes_nothing(self):
        d = aa.decide(self.v, self.cfg, date.today())
        self.assertEqual(aa.write_markers(self.v, d["approve"], ""), [])
        self.assertFalse((self.v / "state" / "internships" / "approvals" / "q1.json").exists())

    def test_off_by_default_reports_only(self):
        cfg = dict(self.cfg, auto_apply=False)
        d = aa.decide(self.v, cfg, date.today())
        self.assertFalse(d["auto_apply"]); self.assertEqual(len(d["approve"]), 2)
        self.assertFalse((self.v / "state" / "internships" / "approvals" / "q1.json").exists())


if __name__ == "__main__":
    unittest.main()
