"""GitHub-hosted collector: persist public data as files, not a binary database."""
import argparse
import html
import json
import os
from pathlib import Path
import re

import poller

TABLES = ("documents", "versions", "state", "runs")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def restore(archive, snapshot):
    source = snapshot / "_state/state.json"
    if not source.exists():
        return
    payload = json.loads(source.read_text(encoding="utf-8"))
    if payload.get("schema") != 1:
        raise ValueError("Unsupported public snapshot schema")
    with archive.connect() as db:
        if db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]:
            raise ValueError("Restore requires a fresh temporary database")
        for obj in payload["objects"]:
            sha = obj["sha"]
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("Invalid snapshot object name")
            raw = (snapshot / "_state/objects" / (sha + ".xml")).read_bytes()
            if len(raw) > poller.MAX_BYTES or poller.digest(raw) != sha:
                raise ValueError("Snapshot XML hash mismatch")
            if poller.digest(obj["projection"].encode()) != obj["content_sha"]:
                raise ValueError("Snapshot projection hash mismatch")
            db.execute("INSERT INTO objects VALUES (?,?,?,?)", (sha, raw, obj["projection"], obj["content_sha"]))
        for table in TABLES:
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for row in payload[table]:
                if set(row) != set(columns):
                    raise ValueError("Snapshot columns do not match the collector schema")
                marks = ",".join("?" for _ in columns)
                db.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})", [row[c] for c in columns])


def export(archive, snapshot, interval=600):
    snapshot.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "objects": []}
    with archive.connect() as db:
        for table in TABLES:
            payload[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for row in db.execute("SELECT * FROM objects ORDER BY sha"):
            item = dict(row)
            raw = item.pop("xml")
            target = snapshot / "_state/objects" / (item["sha"] + ".xml")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(raw)
            payload["objects"].append(item)
    write_json(snapshot / "_state/state.json", payload)
    status = archive.status(interval)
    status.update(hosting="GitHub standard cloud runners + GitHub Pages", best_effort_schedule=True,
                  snapshot_at=poller.stamp(), ready_is_at_snapshot_time=True,
                  hosting_note="Scheduled collection; no permanently running Docker/API server.")
    write_json(snapshot / "api/status.json", status)
    documents = archive.lists()
    for document in documents:
        if document["download"]:
            document["download"] = document["download"].lstrip("/")
        versions = archive.versions(document["id"])
        for version in versions:
            version["download"] = version["download"].lstrip("/")
            target = snapshot / version["download"]
            if not target.exists():
                value = archive.version(document["id"], version["id"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value["xml"])
        write_json(snapshot / "api/lists" / document["id"] / "versions.json", {"versions": versions})
    write_json(snapshot / "api/lists.json", {"documents": documents})
    (snapshot / ".nojekyll").write_text("", encoding="utf-8")
    last = status["last_run"] or {}
    title = "Source checks successful" if status["ready"] else "EP source access unavailable"
    if not last:
        title = "Waiting for the first cloud check"
    errors = last.get("errors", [])
    error_text = "\n".join(item if isinstance(item, str) else poller.compact(item) for item in errors)
    entries = []
    for item in documents:
        download = (f'<a href="{html.escape(item["download"], quote=True)}">Download XML</a>'
                    if item["download"] else "XML not yet available")
        entries.append("<tr><td>" + html.escape(item["name"]) + "</td><td>" +
                       html.escape(", ".join(item["labels"])) + "</td><td>" + download + "</td><td>" +
                       html.escape(item["error"] or "") + "</td></tr>")
    page = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EP pre-vote XML archive</title><style>
body{font:17px/1.6 system-ui,sans-serif;max-width:1050px;margin:48px auto;padding:0 24px;color:#202733}
h1{line-height:1.2}h2{font-size:1.3rem}.status{border-left:5px solid #b45100;background:#fff6e9;padding:20px}
pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;font-size:.9rem}
td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}a{color:#174f9d}.muted{color:#596273}
</style><h1>European Parliament<br>Pre-vote XML archive</h1>
<p class="muted">Public voting lists only · Automatic cloud checks · Original XML retained</p>
<div class="status"><h2>__TITLE__</h2><p>Last collection attempt: __CHECKED__</p><pre>__ERRORS__</pre></div>
<p>Listed documents: __LISTED__ · Documents with archived XML: __AVAILABLE__</p>
<p>Checks are requested every 10 minutes. GitHub may delay or skip scheduled runs.
This page is a snapshot: check the time above. Intermediate revisions between checks can be missed.</p>
<table><thead><tr><th>Voting list</th><th>EP label</th><th>XML</th><th>Source status</th></tr></thead><tbody>__ROWS__</tbody></table>
<p><a href="api/status.json">Full status JSON</a> · <a href="api/lists.json">List catalogue JSON</a> ·
<a href="https://github.com/miron-p/ep-xml-poller/actions">Cloud run history</a> ·
<a href="https://www.europarl.europa.eu/plenary/en/votes.html?tab=order">Official EP listing</a></p>
<p class="muted">This is an independent collector, not an EP publication service. An archived file does not
prove it is the latest editorial version. No S&amp;D working documents or voting indications are hosted.</p></html>'''
    for key, value in {"__TITLE__": html.escape(title), "__CHECKED__": html.escape(last.get("started_at", "Not checked")),
                       "__ERRORS__": html.escape(error_text), "__LISTED__": str(status["listed"]),
                       "__AVAILABLE__": str(status["with_xml"]), "__ROWS__": "".join(entries)}.items():
        page = page.replace(key, value)
    (snapshot / "index.html").write_text(page, encoding="utf-8")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    archive = poller.Archive(args.data_dir)
    restore(archive, args.snapshot)
    if not args.export_only:
        with poller.writer_lock(archive):
            poller.Poller(archive, interval=600).once()
    status = export(archive, args.snapshot)
    print(json.dumps(status, indent=2))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as target:
            target.write("exported=true\n")
            target.write("source_ready=" + str(status["ready"]).lower() + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as target:
            target.write("## EP source " + ("checks successful" if status["ready"] else "unavailable") + "\n\n")
            target.write("```json\n" + json.dumps(status, indent=2) + "\n```\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
