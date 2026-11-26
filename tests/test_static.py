"""Static checks on the web UI: accessibility basics and XSS-safe rendering."""
import unittest
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = self.viewport = self.skip_link = self.title = False
        self.label_targets, self.controls, self._in_label = set(), [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.lang = self.lang or (tag == "html" and bool(a.get("lang")))
        self.viewport = self.viewport or (tag == "meta" and a.get("name") == "viewport")
        self.skip_link = self.skip_link or (tag == "a" and a.get("class") == "skip")
        self.title = self.title or tag == "title"
        if tag == "label":
            self._in_label += 1
            if "for" in a:
                self.label_targets.add(a["for"])
        elif tag in ("input", "select"):
            self.controls.append((a.get("id"), self._in_label > 0, a.get("aria-label")))

    def handle_endtag(self, tag):
        if tag == "label":
            self._in_label -= 1


class StaticTests(unittest.TestCase):
    def test_form_controls_have_accessible_names(self):
        audit = Audit()
        audit.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertTrue(audit.lang and audit.viewport and audit.skip_link and audit.title)
        for control_id, wrapped, aria in audit.controls:
            with self.subTest(control=control_id):
                self.assertTrue(wrapped or aria or control_id in audit.label_targets)

    def test_script_never_parses_untrusted_html(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, script)

    def test_no_inline_script_or_remote_assets(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<script>", page)


if __name__ == "__main__":
    unittest.main()
