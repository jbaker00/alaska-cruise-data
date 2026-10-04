#!/usr/bin/env python3
"""Watch the Port of Seattle for new cruise schedule PDFs and republish the app dataset.

Runs from a checkout of this repo (the same repo the app downloads from). Each run:
  1. `git pull` (picks up template edits pushed from elsewhere).
  2. Scrape the Port's cruise pages for "... Cruise Schedule ....pdf" links.
  3. Download + parse any link not seen before into pipeline/schedules/<year>.json
     (newest published PDF per year wins).
  4. Rebuild cruises.json / version.json / report.md at the repo root — always, so template
     edits publish even without a new PDF.
  5. If anything changed, commit and push.
  6. Notify (ntfy) on new PDFs, publishes, ships missing templates, and failures.

Configuration (environment, typically ~/.config/cruise-watcher.env):
  NTFY_TOPIC   optional ntfy.sh topic (or full URL) for phone notifications
  STATE_DIR    default ~/.local/state/cruise-watcher (downloaded PDFs + seen-links list)
  DRY_RUN=1    no git pull/commit/push and no notifications
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import build_dataset  # noqa: E402
import parse_schedule  # noqa: E402

PAGES = [
    "https://www.portseattle.org/page/port-seattle-cruise-schedule",
    "https://www.portseattle.org/maritime/cruise",
]
SITE = "https://www.portseattle.org"
UA = "Mozilla/5.0 (X11; Linux aarch64) SeattleCruiseFinder-watcher/1.0"
LINK_RE = re.compile(r'href="([^"]*cruise[^"]*schedule[^"]*\.pdf)\s*"', re.I)

STATE_DIR = Path(os.environ.get("STATE_DIR", Path.home() / ".local/state/cruise-watcher"))
SCHED_DIR = HERE / "schedules"
NTFY = os.environ.get("NTFY_TOPIC", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def notify(title: str, body: str, priority: str = "default") -> None:
    log(f"NOTIFY [{title}] {body}")
    if DRY_RUN or not NTFY:
        return
    url = NTFY if NTFY.startswith("http") else f"https://ntfy.sh/{NTFY}"
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"Title": title, "Priority": priority, "Tags": "ship"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # notification failure must not fail the run
        log(f"ntfy failed: {e}")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def find_pdf_links() -> set[str]:
    links: set[str] = set()
    for page in PAGES:
        try:
            text = fetch(page).decode("utf-8", "replace")
        except Exception as e:
            log(f"could not load {page}: {e}")
            continue
        for href in LINK_RE.findall(text):
            href = urllib.parse.urljoin(SITE, html.unescape(href).strip())
            # Normalise encoding so the same file isn't seen twice under two spellings.
            links.add(urllib.parse.quote(urllib.parse.unquote(href), safe=":/"))
    return links


def process_new_pdfs(seen: dict) -> None:
    links = find_pdf_links()
    if not links:
        raise RuntimeError("No schedule PDF links found — the Port's page layout may have changed")
    pdf_dir = STATE_DIR / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    SCHED_DIR.mkdir(exist_ok=True)

    for url in sorted(links - seen.keys()):
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        log(f"new PDF: {name}")
        data = fetch(url)
        (pdf_dir / name).write_bytes(data)
        entry = {"firstSeen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "sha256": hashlib.sha256(data).hexdigest()}
        try:
            parsed = parse_schedule.parse_pdf(pdf_dir / name)
        except parse_schedule.ScheduleParseError as e:
            seen[url] = {**entry, "error": str(e)}
            notify("Cruise schedule PDF failed to parse", f"{name}: {e}", "high")
            continue

        target = SCHED_DIR / f"{parsed['year']}.json"
        current = json.loads(target.read_text()) if target.exists() else None
        if current is None or (parsed["published"] or "") > (current.get("published") or ""):
            target.write_text(json.dumps(parsed, indent=2) + "\n")
            turnarounds = sum(1 for c in parsed["calls"] if not c["inTransit"])
            notify("New Port of Seattle cruise schedule",
                   f"{parsed['year']}{' preliminary' if parsed['preliminary'] else ''} schedule "
                   f"published {parsed['published']}: {turnarounds} departures")
        seen[url] = {**entry, "year": parsed["year"], "published": parsed["published"]}


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen_path = STATE_DIR / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    try:
        if not DRY_RUN and not os.environ.get("CRUISE_WATCHER_PULLED"):
            before = git("rev-parse", "HEAD").strip()
            git("pull", "--ff-only")
            if git("rev-parse", "HEAD").strip() != before:
                # This process already imported the old pipeline code — restart so the
                # freshly pulled builder/parser is what runs.
                log("repo updated — restarting with new code")
                os.execve(sys.executable, [sys.executable, *sys.argv],
                          {**os.environ, "CRUISE_WATCHER_PULLED": "1"})

        process_new_pdfs(seen)
        seen_path.write_text(json.dumps(seen, indent=2) + "\n")

        schedules = build_dataset.load_schedules(SCHED_DIR, datetime.now().year)
        templates = build_dataset.load_templates(HERE / "templates")
        cruises, missing, skipped, notes, anomalies = build_dataset.build(schedules, templates)
        meta = build_dataset.write_outputs(REPO, schedules, cruises, missing, skipped, notes, anomalies)
        log(f"build: v{meta['version']} {meta['cruiseCount']} cruises, changed={meta['changed']}")

        paths = ["cruises.json", "version.json", "report.md", "pipeline/schedules"]
        status = git("status", "--porcelain", "--", *paths)
        if not status.strip():
            log("nothing to publish")
            return 0
        if DRY_RUN:
            log("DRY_RUN — not committing:\n" + status)
            return 0
        git("add", "-A", "--", *paths)
        git("commit", "-m", f"Dataset v{meta['version']} ({meta['cruiseCount']} cruises)", "--", *paths)
        git("push")
        log(f"published v{meta['version']}")
        if meta["changed"]:
            body = f"v{meta['version']}: {meta['cruiseCount']} sailings."
            if missing:
                body += " Missing templates (sailings omitted): " + ", ".join(sorted(missing))
            notify("Cruise Finder data published", body)
        return 0
    except Exception as e:
        seen_path.write_text(json.dumps(seen, indent=2) + "\n")
        log(traceback.format_exc())
        notify("Cruise watcher failed", f"{type(e).__name__}: {e}", "high")
        return 1


if __name__ == "__main__":
    sys.exit(main())
