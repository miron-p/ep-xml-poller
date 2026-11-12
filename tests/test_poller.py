import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen, Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poller as p


def listing(name="(A10-0270_2026)_Wolters", label="initial"):
    return (f'<html><a href="/sedcms/votingList/{name}.pdf">PDF</a>'
            f'<a href="/sedcms/votingList/{name}.docx?version={label}">DOCX</a></html>').encode()


def xml(text="AM 12", bold=False, merge=2, split_runs=False):
    text_runs = '<w:r><w:t>AM </w:t></w:r><w:r><w:t>12</w:t></w:r>' if split_runs else (
        f'<w:r>{"<w:rPr><w:b/></w:rPr>" if bold else ""}<w:t>{text}</w:t></w:r>')
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:wordDocument xmlns:w="{p.WORD}"><w:body>'
        '<w:p><w:r><w:t>Voting list — initial</w:t></w:r></w:p><w:tbl><w:tr>'
        '<w:tc><w:p><w:r><w:t>Amendment</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>Vote</w:t></w:r></w:p></w:tc></w:tr><w:tr>'
        f'<w:tc><w:tcPr><w:gridSpan w:val="{merge}"/></w:tcPr><w:p>{text_runs}'
        '<w:r><w:tab/><w:t>part 1</w:t><w:br/><w:t>RCV — Ș</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Separate vote request</w:t></w:r></w:p></w:tc>'
        '</w:tr></w:tbl></w:body></w:wordDocument>').encode()


class FakeEP:
    def __init__(self):
        self.page = listing()
        self.result = (200, {"etag": '"first"', "last-modified": "Thu, 12 Nov 2026 07:55:47 GMT"}, xml())
        self.calls = []

    def __call__(self, url, headers=None):
        self.calls.append((url, headers or {}))
        if url == p.LISTING:
            if isinstance(self.page, tuple):
                return self.page
            return 200, {}, self.page
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class DiscoveryTests(unittest.TestCase):
    def test_real_saved_november_listing(self):
        raw = (Path(__file__).parent / "fixtures/listing-november.html").read_bytes()
        documents = p.discover(raw)
        self.assertEqual(11, len(documents))
        urls = {d["url"] for d in documents}
        for name in ("%28A10-0270_2026%29_Wolters", "%28A10-0093_2026%29_Clergeau", "A10-0261_Vasile-Voiculescu"):
            self.assertIn(f"https://{p.HOST}/sedcms/votingList/{name}.xml", urls)
        self.assertTrue(all(d["labels"] == ["initial"] for d in documents))

    def test_deduplicate_query_labels_and_unusual_names(self):
        raw = listing("C10-0189_Émission Trading", "FINAL+VERSION%2F2")
        items = p.discover(raw)
        self.assertEqual(1, len(items))
        self.assertEqual(["FINAL VERSION/2"], items[0]["labels"])
        self.assertIn("%C3%89mission%20Trading.xml", items[0]["url"])
        self.assertNotIn("?", items[0]["url"])
        self.assertEqual(2, len(items[0]["sources"]))

    def test_unapproved_links_are_ignored(self):
        invalid = ["http://www.europarl.europa.eu/sedcms/votingList/a.xml",
                   "https://www.europarl.europa.eu.evil.test/sedcms/votingList/a.xml",
                   "https://user@www.europarl.europa.eu/sedcms/votingList/a.xml",
                   "https://www.europarl.europa.eu:444/sedcms/votingList/a.xml",
                   "https://www.europarl.europa.eu/sedcms/votingList/%2e%2e/a.xml",
                   "https://www.europarl.europa.eu/sedcms/votingList/a%252f.xml",
                   "https://www.europarl.europa.eu/doceo/document/PV-10-2026-11-12-RCV_EN.xml"]
        for url in invalid:
            self.assertFalse(p.allowed_url(url), url)
        items = p.discover(listing() + "".join(f'<a href="{u}">x</a>' for u in invalid).encode())
        self.assertEqual(1, len(items))

    def test_bad_catalogue_fails_closed(self):
        for body in (b"<html>No lists</html>", b"JavaScript is disabled", b"Verify you're not a robot"):
            with self.assertRaises(ValueError):
                p.discover(body)


class WordMLTests(unittest.TestCase):
    def test_preserves_tabs_breaks_paragraphs_tables_and_merges(self):
        lines = p.wordml_lines(xml())
        self.assertIn('paragraph "AM 12\\tpart 1\\nRCV — Ș"', lines)
        self.assertIn('paragraph "Separate vote request"', lines)
        self.assertEqual(2, lines.count("begin tr"))
        self.assertEqual(3, lines.count("begin tc"))
        self.assertIn("gridSpan", "\n".join(lines))
        self.assertNotEqual(lines, p.wordml_lines(xml(merge=1)))

    def test_run_styling_and_fragmentation_are_ignored(self):
        self.assertEqual(p.wordml_lines(xml()), p.wordml_lines(xml(bold=True)))
        self.assertEqual(p.wordml_lines(xml()), p.wordml_lines(xml(split_runs=True)))

    def test_html_ooxml_and_broken_xml_are_rejected(self):
        for raw in (b"<html>blocked</html>", b"<broken", b"", xml().replace(p.WORD.encode(), b"other")):
            with self.assertRaises(Exception):
                p.wordml_lines(raw)

    def test_dtd_and_entities_rejected_also_in_utf16(self):
        raw = '<!DOCTYPE x [<!ENTITY payload SYSTEM "file:///etc/passwd">]><x>&payload;</x>'
        for encoding in ("utf-8", "utf-16"):
            with self.assertRaisesRegex(ValueError, "DTD"):
                p.wordml_lines(raw.encode(encoding))

    def test_nested_tables_keep_boundaries(self):
        nested = xml().replace(b"</w:tc></w:tr></w:tbl>", b"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Nested</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:tc></w:tr></w:tbl>")
        self.assertEqual(2, p.wordml_lines(nested).count("begin tbl"))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = p.Archive(self.tmp.name)
        self.ep = FakeEP()
        self.poller = p.Poller(self.archive, request=self.ep, pause=0)
        self.clock = 1800000000

    def run_poll(self, seconds=300):
        self.clock += seconds
        with patch("poller.time.time", return_value=self.clock):
            return self.poller.once()

    def doc(self):
        return self.archive.lists()[0]

    def test_initial_conditional_304_and_restart_persistence(self):
        self.assertEqual(1, self.run_poll()["initial"])
        first = self.doc()
        self.ep.result = (304, {}, b"")
        self.assertEqual(1, self.run_poll()["unchanged"])
        self.assertEqual('"first"', self.ep.calls[-1][1]["If-None-Match"])
        self.assertIn("If-Modified-Since", self.ep.calls[-1][1])
        again = p.Archive(self.tmp.name)
        self.assertEqual(1, len(again.versions(first["id"])))
        self.assertEqual(xml(), again.version(first["id"], first["latest_version"])["xml"])

    def test_unlabelled_revision_other_xml_and_reversion_all_retained(self):
        self.run_poll()
        self.ep.result = (200, {"etag": '"second"'}, xml("AM 13"))
        self.assertEqual(1, self.run_poll()["text_or_structure"])
        self.ep.result = (200, {}, xml("AM 13", bold=True))
        self.assertEqual(1, self.run_poll()["other_xml"])
        self.ep.result = (200, {}, xml())
        self.assertEqual(1, self.run_poll()["text_or_structure"])
        versions = self.archive.versions(self.doc()["id"])
        self.assertEqual(4, len(versions))
        self.assertEqual(versions[0]["sha"], versions[-1]["sha"])
        with self.archive.connect() as db:
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM objects").fetchone()[0])

    def test_raw_unchanged_does_not_add_a_version_or_accumulate_stale_etag(self):
        self.run_poll()
        self.ep.result = (200, {}, xml())
        self.assertEqual(1, self.run_poll()["unchanged"])
        self.assertEqual(1, self.doc()["version_count"])
        self.assertIsNone(self.doc()["etag"])

    def test_unconditional_daily_fetch_even_with_etag(self):
        self.run_poll()
        self.run_poll(seconds=86401)
        self.assertEqual({}, self.ep.calls[-1][1])

    def test_version_label_change_does_not_duplicate_document(self):
        self.run_poll()
        self.ep.page = listing(label="FINAL+VERSION%2F2")
        self.run_poll()
        self.assertEqual(1, len(self.archive.lists()))
        self.assertEqual(["FINAL VERSION/2"], self.doc()["labels"])
        self.assertEqual(1, self.doc()["version_count"])

    def test_404_retried_when_xml_becomes_available(self):
        self.ep.result = (404, {}, b"")
        self.assertEqual(1, self.run_poll()["pending_xml"])
        self.assertFalse(self.doc()["xml_available"])
        self.ep.result = (200, {}, xml())
        self.assertEqual(1, self.run_poll()["initial"])
        self.assertTrue(self.doc()["xml_available"])

    def test_invalid_xml_never_replaces_good_xml_or_validators(self):
        self.run_poll()
        before = self.doc()
        self.ep.result = (200, {"etag": '"bad"'}, b"<html>Access denied</html>")
        result = self.run_poll()
        self.assertFalse(result["ok"])
        self.assertEqual(before["latest_version"], self.doc()["latest_version"])
        self.assertEqual(before["etag"], self.doc()["etag"])
        self.assertEqual(xml(), self.archive.version(before["id"], before["latest_version"])["xml"])

    def test_discovery_error_preserves_catalogue_and_checks_known_xml(self):
        self.run_poll()
        self.ep.page = (403, {}, b"")
        self.ep.result = (200, {}, xml("AM 14"))
        result = self.run_poll()
        self.assertFalse(result["ok"])
        self.assertFalse(result["discovery_ok"])
        self.assertEqual(1, result["text_or_structure"])
        self.assertTrue(self.doc()["listed"])

    def test_unlisted_urls_are_rechecked_for_track_window(self):
        self.run_poll()
        self.ep.page = listing("Different-name")
        result = self.run_poll()
        self.assertEqual(2, result["checked"])
        old = next(d for d in self.archive.lists() if "Wolters" in d["name"])
        self.assertFalse(old["listed"])
        result = self.run_poll(seconds=31 * 86400)
        self.assertEqual(1, result["checked"])
        self.assertEqual(2, len(self.archive.lists()))

    def test_retry_after_and_failed_backoff_stay_unhealthy(self):
        self.run_poll()
        self.ep.result = (429, {"retry-after": "1200"}, b"")
        self.assertFalse(self.run_poll()["ok"])
        self.assertGreaterEqual(self.doc()["next_due"], self.clock + 1200)
        skipped = self.run_poll()
        self.assertEqual(0, skipped["checked"])
        self.assertFalse(skipped["ok"])
        self.assertEqual(1, skipped["outstanding_errors"])

    def test_listing_retry_after_does_not_remove_known_sources(self):
        self.run_poll()
        self.ep.page = (429, {"retry-after": "1800"}, b"")
        self.assertFalse(self.run_poll()["ok"])
        self.assertEqual(self.clock + 1800, self.archive.state("listing_retry")["due"])
        self.ep.calls.clear()
        self.run_poll()
        self.assertTrue(all(url != p.LISTING for url, _ in self.ep.calls))
        self.assertTrue(self.doc()["listed"])

    def test_initial_304_cannot_become_a_successful_empty_archive(self):
        self.ep.result = (304, {}, b"")
        result = self.run_poll()
        self.assertFalse(result["ok"])
        self.assertFalse(self.doc()["xml_available"])
        self.assertEqual(0, self.doc()["version_count"])

    def test_utf16_original_is_archived_without_reencoding(self):
        raw = xml().decode("utf-8").replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16")
        self.ep.result = (200, {}, raw)
        self.assertEqual(1, self.run_poll()["initial"])
        doc = self.doc()
        self.assertEqual(raw, self.archive.version(doc["id"], doc["latest_version"])["xml"])

    def test_timeout_preserves_archive(self):
        self.run_poll()
        self.ep.result = TimeoutError("EP timed out")
        self.assertFalse(self.run_poll()["ok"])
        self.assertEqual(1, self.doc()["version_count"])

    def test_process_lock_prevents_second_writer(self):
        with p.writer_lock(self.archive):
            child = subprocess.run([sys.executable, str(Path(p.__file__)), "once", "--data-dir", self.tmp.name],
                                   capture_output=True, text=True, timeout=10)
        self.assertNotEqual(0, child.returncode)
        self.assertIn("Another poller", child.stderr)

    def test_http_api_versions_diff_exact_xml_and_readiness(self):
        self.run_poll()
        self.ep.result = (200, {}, xml("AM 13"))
        self.run_poll()
        server = p.ThreadingHTTPServer(("127.0.0.1", 0), p.handler_for(self.archive, 300))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)

        def get(path):
            with urlopen(base + path, timeout=3) as response:
                return json.load(response)

        self.assertTrue(get("/healthz")["alive"])
        with patch("poller.time.time", return_value=self.clock + 1):
            self.assertTrue(get("/readyz")["ready"])
        documents = get("/api/lists")["documents"]
        doc = documents[0]
        versions = get(f'/api/lists/{doc["id"]}/versions')["versions"]
        self.assertEqual(2, len(versions))
        with urlopen(base + doc["download"], timeout=3) as response:
            self.assertEqual(xml("AM 13"), response.read())
            self.assertIn("attachment;", response.headers["Content-Disposition"])
            self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        diff = get(f'/api/lists/{doc["id"]}/diff')
        self.assertTrue(diff["text_or_structure_changed"])
        self.assertIn("AM 13", "\n".join(diff["diff"]))
        for path, code in ((f'/api/lists/{doc["id"]}/diff?from=no', 400),
                           (f'/xml/{"0" * 24}/{versions[0]["id"]}.xml', 404),
                           ("/../../archive.sqlite3", 404)):
            with self.assertRaises(HTTPError) as caught:
                get(path)
            self.assertEqual(code, caught.exception.code)
        with patch("poller.time.time", return_value=self.clock + 901):
            with self.assertRaises(HTTPError) as caught:
                get("/readyz")
            self.assertEqual(503, caught.exception.code)
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(base + "/api/lists", data=b"{}", method="POST"), timeout=3)
        self.assertEqual(501, caught.exception.code)


class FetchTests(unittest.TestCase):
    def test_only_public_cache_headers_retained(self):
        class Reply(io.BytesIO):
            code = 200
            headers = {"Content-Type": "text/xml", "ETag": '"a"', "Set-Cookie": "not-archived"}
        with patch("poller.build_opener") as factory:
            factory.return_value.open.return_value = Reply(xml())
            status, headers, raw = p.fetch(p.discover(listing())[0]["url"])
        self.assertEqual(200, status)
        self.assertEqual(xml(), raw)
        self.assertEqual({"content-type": "text/xml", "etag": '"a"'}, headers)

    def test_oversized_response_rejected_before_reading(self):
        class Reply(io.BytesIO):
            code = 200
            headers = {"Content-Length": str(p.MAX_BYTES + 1)}
        with patch("poller.build_opener") as factory:
            factory.return_value.open.return_value = Reply(b"")
            with self.assertRaisesRegex(ValueError, "size limit"):
                p.fetch(p.LISTING)

    def test_retry_after_http_date_and_upper_bound(self):
        self.assertEqual(1200, p.retry_delay(1, 300, 0, {"retry-after": "Thu, 01 Jan 1970 00:20:00 GMT"}))
        self.assertEqual(86400, p.retry_delay(1, 300, 0, {"retry-after": "999999999"}))
        self.assertEqual(300, p.retry_delay(1, 300, 0, {"retry-after": "not a date"}))

    def test_redirect_to_unapproved_host_never_requested(self):
        class Reply(io.BytesIO):
            code = 302
            headers = {"Location": "http://127.0.0.1/private"}
        class Opener:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                return Reply()
        opener = Opener()
        with patch("poller.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "Unapproved"):
                p.fetch(p.LISTING)
        self.assertEqual(1, opener.calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
