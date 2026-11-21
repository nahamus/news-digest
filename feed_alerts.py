"""Open a GitHub issue when a feed keeps failing; close it once the feed recovers.

Run by the workflow after build.py. Reads data/health.json and uses the `gh` CLI
(GH_TOKEN must be set). GitHub emails you about new issues, so a dead source
doesn't go quiet unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LABEL = "feed-health"


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def title_for(feed: dict) -> str:
    return f"Feed failing: {feed['name']} ({feed['category']})"


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    threshold = int(cfg.get("alert_after_failures", 3))
    feeds = json.loads((ROOT / "data" / "health.json").read_text(encoding="utf-8"))["feeds"]

    gh("label", "create", LABEL, "--color", "B4441D", "--force",
       "--description", "A news feed is failing to load")
    open_issues = {
        i["title"]: i["number"]
        for i in json.loads(gh("issue", "list", "--label", LABEL, "--state", "open",
                               "--json", "number,title", "--limit", "100"))
    }

    for url, feed in feeds.items():
        title = title_for(feed)
        if feed["fails"] >= threshold and title not in open_issues:
            body = (
                f"**{feed['name']}** in **{feed['category']}** has failed "
                f"{feed['fails']} builds in a row.\n\n"
                f"- Feed URL: {url}\n"
                f"- Last error: `{feed['error']}`\n"
                f"- Last success: {feed['last_ok'] or 'never'}\n\n"
                "Check the URL in a browser. If the feed moved, update it in `config.yaml`; "
                "if it's gone, remove it. This issue closes itself when the feed loads again."
            )
            gh("issue", "create", "--title", title, "--label", LABEL, "--body", body)
            print(f"Opened issue: {title}")
        elif feed["fails"] == 0 and title in open_issues:
            gh("issue", "close", str(open_issues[title]),
               "--comment", "The feed is loading again. Closing automatically.")
            print(f"Closed issue: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
