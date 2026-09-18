import email, email.policy, importlib.util, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mail", ROOT / "scripts" / "internships" / "mail.py")
mail = importlib.util.module_from_spec(spec); spec.loader.exec_module(mail)

VERIFY_HTML = """From: Corteva Careers <no-reply@corteva.com>
Subject: Please verify your email address
Date: Thu, 18 Sep 2026 20:10:00 +0000
Content-Type: text/html; charset=utf-8

<html><body><p>Thanks for applying, Austin. Please confirm your email to complete your application.</p>
<p><a href="https://apply.corteva.com/verify?token=abc123&utm_source=email">Verify my email</a></p>
<p><a href="https://apply.corteva.com/unsubscribe">Unsubscribe</a></p></body></html>
"""
CODE_TEXT = """From: Workday <noreply@myworkday.com>
Subject: Your verification code
Date: Thu, 18 Sep 2026 20:12:00 +0000
Content-Type: text/plain; charset=utf-8

Use this one-time code to verify your account: 483920. It expires in 10 minutes.
"""
EMPLOIVE = """From: Emploive <alerts@emploive.com>
Subject: 3 new Software Engineer internships
Date: Thu, 18 Sep 2026 20:00:00 +0000
Content-Type: text/html; charset=utf-8

<html><body><table><tr><td><b>Stripe</b><br>San Francisco, CA<br><a href="https://emploive.com/apply/123?x=1">Software Engineer Intern</a></td></tr>
<tr><td><b>Datadog</b><br>New York, NY<br><a href="https://careers.datadoghq.com/detail/abc">Software Engineering Intern - Summer 2027</a></td></tr>
<tr><td><a href="https://emploive.com/home">See all jobs</a> <a href="https://emploive.com/unsubscribe?u=1">Unsubscribe</a></td></tr></table></body></html>
"""


def msg(raw):
    return email.message_from_string(raw, policy=email.policy.default)


class MailTests(unittest.TestCase):
    def test_verification_link(self):
        v = mail.extract_verification(msg(VERIFY_HTML))
        self.assertTrue(v["looks_like_verification"])
        self.assertEqual(v["link"], "https://apply.corteva.com/verify?token=abc123&utm_source=email")
        self.assertNotIn("unsubscribe", " ".join(v["links"]))
        self.assertIsNone(v["code"])

    def test_verification_code(self):
        v = mail.extract_verification(msg(CODE_TEXT))
        self.assertEqual(v["code"], "483920"); self.assertTrue(v["looks_like_verification"])

    def test_emploive_rows(self):
        rows = mail.parse_emploive(msg(EMPLOIVE))
        self.assertEqual([r["company"] for r in rows], ["Stripe", "Datadog"])
        self.assertEqual(rows[0]["role"], "Software Engineer Intern"); self.assertEqual(rows[0]["location"], "San Francisco, CA")
        self.assertEqual(rows[1]["url"], "https://careers.datadoghq.com/detail/abc"); self.assertEqual(rows[0]["source"], "emploive")

    def test_password_spaces_are_ignored(self):
        import os
        os.environ["GMAIL_ADDRESS"] = "a@b.c"; os.environ["GMAIL_APP_PASSWORD"] = "abcd efgh ijkl mnop"
        self.assertEqual(mail.creds(), ("a@b.c", "abcdefghijklmnop"))


if __name__ == "__main__":
    unittest.main()
