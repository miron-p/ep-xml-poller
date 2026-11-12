# EP pre-vote XML poller

A separate, portable server service that discovers published European Parliament
**pre-vote plenary voting lists**, checks their XML equivalents, and keeps every
observed revision. It does not change or depend on the committee voting-list app.
No ELVIS, VPN, EP credentials, S&D drafts or voting indications are involved.

## Deployment status — 12 November 2026

The [public hosted edition](https://miron-p.github.io/ep-xml-poller/) uses scheduled
GitHub cloud runners and GitHub Pages, with checks requested **every 10 minutes**.
It is a scheduled collector and static download/status service, rather than an
always-running Docker server. Check the page's timestamp and source status.

The first [cloud validation](https://github.com/miron-p/ep-xml-poller/actions/runs/43296555134)
passed all 29 original tests on Linux/Python 3.12, resolved both Compose files,
and built/ran the Docker image. **The EP listing and all three tested XML files
returned HTTP 403 from that cloud runner. No live XML was retrieved.**
The laptop's direct network tests also received 403. A different host is not a
guaranteed fix; the publication service must accept requests from the collector.

The Docker edition remains available for a server with persistent storage.
It uses Python 3.12 and the standard library only. No application packages are
needed. The free hosted edition preserves its state as JSON and exact XML files
on the separate `archive` branch, restoring a temporary SQLite database for each
run. It uses no Actions artifacts, paid runners, external databases or credentials.

Standard runners in public repositories and public GitHub Pages are available
free under the applicable limits:
[Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits).
Scheduled runs may be delayed or skipped; GitHub can disable them after prolonged
repository inactivity. This service is not suitable for guaranteed real-time
notifications. Monitor repository/site size; GitHub Pages has a 1 GB site limit.

The hosted JSON endpoints are `api/status.json`, `api/lists.json` and
`api/lists/{id}/versions.json`, relative to the public page. The status is a
snapshot, including `snapshot_at`; `ready` describes that snapshot's source
health. Successful workflow completion means the result was saved/published,
not that EP retrieval succeeded. Failed source checks stay explicit on the page.
The dynamic API and comparison endpoints described below belong to the Docker
edition. The hosted edition links to retained versions for inspection.

To request a check or stop automatic checks:

```sh
gh workflow run poll.yml --repo miron-p/ep-xml-poller
gh workflow disable poll.yml --repo miron-p/ep-xml-poller
```

## Deploy on a server with Docker Compose

Use an always-running Linux server with persistent storage and outbound HTTPS.
Copy **this folder only** to the server, then run these commands from it.

First build and test one real collection pass:

```sh
docker compose build
docker compose run --rm --no-deps poller python poller.py once
```

An exit code of 0 means discovery and the due XML checks completed without an
unexpected failure. XML that does not yet exist (404/410) is recorded as pending
and will be retried. Inspect `initial`, `pending_xml`, `errors` and `discovered`
in the printed summary. A successful discovery alone does not prove XML downloads
work. Confirm that at least one real XML is archived before using this live.

If the server receives 403, a challenge page or repeated connection failures,
stop here and resolve access with the hosting provider/EP publication service.
This service does not bypass an access challenge or conceal failed checks.

Once the server can retrieve the source:

```sh
docker compose up -d
curl --fail http://127.0.0.1:8000/api/status
curl --fail http://127.0.0.1:8000/api/lists
docker compose logs --tail=50 poller
```

The server collects immediately at startup, then waits **five minutes after each
completed pass** before starting another. It restarts after crashes/reboots while
the Docker daemon is running. Use `docker compose stop` to stop collection and
`docker compose start` to resume. Do not run a second writer against the same
archive; a process lock rejects overlapping `once`/`serve` commands.

SQLite and all original XML bytes are stored in the `ep-xml-data` named volume,
outside the disposable container. Rebuilding a container retains that volume.
Do not use `docker compose down -v`: that deletes the archive volume. Arrange a
backup of the volume, ideally while the collector is stopped; do not copy only
the main SQLite file while it is live because its WAL may contain recent data.

## Optional public HTTPS address

The base configuration exposes port 8000 only to the server itself. If you want
a public download/API address, point a domain's DNS at the server, allow inbound
TCP 80/443, and start the supplied Caddy reverse proxy:

```sh
export DOMAIN=vl-archive.your-domain.example
docker compose -f compose.yaml -f compose.public.yaml up -d
```

Replace the example with a real domain you control. Caddy manages the HTTPS
certificate when DNS and network access are correctly configured. Keep using
both `-f` arguments for later operations on this combined deployment. The API
is public and read-only; it serves only public EP XML and retrieval metadata.
There is no upload, user-provided fetch URL or remote "poll now" endpoint.

Official deployment references:
[Docker volumes](https://docs.docker.com/engine/storage/volumes/),
[Compose file merging](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/),
[Caddy with Compose](https://caddyserver.com/docs/running#docker-compose).

## What it checks and retains

1. GET the [official voting-order listing](https://www.europarl.europa.eu/plenary/en/votes.html?tab=order).
   Discover PDF/DOCX/XML links under `/sedcms/votingList/`; derive `.xml` from
   their advertised names. No report-number filename guesses or post-vote result
   feeds. PDF/DOCX links are discovery evidence, not downloaded content.
2. Treat the XML path as the identity. A changed `version=` label does not create
   a duplicate document. Renamed paths appear as separate entries; no unverified
   cross-filename matching is performed. Source URLs and observed labels remain
   in the archive.
3. Revalidate known XML URLs independently using ETag/Last-Modified, including
   when listing retrieval fails. Force a full GET at least once per day instead
   of relying indefinitely on validators. Unlisted URLs remain monitored for
   30 days after they were last seen. Previously saved versions are never expired.
4. Validate Word 2003 WordML with a body, a table and text. Reject malformed XML,
   HTML challenges, other XML formats, DTDs/entities, oversized responses and
   unapproved redirects. A rejected response cannot replace good XML/validators.
5. Save each raw-byte change transactionally, with SHA-256, headers, source label
   and retrieval time. Identical bytes create no additional revision. Reverting
   to older bytes creates a new history entry; stored payloads are deduplicated.
6. Compute a separate text/table projection for comparison. It retains ordered
   paragraphs, tabs, line breaks, nested tables, rows, cells, numbering properties
   and cell properties (including merges). Run styling and document metadata are
   ignored by that projection. This is a conservative comparison, **not a full
   semantic voting-list schema**. Changes elsewhere still retain a raw revision
   classified `other_xml`; inspect the original files for those changes.
7. Retry missing XML and failed requests with per-resource backoff, normally up
   to an hour. Honour a longer Retry-After, capped at a day. Delay successive XML
   requests by 0.5 seconds. Retain good data and show retrieval errors explicitly.

Polling can miss intermediate versions replaced between checks. A 304 is the
publication server's assertion, not independent proof of the latest editorial
state. Daily full retrieval reduces reliance on validators but cannot guarantee
the freshness of upstream caches. Retrieval times are not publication timestamps.
The service finds the lists exposed by the current English listing, not a full
historical or multilingual plenary archive.

## Read-only API

| Path | Returns |
| --- | --- |
| `/api/status` | Last run, source health, pending/error counts, last success |
| `/api/lists` | Discovered lists, source links, current status and download URLs |
| `/api/lists/{id}/versions` | All observed XML revisions, newest first |
| `/api/lists/{id}/diff` | Text/table difference between the last two revisions |
| `/api/lists/{id}/diff?from=1&to=2` | Difference for two version IDs of this list |
| `/xml/{id}/{version}.xml` | Exact original XML bytes as a file download |
| `/healthz` | Process liveness; stays 200 during upstream failures |
| `/readyz` | 503 if the latest run failed or successful polling is stale |

Get IDs and version IDs from the lists/versions responses; the numbers in the
example are illustrative. Diff output is capped at 2,000 lines with an explicit
`truncated` flag. JSON times ending `_at` are ISO UTC; numeric check/due/seen
times are Unix seconds. `xml_available` means a valid copy has been archived;
always check `error`, `last_success` and `last_check` for current source status.
Pending XML alone does not make `/readyz` fail. Neither endpoint asserts the
publication service's editorial completeness. Logs retain 1,000 run summaries;
XML revision history has no automatic deletion. Monitor persistent disk usage.

## Configuration and development

`POLL_SECONDS` defaults to 300 (minimum 60, maximum 86400). `TRACK_DAYS` defaults
to 30 (range 1–365). Set them in the server shell or a Compose `.env` file before
starting the service. Keep one replica with its own persistent volume.
Sleeping hosting plans and ephemeral container disks do not provide reliable
unattended monitoring/history.

Without Docker, from this directory with Python 3.10+:

```sh
python poller.py once --data-dir ./data
python poller.py serve --data-dir ./data --interval 300
python poller.py status --data-dir ./data
python -m unittest discover -s tests -v
```

The server binds to localhost by default. Container defaults bind to all
container interfaces with port 8000. `BIND_HOST`, `PORT`, `DATA_DIR` and
`EP_USER_AGENT` can be set directly for other deployment arrangements. No CORS
header is enabled by default; integrate via a same-origin application server or
configure your own reverse proxy for the specific application origin.

See [VERIFICATION.md](VERIFICATION.md) for what was and was not verified.
