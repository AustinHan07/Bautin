"""Unit tests for the Bautin lane guard rules (pure functions, no Hermes runtime)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lane_rules", REPO / "bautin" / "plugins" / "lane_guards" / "rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

QUEUE = """| id | found | company | role | location | link | score | reason |
|---|---|---|---|---|---|---|---|
| q1 | 2026-09-18 | Robinhood | Software Engineer Intern - Backend | Menlo Park, CA | https://boards.greenhouse.io/robinhood/jobs/8123225 | 4 | faang |
| q2 | 2026-09-18 | Duolingo | Software Engineer Intern | Pittsburgh, PA | https://job-boards.greenhouse.io/duolingounirecruitment/jobs/8806115002 |  | known |
| q3 | 2026-09-18 | Visa | Software Engineer Intern | Austin, TX | https://visa.wd5.myworkdayjobs.com/x |  | known |
"""
FACTS = """# Facts
- University: University of Wisconsin-Madison
- Expected graduation: May 2028
- GPA: 3.7
"""


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); v = Path(self.td.name)
        (v / "state" / "internships").mkdir(parents=True)
        (v / "state" / "internships" / "queue.md").write_text(QUEUE)
        refs = v / "skills" / "internships" / "internship-pipeline" / "references"; refs.mkdir(parents=True)
        (refs / "facts.md").write_text(FACTS)
        self.v = v; self.r = rules.InternshipsRules(v)

    def tearDown(self):
        self.td.cleanup()

    def test_parse_ids(self):
        self.assertEqual(rules.InternshipsRules.parse_ids("q1, 2 and q5-7"), ({"q1", "q2", "q5", "q6", "q7"}, False))
        self.assertEqual(rules.InternshipsRules.parse_ids("all"), (set(), True))

    def test_apply_records_markers_and_rewrites(self):
        note = self.r.on_message("apply q1, q3, q9", "12345")
        self.assertIn("Approval recorded for: q1 Robinhood", note)
        self.assertIn("q3 Visa", note)
        self.assertIn("No queue row for: q9", note)
        marker = json.loads((self.v / "state" / "internships" / "approvals" / "q1.json").read_text())
        self.assertEqual(marker["url"], "https://boards.greenhouse.io/robinhood/jobs/8123225")
        self.assertEqual(marker["by"], "12345")
        self.assertFalse((self.v / "state" / "internships" / "approvals" / "q2.json").exists())
        self.assertIn("apply q1, q3, q9", (self.v / "log" / "internships" / "approvals.log").read_text())
        self.assertIsNone(self.r.on_message("please apply to everything", "1"))   # not an approval command
        self.assertIn("No queue ids", self.r.on_message("apply", "1"))

    def test_apply_all_covers_unapproved_rows_only(self):
        self.r.on_message("apply q2", "1")
        note = self.r.on_message("apply all", "1")
        self.assertIn("q1 Robinhood", note); self.assertIn("q3 Visa", note); self.assertNotIn("q2 Duolingo", note)

    def test_skip_removes_rows_and_markers(self):
        self.r.notion_skip = lambda ids: "ok"
        self.r.on_message("apply q2", "1")
        note = self.r.on_message("skip q2, q9", "1")
        self.assertIn("Skipped and removed from the queue: q2", note); self.assertIn("Not in the queue: q9", note)
        q = (self.v / "state" / "internships" / "queue.md").read_text()
        self.assertNotIn("| q2 |", q); self.assertIn("| q1 |", q); self.assertIn("| q3 |", q)
        self.assertFalse((self.v / "state" / "internships" / "approvals" / "q2.json").exists())
        self.assertIn("skip:q2,q9", (self.v / "log" / "internships" / "approvals.log").read_text())
        self.assertIsNone(self.r.on_message("please skip the boring ones", "1"))

    def test_submit_blocked_without_marker(self):
        cmd = {"command": "python3 /bautin/scripts/internships/submit.py --id q1 --submit"}
        self.assertIn("no approval on file for q1", self.r.before_tool("terminal", cmd))
        self.assertIsNone(self.r.before_tool("terminal", {"command": "python3 /bautin/scripts/internships/submit.py --id q1"}))  # dry run ok
        self.assertIn("needs `--id", self.r.before_tool("terminal", {"command": "python3 submit.py --submit"}))
        self.assertIsNone(self.r.before_tool("read_file", {"path": "/x"}))
        self.r.on_message("apply q1", "1")
        self.assertIsNone(self.r.before_tool("terminal", cmd))

    def test_draft_claims_check(self):
        good = "I am a student at the University of Wisconsin-Madison graduating in May 2028 with a 3.7 GPA."
        bad = "I graduated from Stanford University in 2024 with a 3.9 GPA and improved latency by 40%."
        args = {"path": "/vault/state/internships/drafts/q1.md", "content": bad}
        out = self.r.after_tool("write_file", args, "ok")
        self.assertIn("NOT found", out)
        for claim in ("year '2024'", "number '3.9'", "percentage '40%'", "school 'Stanford University'"):
            self.assertIn(claim, out)
        out = self.r.after_tool("write_file", {"path": "/vault/state/internships/drafts/q1.md", "content": good}, "ok")
        self.assertIn("every year, number, and school", out)
        self.assertIsNone(self.r.after_tool("write_file", {"path": "/vault/state/internships/notes.md", "content": bad}, "ok"))
        # patch tool: content read from the host copy of the sandbox path
        d = self.v / "state" / "internships" / "drafts"; d.mkdir(); (d / "q2.md").write_text(bad)
        out = self.r.after_tool("patch", {"path": "/vault/state/internships/drafts/q2.md"}, "patched")
        self.assertIn("Stanford", out)

    def test_post_submit_pushes_to_notion(self):
        d = self.v / "state" / "internships" / "drafts"; d.mkdir()
        (d / "q1-result.json").write_text(json.dumps({"status": "submitted", "id": "q1"}))
        calls = []
        self.r.push_notion = lambda p: calls.append(p) or "pushed"
        self.assertIsNone(self.r.on_tool_done("terminal", {"command": "python3 submit.py --id q1"}, ""))          # dry run: no push
        self.assertEqual(self.r.on_tool_done("terminal", {"command": "python3 submit.py --id q1 --submit"}, ""), "pushed")
        self.assertEqual(calls[0].name, "q1-result.json")
        (d / "q1-result.json").write_text(json.dumps({"status": "incomplete", "id": "q1"}))
        self.assertIsNone(self.r.on_tool_done("terminal", {"command": "python3 submit.py --id q1 --submit"}, ""))

    def test_mail_verification_tool_shape(self):
        spec2 = importlib.util.spec_from_file_location("lane_guards_pkg", REPO / "bautin" / "plugins" / "lane_guards" / "__init__.py",
                                                       submodule_search_locations=[str(REPO / "bautin" / "plugins" / "lane_guards")])
        pkg = importlib.util.module_from_spec(spec2); sys.modules["lane_guards_pkg"] = pkg; spec2.loader.exec_module(pkg)
        self.assertEqual(pkg.MAIL_TOOL_SCHEMA["required"], ["domain"])
        import subprocess
        real = subprocess.run
        class R:  # fake completed process
            stdout = '{"link": "https://x/verify?t=1", "code": null}'
        subprocess.run = lambda *a, **k: R()
        try:
            out = json.loads(pkg.mail_verification({"domain": "x.com", "since_min": 10}))
        finally:
            subprocess.run = real
        self.assertEqual(out["link"], "https://x/verify?t=1")

    def test_host_path_translation(self):
        self.assertEqual(rules.host_path(Path("/h/v"), "/vault/state/x.md"), Path("/h/v/state/x.md"))
        self.assertEqual(rules.host_path(Path("/h/v"), "/tmp/x"), Path("/tmp/x"))


if __name__ == "__main__":
    unittest.main()
