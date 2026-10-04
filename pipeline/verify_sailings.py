#!/usr/bin/env python3
"""Weekly second opinion on every upcoming sailing — flags, never edits.

For each upcoming departure date in cruises.json, fetch cruisetimetables.com's
"cruises from Seattle on <date>" page and compare each of our sailings' length (nights)
and end port with the listing for the same ship. Results:

  pipeline/verification.json   {checkedAt, results: {<cruise id>: {status, ...}}}
  verification.md              human summary (mismatches first) — read by the Monday report

status: match | mismatch | not_listed | error. build_dataset.py stamps `verifiedOn` on
matching sailings (shown in the app as "Itinerary checked <date>"). Mismatches need a
person to research and fix the ship template — this script never changes cruise data.

Polite by design: runs nightly but fetches only the MAX_DATES_PER_RUN stalest dates, one request
every DELAY seconds — every sailing gets re-checked about weekly at ~25 requests/day. On a 429 the
run stops early (the next night resumes) instead of retrying into the limit.
"""

from __future__ import annotations

import html
import os
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
URL = "https://www.cruisetimetables.com/fromseattlewashington-{d}.html"
UA = "Mozilla/5.0 (X11; Linux aarch64) SeattleCruiseFinder-verify/1.0"
DELAY = 25.0       # the site rate-limits (429) aggressively — stay far below it
MAX_DATES_PER_RUN = int(os.environ.get("VERIFY_MAX_DATES", "25"))  # nightly slice; full cycle ≈ 1 week
HORIZON_DAYS = 550  # roughly the published schedules (current + next season)
REFRESH_DAYS = 30   # keep an existing verifiedOn this long to avoid weekly dataset churn


def norm_ship(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("of the", "ofthe"))


def city(port: str) -> str:
    """'Vancouver, BC' / 'Vancouver, Canada' / 'Tokyo (Yokohama), Japan' → 'vancouver' / 'tokyo'."""
    return re.sub(r"[^a-z]", "", re.split(r"[,(]", port)[0].lower())


def fetch(d: date) -> str:
    req = urllib.request.Request(URL.format(d=d.strftime("%d%b%Y").lower()), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse(page: str) -> list[dict]:
    """Each 'cd-listing' block → {ship, nights, name, end, ports, prices, lineUrl}."""
    out = []
    page = re.sub(r"\s+", " ", page)
    for block in page.split("<div class='cd-listing'>")[1:]:
        ship = re.search(r"Ship <a[^>]*>([^<]+)</a>", block)
        title = re.search(r"<b>(\d+) Night ([^<]+)</b>", block)
        itin = block.split("Cruise Itinerary</b>:", 1)
        if not (ship and title and len(itin) == 2):
            continue
        ports = [html.unescape(p) for p in re.findall(r"<a class=red[^>]*>([^<]+)</a>", itin[1])]
        prices = dict(re.findall(r"(Interior|Inside|Oceanview|Ocean View|Outside|Balcony|Verandah|Suite|Deluxe)&nbsp;\$(\d[\d,]*)", block))
        line_url = re.search(r'More details at ?<br> ?<a target="_blank" href="([^"]+)"', block)
        out.append({"ship": html.unescape(ship.group(1)).strip(), "nights": int(title.group(1)),
                    "name": html.unescape(title.group(2)).strip(), "end": ports[-1] if ports else "",
                    "ports": ports, "prices": {k: int(v.replace(",", "")) for k, v in prices.items()},
                    "lineUrl": line_url.group(1) if line_url else None})
    return out


def main() -> int:
    cruises = json.loads((REPO / "cruises.json").read_text())
    prev_path = HERE / "verification.json"
    prev = json.loads(prev_path.read_text()).get("results", {}) if prev_path.exists() else {}
    today = date.today()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    by_date: dict[date, list[dict]] = {}
    for c in cruises:
        d = datetime.fromtimestamp(c["departureDate"], timezone.utc).date()
        if today <= d <= today + timedelta(days=HORIZON_DAYS):
            by_date.setdefault(d, []).append(c)

    def staleness(d: date) -> tuple:
        olds = [prev.get(c["id"]) or {} for c in by_date[d]]
        never = any(not o.get("checkedAt") or o.get("status") == "error" for o in olds)
        oldest = min((o.get("checkedAt") or "") for o in olds)
        return (not never, oldest, d)  # errors/never-checked first, then oldest check

    todo = set(sorted(by_date, key=staleness)[:MAX_DATES_PER_RUN])
    results: dict[str, dict] = {}
    fetched = 0
    throttled = False
    for d in sorted(by_date):
        if d not in todo or throttled:
            for c in by_date[d]:  # keep last known result until this date's turn comes round
                if c["id"] in prev and prev[c["id"]].get("date"):
                    results[c["id"]] = prev[c["id"]]
            continue
        if fetched:
            time.sleep(DELAY)
        fetched += 1
        try:
            listings = parse(fetch(d))
            if not listings:  # throttled/blank page — retry next run rather than report "not listed"
                raise RuntimeError("no listings parsed (throttled or page changed)")
        except urllib.error.HTTPError as e:
            if e.code == 429:  # stop for tonight; tomorrow's run starts with these dates
                throttled = True
                for c in by_date[d]:
                    if c["id"] in prev and prev[c["id"]].get("date"):
                        results[c["id"]] = prev[c["id"]]
                continue
            for c in by_date[d]:
                results[c["id"]] = {**prev.get(c["id"], {}), "ship": c["shipName"], "date": d.isoformat(),
                                    "ourNights": c["durationNights"], "ourEnd": c["portsOfCall"][-1]["name"],
                                    "source": URL.format(d=d.strftime("%d%b%Y").lower()),
                                    "status": "error", "error": str(e)[:200], "checkedAt": now}
            continue
        except Exception as e:  # network/site trouble: keep last known result, mark error
            for c in by_date[d]:
                results[c["id"]] = {**prev.get(c["id"], {}), "ship": c["shipName"], "date": d.isoformat(),
                                    "ourNights": c["durationNights"], "ourEnd": c["portsOfCall"][-1]["name"],
                                    "source": URL.format(d=d.strftime("%d%b%Y").lower()),
                                    "status": "error", "error": str(e)[:200], "checkedAt": now}
            continue
        for c in by_date[d]:
            ours_end = c["portsOfCall"][-1]["name"]
            same_ship = [l for l in listings if norm_ship(l["ship"]) == norm_ship(c["shipName"])]
            base = {"ship": c["shipName"], "date": d.isoformat(), "ourNights": c["durationNights"], "ourEnd": ours_end,
                    "checkedAt": now, "source": URL.format(d=d.strftime("%d%b%Y").lower())}
            if not same_ship:
                results[c["id"]] = {**base, "status": "not_listed"}
                continue
            exact = [l for l in same_ship if l["nights"] == c["durationNights"] and city(l["end"]) == city(ours_end)]
            best = exact[0] if exact else min(same_ship, key=lambda l: abs(l["nights"] - c["durationNights"]))
            status = "match" if exact else "mismatch"
            verified_on = None
            if status == "match":
                old = prev.get(c["id"], {})
                verified_on = old.get("verifiedOn") if old.get("status") == "match" and old.get("verifiedOn") and \
                    (today - date.fromisoformat(old["verifiedOn"])).days < REFRESH_DAYS else today.isoformat()
            results[c["id"]] = {**base, "status": status, "theirNights": best["nights"], "theirEnd": best["end"],
                                "theirName": best["name"], "theirPrices": best["prices"], "lineUrl": best["lineUrl"],
                                **({"theirPorts": best["ports"]} if status == "mismatch" else {}),
                                **({"verifiedOn": verified_on} if verified_on else {})}

    prev_path.write_text(json.dumps({"checkedAt": now, "results": results}, indent=1, sort_keys=True) + "\n")
    write_report(results, today)
    print(f"fetched {fetched} date pages this run{' (stopped early: rate-limited)' if throttled else ''}")
    return 0


def write_report(results: dict, today: date) -> None:
    counts = {s: sum(1 for r in results.values() if r["status"] == s) for s in ("match", "mismatch", "not_listed", "error")}
    lines = [f"# Weekly sailing verification — {today.isoformat()}", "",
             f"Checked {len(results)} upcoming sailings against cruisetimetables.com: "
             f"**{counts['match']} match**, **{counts['mismatch']} mismatch**, {counts['not_listed']} not listed, {counts['error']} errors.",
             "", "Mismatches need a person to research (ideally in the cruise line's agent portal) and fix the ship template "
             "in `pipeline/templates/`. Nothing here changes the app data automatically.", ""]
    order = sorted(results.values(), key=lambda r: (r["date"], r["ship"]))
    for status, title in (("mismatch", "## ⚠️ Mismatches"), ("not_listed", "## Not listed on the source site"),
                          ("error", "## Fetch errors")):
        rows = [r for r in order if r["status"] == status]
        if not rows:
            continue
        lines += [title, ""]
        for r in rows:
            if status == "mismatch":
                lines.append(f"- **{r['ship']} {r['date']}** — app: {r['ourNights']} nights → {r['ourEnd']}; "
                             f"source: {r['theirNights']} nights → {r['theirEnd']} ({r['theirName']}). "
                             f"[line page]({r['lineUrl']}) · [source]({r['source']})")
            else:
                lines.append(f"- {r['ship']} {r['date']} ({r['ourNights']} nights → {r['ourEnd']}) — [source]({r['source']})")
        lines.append("")
    (REPO / "verification.md").write_text("\n".join(lines))
    print(f"verified {len(results)}: {counts}")


if __name__ == "__main__":
    if "--report-only" in sys.argv:  # rebuild verification.md from the saved verification.json
        saved = json.loads((HERE / "verification.json").read_text())
        write_report(saved["results"], date.fromisoformat(saved["checkedAt"][:10]))
        sys.exit(0)
    sys.exit(main())
