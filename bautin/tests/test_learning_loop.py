import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("learning_loop", REPO / "bautin" / "plugins" / "learning_loop" / "__init__.py")
ll = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ll)


class Dummy:
    _MEMORY_REVIEW_PROMPT = "memory prompt. If nothing is worth saving, just say 'Nothing to save.' and stop."
    _SKILL_REVIEW_PROMPT = "skill prompt."
    _COMBINED_REVIEW_PROMPT = "combined prompt."


class LearningLoopTests(unittest.TestCase):
    def test_suffix_applied_once(self):
        self.assertEqual(ll.apply_to(Dummy), 3)
        self.assertTrue(Dummy._SKILL_REVIEW_PROMPT.endswith(ll.SUFFIX))
        self.assertEqual(ll.apply_to(Dummy), 0)                       # idempotent
        self.assertEqual(Dummy._MEMORY_REVIEW_PROMPT.count("Bautin rule:"), 1)

    def test_real_aiagent_prompts_get_the_rule(self):
        import sys
        sys.path.insert(0, str(REPO))
        import run_agent
        n = ll.apply_to(run_agent.AIAgent)
        self.assertIn("Bautin rule:", run_agent.AIAgent._SKILL_REVIEW_PROMPT)
        self.assertIn("Nothing to save", run_agent.AIAgent._SKILL_REVIEW_PROMPT)  # upstream line still present


if __name__ == "__main__":
    unittest.main()
