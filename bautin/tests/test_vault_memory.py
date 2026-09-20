"""Unit tests for the Bautin vault memory provider (no Hermes runtime, no network)."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
spec = importlib.util.spec_from_file_location("vault_memory", REPO / "bautin" / "plugins" / "vault" / "__init__.py")
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


class VaultMemoryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.vault = root / "vault"; (self.vault / "memory").mkdir(parents=True)
        self.home = root / "profile"; self.home.mkdir()
        (self.home / "config.yaml").write_text(f"memory:\n  provider: vault\nbautin:\n  vault: {self.vault}\n")
        self.p = vm.VaultMemoryProvider()
        self.p.initialize("sess-1", hermes_home=str(self.home), agent_identity="internships", platform="cli")

    def tearDown(self):
        self.td.cleanup()

    def call(self, tool, **args):
        return json.loads(self.p.handle_tool_call(tool, args))

    def test_available_and_lane_dir_created(self):
        self.assertTrue(self.p.is_available())
        self.assertTrue((self.vault / "memory" / "internships").is_dir())
        self.assertEqual([s["name"] for s in self.p.get_tool_schemas()], ["vault_memory_search", "vault_memory_save"])
        self.assertIn("0 durable facts for lane 'internships' (nothing yet)", self.p.system_prompt_block())
        self.assertEqual(self.p.prefetch("anything"), "")

    def save(self, fact, kind="company", subject="Robinhood", **kw):
        return self.call("vault_memory_save", fact=fact, kind=kind, subject=subject, **kw)

    def test_save_writes_frontmatter_file_under_kind_and_subject(self):
        r = self.save("Robinhood closes SWE intern applications by early October each year.",
                      tags=["Robinhood", "deadline"], source="https://x.test/p")
        self.assertTrue(r["saved"]); self.assertEqual((r["total_facts"], r["revision"]), (1, 1))
        self.assertEqual(r["path"], "memory/internships/company/robinhood.md")
        text = (self.vault / r["path"]).read_text()
        self.assertTrue(text.startswith("---\nlane: internships\nkind: company\nsubject: Robinhood\ndate: "))
        self.assertIn("revision: 1\nsource: https://x.test/p\ntype: memory\ntags: [robinhood, deadline]\nsession: sess-1\n---\n", text)
        again = self.save("  Robinhood closes SWE intern applications by early  October each year. ")
        self.assertFalse(again["saved"]); self.assertTrue(again["unchanged"])
        self.assertIn("1 durable facts for lane 'internships' (1 company)", self.p.system_prompt_block())

    def test_gate_refuses_what_is_not_durable(self):
        bad = [
            (dict(fact="IBM needs an account.", kind="rumour", subject="ibm"), "kind must be one of"),
            (dict(fact="IBM's Apply button goes to an IBMid sign-in before any form.", kind="portal", subject=""), "subject is required"),
            (dict(fact="short", kind="portal", subject="ibm"), "at least 20"),
            (dict(fact="x" * 1001, kind="portal", subject="ibm"), "1001 characters"),
            (dict(fact="Applied to q15 at IBM through the careers portal today.", kind="outcome", subject="ibm"), "must not reference a queue id"),
            (dict(fact="IBM stopped accepting intern applications yesterday afternoon.", kind="portal", subject="ibm"), "must not use a relative date"),
            (dict(fact="We just finished filling the RTX Workday form for Austin.", kind="outcome", subject="rtx"), "session narration"),
        ]
        for args, expect in bad:
            out = self.p.handle_tool_call("vault_memory_save", args)
            self.assertIn(expect, out, f"gate let through: {args}")
        self.assertEqual(self.p._count(), 0)

    def test_pass_supersedes_same_subject_and_keeps_history(self):
        a = self.save("IBM's Apply button goes to an IBMid sign-in before any form is shown.", kind="portal", subject="IBM")
        self.assertEqual(a["revision"], 1)
        b = self.save("IBM's Apply button goes to an IBMid sign-in; the account can be created with an email address.",
                      kind="portal", subject="IBM")
        self.assertEqual((b["revision"], b["path"]), (2, "memory/internships/portal/ibm.md"))
        self.assertIn("IBMid sign-in before any form", b["superseded"])
        self.assertEqual(self.p._count(), 1)                       # replaced, not accumulated
        text = (self.vault / b["path"]).read_text()
        self.assertIn("first_saved: ", text); self.assertIn("revision: 2", text)
        self.assertIn("created with an email address", text)

    def test_search_filters_by_kind(self):
        self.save("IBM's Apply button goes to an IBMid sign-in before any form.", kind="portal", subject="IBM")
        self.save("IBM titles its intern roles by product area rather than by engineering level.", kind="company", subject="IBM")
        self.assertEqual(self.call("vault_memory_search", query="IBM")["count"], 2)
        only = self.call("vault_memory_search", query="IBM", kind="portal")
        self.assertEqual((only["count"], only["results"][0]["kind"]), (1, "portal"))
        self.assertEqual(only["results"][0]["subject"], "IBM")
        self.assertIn("unknown kind", self.p.handle_tool_call("vault_memory_search", {"query": "IBM", "kind": "nope"}))

    def test_search_ranks_by_matched_terms_then_recency(self):
        self.save("Waymo recruiters reply to intern applicants within about two weeks.", subject="Waymo", tags=["waymo"])
        self.save("Waymo SWE intern postings ask for C++ and robotics coursework.", kind="portal", subject="Waymo ATS", tags=["waymo", "swe"])
        self.save("Duolingo runs its intern hiring through Greenhouse.", subject="Duolingo", tags=["duolingo"])
        r = self.call("vault_memory_search", query="Waymo robotics C++", limit=5)
        self.assertEqual(r["count"], 2)
        self.assertIn("robotics", r["results"][0]["fact"])
        self.assertEqual(r["results"][0]["matched_terms"], 3)
        self.assertEqual(r["total_facts"], 3)
        for rg in (self.p._rg, None):            # ripgrep path and the pure-Python fallback agree
            self.p._rg = rg
            self.assertEqual(self.call("vault_memory_search", query="duolingo")["results"][0]["path"].startswith("memory/internships/"), True)

    def test_validation(self):
        self.assertIn("required", self.call("vault_memory_search", query="")["error"] if isinstance(self.call("vault_memory_search", query=""), dict) else self.p.handle_tool_call("vault_memory_search", {"query": ""}))
        out = self.p.handle_tool_call("vault_memory_save", {"fact": "x" * 1001, "kind": "portal", "subject": "ibm"})
        self.assertIn("1001 characters", out)
        self.assertIn("unknown tool", self.p.handle_tool_call("nope", {}))

    def test_missing_vault_is_loud_and_toolless(self):
        (self.home / "config.yaml").write_text("bautin:\n  vault: /nonexistent/vault\n")
        p = vm.VaultMemoryProvider()
        p.initialize("sess-2", hermes_home=str(self.home), agent_identity="internships")
        self.assertFalse(p.is_available()); self.assertIn("does not exist", p.unavailable_reason())
        self.assertEqual(p.get_tool_schemas(), [])
        self.assertIn("UNAVAILABLE", p.system_prompt_block())
        self.assertIn("unavailable", p.handle_tool_call("vault_memory_search", {"query": "x"}))
        (self.home / "config.yaml").write_text("memory: {}\n")
        p2 = vm.VaultMemoryProvider(); p2.initialize("s", hermes_home=str(self.home))
        self.assertIn("not set", p2.unavailable_reason())


if __name__ == "__main__":
    unittest.main()
