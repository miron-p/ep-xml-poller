# Verification — 12 November 2026

## Passed

- `python -m unittest discover -s tests -v`: **29 tests passed** on Windows,
  Python 3.10.11. The complete suite ran outside the sandbox to permit loopback
  HTTP connections; its EP responses are controlled test doubles, not live traffic.
- Python syntax parsing passed for both source files.
- Both Compose files parsed as YAML, with the expected `poller` / `caddy` services.
  This is not equivalent to `docker compose config` or a container build.
- Discovery against an unchanged saved official EP catalogue found **11** unique
  XML candidate URLs, including Wolters, Clergeau and Vasile-Voiculescu. A separate
  manual check against the older saved September catalogue found **48** candidates.
- The shipped November fixture matches the existing saved catalogue byte for
  byte. SHA-256:
  `8c3ee872269d661b4e85482ad17d409d7b90da6de2eaa8756ffc1fd13260ae7b`.

Covered behavior: label/path deduplication, unusual filenames, rejected non-EP
and post-vote links, tabs/breaks/paragraphs and nested table/merge boundaries,
styling-only differences, UTF-16 byte preservation, DTD/entity rejection,
conditional requests and 304 handling, daily unconditional GETs, same-label
revisions, reversions, persistent history after reopening SQLite, 404-then-200,
invalid XML preserving good bytes/validators, discovery failure preserving known
URLs, independent known-URL checks, 30-day unlisted monitoring, Retry-After and
backoff health, timeouts, process locks, live loopback read-only HTTP routes,
exact-byte downloads, diff output, readiness/staleness and rejected write methods.
Response-size limits and exclusion of cookies from archived response headers are
also covered.

The actual `serve` command was briefly started as a separate local process with
the failed live-test archive. `/healthz` returned 200, `/api/status` exposed the
failed poll, and `/readyz` returned 503. The test process was stopped afterward;
no local scheduled collection was left running.

## Live source tests

Direct GETs to the public voting-order listing and the Wolters XML returned
**HTTP 403** using the laptop's network outside the sandbox. The finished CLI
was then exercised with a separate empty runtime archive:

```text
python poller.py once --data-dir ../.tmp/ep-xml-poller-live
2026-11-12T19:50:35Z
exit code: 1
ok: false
discovery_ok: false
discovered: 0
checked: 0
error: Discovery failed: Catalogue HTTP 403
```

This verifies that the finished collector reports access failure instead of
reporting "no changes" or seeding an empty successful catalogue. It does not
prove that public-server retrieval will succeed or fail. Initial sandbox-only
attempts failed DNS; those were superseded by the direct network tests above.

## Still to verify on the deployment host

- Docker image build, Compose resolution, startup, volume permissions and restart
  behavior. Docker is not installed on this laptop.
- Successful live discovery and a real XML download from that host's network.
- Parsing and comparison against real public WordML files. WordML test payloads
  in this release are synthetic; the real saved-page fixture validates discovery
  only. Original XML bytes are retained regardless of the comparison projection.
- Public DNS, TLS certificate issuance and proxy connectivity, if using Caddy.

No public service, scheduled automation or paid server was created. Runtime test
databases, user documents, S&D drafts and working packages are excluded from the
delivery ZIP. The committee HTML and its embedded library were not edited.
