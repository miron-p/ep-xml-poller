"""Small, read-only network probe for the deployment host; no credentials."""
import argparse
import json
import os
from pathlib import Path
import time
from xml.etree import ElementTree as ET

import poller


KNOWN_XML = [
    "https://www.europarl.europa.eu/sedcms/votingList/%28A10-0270_2026%29_Wolters.xml",
    "https://www.europarl.europa.eu/sedcms/votingList/%28A10-0093_2026%29_Clergeau.xml",
    "https://www.europarl.europa.eu/sedcms/votingList/A10-0261_Vasile-Voiculescu.xml",
]


def probe():
    report = {"checked_at": poller.stamp(), "runner": os.environ.get("RUNNER_OS", os.name),
              "listing_ok": False, "xml_ok": 0, "requests": []}
    try:
        status, headers, raw = poller.fetch(poller.LISTING)
        item = {"url": poller.LISTING, "http_status": status, "headers": headers}
        if status == 200:
            documents = poller.discover(raw)
            report["listing_ok"] = True
            item["discovered"] = len(documents)
        report["requests"].append(item)
    except Exception as error:
        report["requests"].append({"url": poller.LISTING, "error": str(error)})
    for url in KNOWN_XML:
        time.sleep(0.5)
        item = {"url": url, "valid_wordml": False}
        try:
            status, headers, raw = poller.fetch(url)
            item.update(http_status=status, headers=headers, bytes=len(raw))
            if status == 200:
                lines = poller.wordml_lines(raw)
                root = ET.fromstring(raw)
                item.update(valid_wordml=True, sha256=poller.digest(raw),
                            tables=len(root.findall(".//" + poller.W + "tbl")),
                            rows=len(root.findall(".//" + poller.W + "tr")), projection_lines=len(lines))
                report["xml_ok"] += 1
                if headers.get("etag"):
                    followup, _, followup_raw = poller.fetch(url, {"If-None-Match": headers["etag"]})
                    item["conditional_get_status"] = followup
                    if followup == 200:
                        item["conditional_same_bytes"] = poller.digest(raw) == poller.digest(followup_raw)
        except Exception as error:
            item["error"] = str(error)
        report["requests"].append(item)
    report["ok"] = report["listing_ok"] and report["xml_ok"] > 0
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="probe-report.json")
    args = parser.parse_args()
    report = probe()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("## Live EP network probe\n\n```json\n" + text + "\n```\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
