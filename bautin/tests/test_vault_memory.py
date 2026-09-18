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
        self.assertIn("0 dated facts for lane 'internships'", self.p.system_prompt_block())
        self.assertEqual(self.p.prefetch("anything"), "")

    def test_save_writes_frontmatter_file_and_dedupes(self):
        r = self.call("vault_memory_save", fact="Robinhood closes SWE intern apps by early October.", tags=["Robinhood", "deadline"], source="https://x.test/p")
        self.assertTrue(r["saved"]); self.assertEqual(r["total_facts"], 1)
        path = self.vault / r["path"]
        text = path.read_text()
        self.assertTrue(text.startswith("---\nlane: internships\ndate: "))
        self.assertIn("source: https://x.test/p\ntype: memory\ntags: [robinhood, deadline]\nsession: sess-1\n---\n", text)
        self.assertTrue(text.endswith("Robinhood closes SWE intern apps by early October.\n"))
        again = self.call("vault_memory_save", fact="  Robinhood closes SWE intern apps by early  October. ")
        self.assertFalse(again["saved"]); self.assertTrue(again["duplicate"])
        self.assertIn("1 dated facts", self.p.system_prompt_block())

    def test_search_ranks_by_matched_terms_then_recency(self):
        self.call("vault_memory_save", fact="Waymo recruiters reply within two weeks.", tags=["waymo"])
        self.call("vault_memory_save", fact="Waymo SWE intern posting asks for C++ and robotics coursework.", tags=["waymo", "swe"])
        self.call("vault_memory_save", fact="Duolingo uses Greenhouse.", tags=["duolingo"])
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
        long = "x" * 1001
        out = self.p.handle_tool_call("vault_memory_save", {"fact": long})
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
