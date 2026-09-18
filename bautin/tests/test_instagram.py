import importlib.util, json, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "internships" / f"{name}.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
igs, igx = load("ig_stories"), load("ig_extract")


class StoriesHelpers(unittest.TestCase):
    def test_story_id_and_links(self):
        self.assertEqual(igs.story_id("https://www.instagram.com/stories/zero2sudo/3721234567890/"), "3721234567890")
        self.assertEqual(igs.story_id("https://www.instagram.com/zero2sudo/"), "")
        links = igs.external_links(["https://www.instagram.com/zero2sudo/", "https://l.instagram.com/?u=https%3A%2F%2Fboards.greenhouse.io%2Ffigma%2Fjobs%2F1&e=x",
                                    "https://careers.microsoft.com/x", "#", "https://www.facebook.com/x"])
        self.assertEqual(links, ["https://boards.greenhouse.io/figma/jobs/1", "https://careers.microsoft.com/x"])


class ExtractTests(unittest.TestCase):
    def test_parse_and_merge(self):
        self.assertEqual(igx.parse_postings('junk {"postings":[{"company":"Figma","role":"SWE Intern","url":"","location":"","note":"opened today"}]} tail')[0]["company"], "Figma")
        self.assertEqual(igx.parse_postings("no json"), [])
        self.assertEqual(igx.parse_postings('{"postings":[{"company":""}]}'), [])
        merged = igx.merge_rows([{"company": "Figma", "role": "SWE Intern", "url": ""}], [{"company": "figma", "role": "swe intern", "url": ""}, {"company": "Nvidia", "role": "", "url": ""}])
        self.assertEqual(len(merged), 2)

    def test_extract_with_fake_vision(self):
        with tempfile.TemporaryDirectory() as td:
            v = Path(td); ig = v / "state" / "internships" / "ig" / "2026-09-18"; ig.mkdir(parents=True)
            (ig / "1.png").write_bytes(b"png"); (ig / "2.png").write_bytes(b"png")
            (v / "state" / "internships" / "ig" / "seen.json").write_text(json.dumps({
                "1": {"date": "2026-09-18", "frame": "state/internships/ig/2026-09-18/1.png", "links": ["https://boards.greenhouse.io/figma/jobs/1"]},
                "2": {"date": "2026-09-18", "frame": "state/internships/ig/2026-09-18/2.png", "links": []},
                "3": {"date": "2026-09-17", "frame": "state/internships/ig/2026-09-17/3.png", "links": [], "extracted": "done"}}))
            answers = {b"png": None}
            calls = []
            def fake(png, model, key):
                calls.append(model)
                n = len(calls)
                text = ('{"postings":[{"company":"Figma","role":"Software Engineer Intern","location":"SF","url":"","note":"opened"}]}' if n == 1
                        else '{"postings":[{"company":"Nvidia","role":"","location":"","url":"","note":"Ignite opened"}]}')
                return {"output_text": text}
            out = igx.extract(v, "gpt-5.6-luna", "k", caller=fake)
            self.assertEqual(out, {"frames": 2, "rows": 2, "leads": 1})
            src = json.loads((v / "state" / "internships" / "sources" / "zero2sudo.json").read_text())["rows"]
            self.assertEqual(src[0]["url"], "https://boards.greenhouse.io/figma/jobs/1")      # link sticker fills the URL
            self.assertEqual(src[1]["url"], ""); self.assertEqual(src[1]["role"], "Internship (see story)")
            self.assertIn("| Nvidia |", (v / "state" / "internships" / "leads.md").read_text())
            seen = json.loads((v / "state" / "internships" / "ig" / "seen.json").read_text())
            self.assertTrue(seen["1"]["extracted"] and seen["2"]["extracted"]); self.assertEqual(seen["3"]["extracted"], "done")
            self.assertEqual(igx.extract(v, "gpt-5.6-luna", "k", caller=fake)["frames"], 0)   # idempotent


if __name__ == "__main__":
    unittest.main()
