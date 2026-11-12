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

The original ZIP was not deployed at creation. The later cloud deployment and
its results are recorded below. Runtime test databases, user documents, S&D drafts
and working packages are excluded. The committee HTML and embedded library were
not edited.

## Public cloud deployment follow-up

With the user's explicit permission, the generic collector was published at
https://github.com/miron-p/ep-xml-poller. Only generic code and public EP material
were published. The first cloud run is
https://github.com/miron-p/ep-xml-poller/actions/runs/43296555134.

- Ubuntu 24.04 / Python 3.12: all 29 original tests passed.
- Both actual Docker Compose configurations resolved successfully.
- The Docker image built successfully and ran as its unprivileged user with a
  writable data volume.
- Independent GETs to the listing, Wolters XML, Clergeau XML and
  Vasile-Voiculescu XML all returned HTTP 403. The Docker collection pass also
  returned a failed discovery status, preserving that error.
- No successful live XML download or real WordML parsing is claimed.

The hosted adapter adds four persistence/publication tests: all **33 tests passed
locally**. They cover complete snapshot restore with revision history and
validators, exact-byte public downloads, failed-source status visibility,
corruption rejection and empty bootstrap status. The adapter stores versioned
public JSON/XML files instead of committing a changing binary SQLite database.

The scheduled deployment uses free standard public-repository runners and a
GitHub Pages public status/download page. Its ten-minute schedule is best-effort;
it has no permanently running API server. Public Pages snapshots show their
collection times and errors. Network access remains unverified on other hosts.
