"""Public EP pre-vote WordML collector. Python standard library only."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree as ET
from xml.parsers import expat

LISTING = "https://www.europarl.europa.eu/plenary/en/votes.html?tab=order"
HOST = "www.europarl.europa.eu"
WORD = "http://schemas.microsoft.com/office/word/2003/wordml"
W = "{" + WORD + "}"
MAX_BYTES = 20 * 1024 * 1024
PUBLIC_HEADERS = {"etag", "last-modified", "content-type", "content-length", "cache-control",
                  "date", "expires", "location", "retry-after", "content-encoding"}
LOG = logging.getLogger("ep-poller")


def stamp(now=None):
    return dt.datetime.fromtimestamp(now if now is not None else time.time(), dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def retry_delay(count, interval, now, headers):
    delay = min(3600, interval * 2 ** min(count - 1, 6))
    retry = headers.get("retry-after", "")
    if retry:
        try:
            from email.utils import parsedate_to_datetime
            delay = max(delay, int(retry) if retry.isdigit() else
                        parsedate_to_datetime(retry).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            pass
    return min(86400, delay)


def allowed_url(url):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != HOST or parts.port
                or parts.username or parts.password or parts.fragment):
            return False
        path = unquote(parts.path)
        if "\\" in path or ".." in path or "%" in path:
            return False
        return path == "/plenary/en/votes.html" or bool(
            re.fullmatch(r"/sedcms/votingList/[^/]+\.(?:xml|docx|pdf)", path, re.I))
    except ValueError:
        return False


class ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.documents = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        url = urljoin(LISTING, href)
        if not allowed_url(url):
            return
        parts = urlsplit(url)
        path = unquote(parts.path)
        if not re.fullmatch(r"/sedcms/votingList/[^/]+\.(?:xml|docx|pdf)", path, re.I):
            return
        # The publication's version= query is a label, not the document identity.
        xml_path = re.sub(r"\.(?:xml|docx|pdf)$", ".xml", path, flags=re.I)
        xml_url = urlunsplit(("https", HOST, quote(xml_path, safe="/-._~"), "", ""))
        item = self.documents.setdefault(xml_url, {
            "url": xml_url, "name": xml_path.rsplit("/", 1)[1], "sources": [], "labels": []})
        if url not in item["sources"]:
            item["sources"].append(url)
        for label in parse_qs(parts.query).get("version", []):
            if label not in item["labels"]:
                item["labels"].append(label)


def discover(raw):
    text = raw.decode("utf-8-sig", "replace")
    if re.search(r"verify (?:that )?you(?:'|’)re not a robot|captcha|javascript is disabled", text, re.I):
        raise ValueError("EP returned an access challenge, not a voting-list catalogue")
    parser = ListingParser()
    parser.feed(text)
    if not parser.documents:
        raise ValueError("No voting-list links found; catalogue unavailable or page structure changed")
    return list(parser.documents.values())


def wordml_lines(raw):
    """Conservative text/structure projection; original bytes remain authoritative.

    Keep paragraph and table/cell boundaries, tabs, breaks, numbering and cell
    properties. Ignore run styling and document metadata. This is not a complete
    semantic EP schema and never substitutes for the archived original XML.
    """
    if len(raw) > MAX_BYTES:
        raise ValueError("XML exceeds size limit")
    validator = expat.ParserCreate()
    depth = 0

    def start(_name, _attrs):
        nonlocal depth
        depth += 1
        if depth > 128:
            raise ValueError("XML nesting exceeds limit")

    def end(_name):
        nonlocal depth
        depth -= 1

    def reject(*_args):
        raise ValueError("DTD and entity declarations are not accepted")

    validator.StartElementHandler = start
    validator.EndElementHandler = end
    validator.StartDoctypeDeclHandler = reject
    validator.EntityDeclHandler = reject
    validator.ExternalEntityRefHandler = reject
    validator.Parse(raw, True)
    root = ET.fromstring(raw)
    body = root.find(W + "body")
    if root.tag != W + "wordDocument" or body is None or body.find(".//" + W + "tbl") is None:
        raise ValueError("Expected Word 2003 WordML with a body and voting tables")

    def tree(el):
        return [el.tag, sorted(el.attrib.items()), el.text or "", [tree(c) for c in el]]

    lines = []
    # Transparent wrappers (including Word auxiliary sections) retain order.
    def visit(el):
        if el.tag == W + "p":
            numbering = el.find(W + "pPr/" + W + "listPr")
            if numbering is not None:
                lines.append("numbering " + compact(tree(numbering)))
            pieces = []
            for part in el.iter():
                if part.tag == W + "t":
                    pieces.append(part.text or "")
                elif part.tag == W + "tab":
                    pieces.append("\t")
                elif part.tag in (W + "br", W + "cr"):
                    pieces.append("\n")
            lines.append("paragraph " + compact("".join(pieces)))
            return
        structural = el.tag in (W + "tbl", W + "tr", W + "tc")
        if structural:
            name = el.tag.split("}")[-1]
            lines.append("begin " + name)
            if name == "tc":
                properties = el.find(W + "tcPr")
                if properties is not None:
                    lines.append("cell-properties " + compact(tree(properties)))
        for child in el:
            if child.tag not in (W + "pPr", W + "rPr", W + "tblPr", W + "tblGrid",
                                  W + "trPr", W + "tcPr", W + "sectPr"):
                visit(child)
        if structural:
            lines.append("end " + name)

    visit(body)
    if not any(line.startswith("paragraph ") and line != 'paragraph ""' for line in lines):
        raise ValueError("WordML contains no text")
    return lines


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, headers=None, timeout=25):
    """Bounded GETs; revalidate every redirect, including its origin."""
    opener = build_opener(NoRedirects())
    deadline = time.monotonic() + timeout
    for _ in range(4):
        if not allowed_url(url):
            raise ValueError("Unapproved EP address or redirect")
        request = Request(url, headers={
            "User-Agent": os.environ.get("EP_USER_AGENT", "EP-VL-XML-Poller/1.0 (public pre-vote archive)"),
            "Accept": "application/xml,text/xml,text/html;q=0.8,*/*;q=0.1",
            "Accept-Encoding": "identity", **(headers or {})})
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("EP request deadline exceeded")
            response = opener.open(request, timeout=remaining)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            response_headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in PUBLIC_HEADERS}
            if status in (301, 302, 303, 307, 308):
                url = urljoin(url, response_headers.get("location", ""))
                continue
            if status != 200:
                return status, response_headers, b""
            if int(response_headers.get("content-length", "0")) > MAX_BYTES:
                raise ValueError("EP response exceeds size limit")
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("EP response exceeds size limit")
            return status, response_headers, raw
    raise ValueError("Too many EP redirects")


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 sources TEXT NOT NULL, labels TEXT NOT NULL, first_seen REAL NOT NULL,
 last_seen REAL NOT NULL, listed INTEGER NOT NULL DEFAULT 1,
 last_check REAL, last_success REAL, last_full REAL, latest_version INTEGER,
 etag TEXT, last_modified TEXT, failures INTEGER NOT NULL DEFAULT 0,
 next_due REAL NOT NULL DEFAULT 0, http_status INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS objects (
 sha TEXT PRIMARY KEY, xml BLOB NOT NULL, projection TEXT NOT NULL, content_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
 id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 sha TEXT NOT NULL REFERENCES objects(sha), fetched_at REAL NOT NULL,
 kind TEXT NOT NULL, headers TEXT NOT NULL, labels TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS version_document ON versions(document_id, id);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started REAL NOT NULL, summary TEXT NOT NULL);
"""


class Archive:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "archive.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, compact(value)))

    def catalogue(self, items, now):
        with self.connect() as db:
            db.execute("UPDATE documents SET listed=0")
            for item in items:
                db.execute("""INSERT INTO documents
                    (id,url,name,sources,labels,first_seen,last_seen) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(url) DO UPDATE SET sources=excluded.sources,
                    labels=excluded.labels,last_seen=excluded.last_seen,listed=1""",
                    (digest(item["url"].encode())[:24], item["url"], item["name"],
                     compact(item["sources"]), compact(item["labels"]), now, now))

    def due(self, now, track_days):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM documents
                WHERE (listed=1 OR last_seen>=?) AND next_due<=? ORDER BY last_check IS NOT NULL,last_check,id""",
                (now - track_days * 86400, now))]

    def fail(self, document, status, error, now, interval, headers):
        count = document["failures"] + 1
        delay = retry_delay(count, interval, now, headers)
        with self.connect() as db:
            db.execute("""UPDATE documents SET last_check=?,http_status=?,error=?,failures=?,next_due=?
                WHERE id=?""", (now, status, error[:1000], count, now + delay, document["id"]))

    def accept(self, document, status, headers, raw, now):
        with self.connect() as db:
            if status == 304:
                if not document["latest_version"]:
                    raise ValueError("Received 304 before any XML was archived")
                kind = "unchanged"
                etag = headers.get("etag", document["etag"])
                modified = headers.get("last-modified", document["last_modified"])
                full = document["last_full"]
                version = document["latest_version"]
            else:
                projection = wordml_lines(raw)
                sha = digest(raw)
                content_sha = digest(compact(projection).encode())
                previous = db.execute("""SELECT v.sha,o.content_sha FROM versions v
                    JOIN objects o ON o.sha=v.sha WHERE v.id=?""", (document["latest_version"],)).fetchone()
                version = document["latest_version"]
                kind = "unchanged"
                if not previous or previous["sha"] != sha:
                    kind = ("initial" if not previous else "text_or_structure"
                            if previous["content_sha"] != content_sha else "other_xml")
                    db.execute("INSERT OR IGNORE INTO objects VALUES (?,?,?,?)",
                               (sha, raw, compact(projection), content_sha))
                    cursor = db.execute("""INSERT INTO versions
                        (document_id,sha,fetched_at,kind,headers,labels) VALUES (?,?,?,?,?,?)""",
                        (document["id"], sha, now, kind, compact(headers), document["labels"]))
                    version = cursor.lastrowid
                etag = headers.get("etag")
                modified = headers.get("last-modified")
                full = now
            db.execute("""UPDATE documents SET last_check=?,last_success=?,last_full=?,latest_version=?,
                etag=?,last_modified=?,failures=0,next_due=0,http_status=?,error=NULL WHERE id=?""",
                (now, now, full, version, etag, modified, status, document["id"]))
        return kind

    def lists(self):
        with self.connect() as db:
            rows = db.execute("""SELECT d.*,v.sha,v.kind,v.fetched_at,
                (SELECT COUNT(*) FROM versions x WHERE x.document_id=d.id) version_count
                FROM documents d LEFT JOIN versions v ON v.id=d.latest_version
                ORDER BY d.listed DESC,d.last_seen DESC,d.name""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["sources"] = json.loads(item["sources"])
            item["labels"] = json.loads(item["labels"])
            item["listed"] = bool(item["listed"])
            item["xml_available"] = item["latest_version"] is not None
            item["download"] = (f'/xml/{item["id"]}/{item["latest_version"]}.xml'
                                if item["xml_available"] else None)
            result.append(item)
        return result

    def versions(self, doc_id):
        with self.connect() as db:
            rows = db.execute("""SELECT id,document_id,sha,fetched_at,kind,headers,labels FROM versions
                WHERE document_id=? ORDER BY id DESC""", (doc_id,)).fetchall()
        return [{**dict(r), "headers": json.loads(r["headers"]), "labels": json.loads(r["labels"]),
                 "download": f'/xml/{doc_id}/{r["id"]}.xml'} for r in rows]

    def version(self, doc_id, version_id):
        with self.connect() as db:
            row = db.execute("""SELECT o.*,d.name FROM versions v JOIN objects o ON o.sha=v.sha
                JOIN documents d ON d.id=v.document_id WHERE v.id=? AND v.document_id=?""",
                (version_id, doc_id)).fetchone()
        return dict(row) if row else None

    def record_run(self, summary):
        with self.connect() as db:
            db.execute("INSERT INTO runs(started,summary) VALUES (?,?)", (summary["started"], compact(summary)))
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 1000)")
        self.set_state("last_run", summary)

    def status(self, interval):
        last = self.state("last_run")
        documents = self.lists()
        success = self.state("last_success")
        fresh = success is not None and time.time() - success <= max(900, interval * 3)
        return {"service": "EP public pre-vote XML poller", "source": LISTING,
                "now": stamp(), "interval_seconds": interval, "ready": bool(fresh and last and last["ok"]),
                "last_success": stamp(success) if success else None, "last_run": last,
                "documents": len(documents), "listed": sum(d["listed"] for d in documents),
                "with_xml": sum(d["xml_available"] for d in documents),
                "pending_xml": sum(not d["xml_available"] and d["http_status"] in (404, 410) for d in documents),
                "errors": sum(bool(d["error"]) and d["http_status"] not in (404, 410) for d in documents)}


class Poller:
    def __init__(self, archive, interval=300, track_days=30, full_seconds=86400,
                 request=fetch, pause=0.5):
        self.archive, self.interval, self.track_days = archive, interval, track_days
        self.full_seconds, self.request, self.pause = full_seconds, request, pause

    def once(self, stop=None):
        now = time.time()
        summary = {"started": now, "started_at": stamp(now), "ok": False, "discovery_ok": False,
                   "discovered": 0, "checked": 0, "initial": 0, "text_or_structure": 0,
                   "other_xml": 0, "unchanged": 0, "pending_xml": 0, "errors": []}
        listing_retry = self.archive.state("listing_retry", {})
        if listing_retry.get("due", 0) > now:
            summary["errors"].append("Catalogue in retry backoff after: " + listing_retry.get("error", "failure"))
        else:
            headers = {}
            try:
                status, headers, raw = self.request(LISTING)
                if status != 200:
                    raise ValueError(f"Catalogue HTTP {status}")
                items = discover(raw)
                self.archive.catalogue(items, now)
                self.archive.set_state("listing_retry", {})
                self.archive.set_state("catalogue", {"checked_at": stamp(now), "sha256": digest(raw)})
                summary.update(discovery_ok=True, discovered=len(items))
            except Exception as error:
                count = listing_retry.get("failures", 0) + 1
                delay = retry_delay(count, self.interval, now, headers)
                self.archive.set_state("listing_retry", {"due": now + delay, "failures": count, "error": str(error)[:1000]})
                summary["errors"].append("Discovery failed: " + str(error)[:1000])
        # Known URLs are checked independently, even when catalogue retrieval fails.
        for document in self.archive.due(now, self.track_days):
            if stop and stop.is_set():
                summary["errors"].append("Poll interrupted")
                break
            headers = {}
            if document["latest_version"] and now - (document["last_full"] or 0) < self.full_seconds:
                if document["etag"]:
                    headers["If-None-Match"] = document["etag"]
                if document["last_modified"]:
                    headers["If-Modified-Since"] = document["last_modified"]
            status, response_headers = None, {}
            try:
                status, response_headers, raw = self.request(document["url"], headers)
                summary["checked"] += 1
                if status not in (200, 304):
                    raise ValueError(f"XML HTTP {status}")
                kind = self.archive.accept(document, status, response_headers, raw, time.time())
                summary[kind] += 1
            except Exception as error:
                self.archive.fail(document, status, str(error), time.time(), self.interval, response_headers)
                if status in (404, 410):
                    summary["pending_xml"] += 1
                else:
                    summary["errors"].append({"id": document["id"], "message": str(error)[:1000]})
            if self.pause:
                if stop:
                    stop.wait(self.pause)
                else:
                    time.sleep(self.pause)
        # Backed-off failures must still show as unhealthy on subsequent runs.
        outstanding = [d for d in self.archive.lists() if d["error"] and d["http_status"] not in (404, 410)
                       and (d["listed"] or d["last_seen"] >= now - self.track_days * 86400)]
        summary["outstanding_errors"] = len(outstanding)
        summary["ok"] = summary["discovery_ok"] and not summary["errors"] and not outstanding
        summary["finished_at"] = stamp()
        if summary["ok"]:
            self.archive.set_state("last_success", time.time())
        self.archive.record_run(summary)
        LOG.info("poll %s", compact(summary))
        return summary


@contextlib.contextmanager
def writer_lock(archive):
    """A process-lifetime OS lock prevents overlapping writers, including --once."""
    handle = open(archive.directory / "writer.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise RuntimeError("Another poller is already using this archive") from error
    try:
        yield
    finally:
        handle.close()


def handler_for(archive, interval):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EPXML/1.0"

        def log_message(self, fmt, *args):
            LOG.info("http %s", fmt % args)

        def respond(self, status, value, content_type="application/json; charset=utf-8", extra=None):
            data = value if isinstance(value, bytes) else (compact(value) + "\n").encode()
            self.send_response(status)
            for key, val in {"Content-Type": content_type, "Content-Length": str(len(data)),
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                             "Content-Security-Policy": "default-src 'none'; sandbox", **(extra or {})}.items():
                self.send_header(key, val)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                self.route()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                LOG.exception("API request failed")
                self.respond(500, {"error": "Archive request failed"})

        def route(self):
            request = urlsplit(self.path)
            path = request.path
            if path == "/":
                return self.respond(200, {"service": "EP pre-vote XML archive", "read_only": True,
                    "endpoints": ["/healthz", "/readyz", "/api/status", "/api/lists",
                                  "/api/lists/{id}/versions", "/api/lists/{id}/diff",
                                  "/xml/{id}/{version}.xml"]})
            if path == "/healthz":
                return self.respond(200, {"alive": True})
            if path in ("/api/status", "/readyz"):
                result = archive.status(interval)
                return self.respond(503 if path == "/readyz" and not result["ready"] else 200, result)
            if path == "/api/lists":
                return self.respond(200, {"documents": archive.lists()})
            match = re.fullmatch(r"/api/lists/([a-f0-9]{24})/(versions|diff)", path)
            if match:
                doc_id, action = match.groups()
                versions = archive.versions(doc_id)
                if not versions:
                    return self.respond(404, {"error": "No archived XML versions for this document"})
                if action == "versions":
                    return self.respond(200, {"versions": versions})
                query = parse_qs(request.query)
                try:
                    newer = int(query.get("to", [versions[0]["id"]])[0])
                    older = int(query.get("from", [versions[1]["id"] if len(versions) > 1 else versions[0]["id"]])[0])
                except ValueError:
                    return self.respond(400, {"error": "from and to must be version IDs"})
                a, b = archive.version(doc_id, older), archive.version(doc_id, newer)
                if not a or not b:
                    return self.respond(404, {"error": "Version does not belong to this document"})
                diff = list(difflib.unified_diff(json.loads(a["projection"]), json.loads(b["projection"]),
                                               fromfile=str(older), tofile=str(newer), lineterm=""))
                return self.respond(200, {"from": older, "to": newer,
                    "raw_changed": a["sha"] != b["sha"], "text_or_structure_changed": a["content_sha"] != b["content_sha"],
                    "projection_note": "Conservative WordML comparison; inspect originals for other XML changes.",
                    "truncated": len(diff) > 2000, "diff": diff[:2000]})
            match = re.fullmatch(r"/xml/([a-f0-9]{24})/([1-9][0-9]*)\.xml", path)
            if match:
                result = archive.version(match[1], int(match[2]))
                if not result:
                    return self.respond(404, {"error": "XML version not found"})
                return self.respond(200, result["xml"], "application/xml",
                    {"Content-Disposition": f"attachment; filename=\"{match[1]}.xml\"; filename*=UTF-8''{quote(result['name'], safe='')}",
                     "ETag": '"' + result["sha"] + '"'})
            self.respond(404, {"error": "Unknown endpoint"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("once", "serve", "status"), nargs="?", default="serve")
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "./data"))
    parser.add_argument("--interval", type=int, default=int(os.environ.get("POLL_SECONDS", "300")))
    parser.add_argument("--track-days", type=int, default=int(os.environ.get("TRACK_DAYS", "30")))
    parser.add_argument("--host", default=os.environ.get("BIND_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()
    if not 60 <= args.interval <= 86400 or not 1 <= args.track_days <= 365:
        parser.error("interval must be 60–86400 seconds and track-days must be 1–365")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    archive = Archive(args.data_dir)
    if args.command == "status":
        print(json.dumps(archive.status(args.interval), indent=2))
        return 0
    with writer_lock(archive):
        poller = Poller(archive, interval=args.interval, track_days=args.track_days)
        if args.command == "once":
            summary = poller.once()
            print(json.dumps(summary, indent=2))
            return 0 if summary["ok"] else 1
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        server = ThreadingHTTPServer((args.host, args.port), handler_for(archive, args.interval))
        server.daemon_threads = True
        server.timeout = 1

        def collect():
            while not stop.is_set():
                try:
                    poller.once(stop)
                except Exception:
                    LOG.exception("Poll failed unexpectedly; preserving archive and retrying next interval")
                stop.wait(args.interval)

        worker = threading.Thread(target=collect, name="collector", daemon=True)
        worker.start()
        LOG.info("Read-only archive listening on %s:%s; polling every %ss", args.host, args.port, args.interval)
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            stop.set()
            server.server_close()
            worker.join(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
