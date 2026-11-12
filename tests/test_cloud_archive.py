import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cloud_archive as cloud
import poller
from test_poller import FakeEP, xml


class CloudArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = poller.Archive(self.root / "runtime")
        self.snapshot = self.root / "public"

    def test_roundtrip_retains_versions_original_bytes_and_validators(self):
        ep = FakeEP()
        poller.Poller(self.archive, request=ep, pause=0).once()
        ep.result = 200, {"etag": '"second"'}, xml("AM 13")
        poller.Poller(self.archive, request=ep, pause=0).once()
        cloud.export(self.archive, self.snapshot)
        restored = poller.Archive(self.root / "restored")
        cloud.restore(restored, self.snapshot)
        doc = restored.lists()[0]
        self.assertEqual(2, doc["version_count"])
        self.assertEqual('"second"', doc["etag"])
        self.assertEqual(xml("AM 13"), restored.version(doc["id"], doc["latest_version"])["xml"])
        self.assertEqual(self.archive.state("last_run"), restored.state("last_run"))
        self.assertEqual(self.archive.versions(doc["id"]), restored.versions(doc["id"]))
        public = json.loads((self.snapshot / "api/lists.json").read_text(encoding="utf-8"))["documents"][0]
        self.assertEqual(xml("AM 13"), (self.snapshot / public["download"]).read_bytes())
        self.assertFalse(public["download"].startswith("/"))
        self.assertTrue((self.snapshot / ".nojekyll").exists())

    def test_failed_ep_check_is_publicly_visible_without_false_success(self):
        ep = FakeEP()
        ep.page = 403, {}, b""
        poller.Poller(self.archive, request=ep, pause=0).once()
        status = cloud.export(self.archive, self.snapshot)
        self.assertFalse(status["ready"])
        self.assertEqual(0, status["with_xml"])
        page = (self.snapshot / "index.html").read_text(encoding="utf-8")
        self.assertIn("EP source access unavailable", page)
        self.assertIn("HTTP 403", page)
        restored = poller.Archive(self.root / "restored")
        cloud.restore(restored, self.snapshot)
        self.assertEqual(self.archive.state("listing_retry"), restored.state("listing_retry"))

    def test_tampered_xml_is_rejected_and_restore_rolls_back(self):
        poller.Poller(self.archive, request=FakeEP(), pause=0).once()
        cloud.export(self.archive, self.snapshot)
        next((self.snapshot / "_state/objects").glob("*.xml")).write_bytes(b"tampered")
        restored = poller.Archive(self.root / "restored")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            cloud.restore(restored, self.snapshot)
        self.assertEqual([], restored.lists())

    def test_empty_bootstrap_is_waiting_and_contains_no_xml(self):
        status = cloud.export(self.archive, self.snapshot)
        self.assertFalse(status["ready"])
        self.assertIn("Waiting for the first cloud check", (self.snapshot / "index.html").read_text())
        self.assertFalse(list(self.snapshot.rglob("*.xml")))


if __name__ == "__main__":
    unittest.main()
