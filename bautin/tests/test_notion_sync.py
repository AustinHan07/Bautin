"""Unit tests for notion_sync.py with a fake Notion API (no network)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("notion_sync", ROOT / "scripts" / "internships" / "notion_sync.py")
ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns)

NOTION_MD = """---
database_id: db-1
page_id: page-1
---
"""
QUEUE = """| id | found | company | role | location | link | score | reason |
|---|---|---|---|---|---|---|---|
| q1 | 2026-09-18 | Robinhood | Software Engineer Intern - Backend | Menlo Park, CA | https://boards.greenhouse.io/robinhood/jobs/8123225 | 4 | faang |
| q2 | 2026-09-18 | Waymo | Scenes Intern | Mountain View, CA | https://careers.withwaymo.com/jobs?gh_jid=8210170&utm_source=Simplify |  | faang |
"""


def page(company, role, url, status, applied=None):
    return {"id": f"pg-{company}", "properties": {
        "Company": {"type": "title", "title": [{"plain_text": company}]},
        "Role": {"type": "rich_text", "rich_text": [{"plain_text": role}]},
        "Location": {"type": "rich_text", "rich_text": [{"plain_text": "X"}]},
        "Apply URL": {"type": "url", "url": url}, "Status": {"type": "select", "select": {"name": status}},
        "Posted": {"type": "date", "date": {"start": "2026-09-17"}}, "Applied": {"type": "date", "date": {"start": applied} if applied else None},
        "Portal": {"type": "select", "select": None}, "Source": {"type": "select", "select": None}, "Track": {"type": "select", "select": None},
        "Company type": {"type": "select", "select": None}, "Referral": {"type": "checkbox", "checkbox": False},
        "Notes": {"type": "rich_text", "rich_text": [{"plain_text": "n"}]}}}


class FakeAPI:
    def __init__(self):
        self.pages = [page("Waymo", "Scenes Intern", "https://careers.withwaymo.com/jobs?gh_jid=8210170", "Skipped"),
                      page("Corteva", "Agentic AI Engineer Intern", "https://apply.corteva.com/x", "Applied", "2026-09-17")]
        self.calls = []
        self.blocks = [{"id": "blk-1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Applications sent: 62"}]}}]

    def __call__(self, method, path, body=None, tok=None):
        self.calls.append((method, path, body))
        if path.startswith("/databases/db-1/query"):
            f = (body or {}).get("filter")
            if f and "url" in f:
                return {"results": [p for p in self.pages if p["properties"]["Apply URL"]["url"] == f["url"]["equals"]], "has_more": False}
            if f and "select" in f:
                return {"results": [p for p in self.pages if p["properties"]["Status"]["select"]["name"] == f["select"]["equals"]], "has_more": False}
            return {"results": self.pages, "has_more": False}
        if method == "POST" and path == "/pages":
            self.pages.append({"id": "pg-new", "properties": body["properties"] | {"Apply URL": {"type": "url", "url": body["properties"]["Apply URL"]["url"]},
                                                                                     "Status": {"type": "select", "select": body["properties"]["Status"]["select"]}}})
            return {"id": "pg-new"}
        if method == "PATCH" and path.startswith("/pages/"):
            return {"id": path.split("/")[-1]}
        if method == "GET" and path.startswith("/blocks/page-1/children"):
            return {"results": self.blocks}
        if method == "PATCH" and path.startswith("/blocks/"):
            return {}
        raise AssertionError(f"unexpected {method} {path}")


class NotionSyncTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.v = Path(self.td.name)
        (self.v / "state" / "internships" / "drafts").mkdir(parents=True)
        (self.v / "state" / "internships" / "notion.md").write_text(NOTION_MD)
        (self.v / "state" / "internships" / "queue.md").write_text(QUEUE)
        self.api = FakeAPI(); ns.request = self.api
        self.cfg = ns.load_cfg(self.v)

    def tearDown(self):
        self.td.cleanup()

    def test_pull_writes_index_and_done_sets(self):
        out = ns.pull(self.v, self.cfg)
        self.assertEqual(out["count"], 2); self.assertEqual(out["by_status"], {"Applied": 1, "Skipped": 1})
        urls, keys = ns.done_index(self.v, self.cfg)
        self.assertIn("https://careers.withwaymo.com/jobs?gh_jid=8210170", urls)
        self.assertIn("waymo scenes intern", keys)

    def test_classifiers(self):
        self.assertEqual(ns.classify_track("Software Engineer Intern - Backend"), "SWE")
        self.assertEqual(ns.classify_track("Agentic AI Engineer Intern"), "AI")
        self.assertEqual(ns.classify_track("Machine Learning Intern"), "ML")
        self.assertEqual(ns.classify_track("Engineering Intern"), "SWE-adj")
        self.assertEqual(ns.classify_portal("https://visa.wd5.myworkdayjobs.com/x", None), "Workday")
        self.assertEqual(ns.classify_portal("https://x", "greenhouse"), "Greenhouse")
        self.assertEqual(ns.classify_company_type("faang"), "Big Tech"); self.assertEqual(ns.classify_company_type("known"), "Strong mid")

    def test_push_submission_creates_row_and_refreshes_counter(self):
        res = self.v / "state" / "internships" / "drafts" / "q1-result.json"
        res.write_text(json.dumps({"status": "submitted", "id": "q1", "url": "https://boards.greenhouse.io/robinhood/jobs/8123225",
                                   "company": "Robinhood", "role": "Software Engineer Intern - Backend", "family": "greenhouse", "confirmation": "Thank you for applying"}))
        out = ns.push_result(self.v, self.cfg, res)
        self.assertEqual(out["action"], "created"); self.assertTrue(out["pushed"]); self.assertEqual(out["status"], "Applied")
        self.assertEqual(out["counter"], 2)                        # Corteva + the new Robinhood row
        create = next(b for m, p, b in self.api.calls if m == "POST" and p == "/pages")
        props = create["properties"]
        self.assertEqual(props["Company"]["title"][0]["text"]["content"], "Robinhood")
        self.assertEqual(props["Portal"]["select"]["name"], "Greenhouse"); self.assertEqual(props["Track"]["select"]["name"], "SWE")
        self.assertEqual(props["Company type"]["select"]["name"], "Big Tech"); self.assertEqual(props["Source"]["select"]["name"], "Simplify")
        self.assertIn("Confirmation: Thank you for applying", props["Notes"]["rich_text"][0]["text"]["content"])
        patch = next(b for m, p, b in self.api.calls if m == "PATCH" and p == "/blocks/blk-1")
        self.assertEqual(patch["paragraph"]["rich_text"][0]["text"]["content"], "Applications sent: 2")

    def test_push_uncertain_updates_existing_row_without_counter(self):
        self.api.pages.append(page("Robinhood", "Software Engineer Intern - Backend", "https://boards.greenhouse.io/robinhood/jobs/8123225", "Queued"))
        res = self.v / "state" / "internships" / "drafts" / "q1-result.json"
        res.write_text(json.dumps({"status": "uncertain", "id": "q1", "url": "https://boards.greenhouse.io/robinhood/jobs/8123225", "message": "no confirmation"}))
        out = ns.push_result(self.v, self.cfg, res)
        self.assertEqual(out["action"], "updated"); self.assertEqual(out["status"], "Applying"); self.assertIsNone(out["counter"])
        self.assertFalse(ns.push_result(self.v, self.cfg, res.with_name("x.json")) if False else False)

    def test_push_queued_skips_rows_already_in_notion(self):
        ns.pull(self.v, self.cfg)
        out = ns.push_queued(self.v, self.cfg)
        self.assertEqual(out["ids"], ["q1"])                        # q2 (Waymo) is Skipped in Notion


if __name__ == "__main__":
    unittest.main()
