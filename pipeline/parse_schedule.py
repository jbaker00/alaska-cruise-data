#!/usr/bin/env python3
"""Parse a Port of Seattle cruise schedule PDF into structured JSON.

The PDF is a two-column table per page:  Day | Date | Vessel | Pier | Cruise Line.
A trailing "*" on the vessel marks an in-transit port of call (not a Seattle
turnaround), which we keep but flag so the dataset builder can skip it.

Text extraction (pdftotext) scrambles the vertically-merged cells the Port uses
for same-day rows, but pdfplumber's word coordinates keep every row on a shared
`top`, so we parse by coordinates: split each page into left/right columns, group
words into rows by `top`, then anchor on the pier number (66 / 91) to separate
the vessel name from the cruise line.

Usage:
    parse_schedule.py SCHEDULE.pdf [-o out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import pdfplumber

DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
PIER_RE = re.compile(r"^\d{2}$")
DAY_NAMES = {"Mon", "Tue", "Tues", "Wed", "Thu", "Thur", "Thurs", "Fri", "Sat", "Sun"}
YEAR_RE = re.compile(r"\b(20\d{2})\b\s+(?:PRELIMINARY\s+)?CRUISE SCHEDULE", re.I)
PUBLISHED_RE = re.compile(r"Published\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", re.I)
ROW_TOLERANCE = 2.0  # points; words within this vertical distance share a row


class ScheduleParseError(Exception):
    pass


def _group_rows(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= ROW_TOLERANCE:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _parse_row(tokens: list[str], year: int) -> dict | None:
    # Expected: [Day] M/D Vessel... Pier CruiseLine...
    i = 0
    if i < len(tokens) and tokens[i] in DAY_NAMES:
        i += 1
    if i >= len(tokens):
        return None
    m = DATE_RE.match(tokens[i])
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    rest = tokens[i + 1:]
    pier_idx = next((j for j, t in enumerate(rest) if PIER_RE.match(t)), None)
    if pier_idx is None or pier_idx == 0:
        return None
    vessel = " ".join(rest[:pier_idx]).strip()
    cruise_line = " ".join(rest[pier_idx + 1:]).strip()
    in_transit = vessel.endswith("*")
    vessel = vessel.rstrip("*").strip()
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    return {
        "date": d.isoformat(),
        "vessel": vessel,
        "pier": int(rest[pier_idx]),
        "cruiseLine": cruise_line,
        "inTransit": in_transit,
    }


def parse_pdf(path: str | Path) -> dict:
    calls: list[dict] = []
    year: int | None = None
    published: str | None = None
    preliminary = False

    with pdfplumber.open(str(path)) as pdf:
        header_text = pdf.pages[0].extract_text() or ""
        if (m := YEAR_RE.search(header_text)):
            year = int(m.group(1))
        if (m := PUBLISHED_RE.search(header_text)):
            mo, dy, yr = map(int, m.groups())
            published = date(yr + 2000 if yr < 100 else yr, mo, dy).isoformat()
        preliminary = "PRELIMINARY" in header_text.upper()
        if year is None:
            raise ScheduleParseError(f"Could not find schedule year in header of {path}")

        for page in pdf.pages:
            mid = page.width / 2
            words = page.extract_words(x_tolerance=1.5)
            for column in ([w for w in words if w["x0"] < mid], [w for w in words if w["x0"] >= mid]):
                for row in _group_rows(column):
                    call = _parse_row([w["text"] for w in row], year)
                    if call:
                        calls.append(call)

    if not calls:
        raise ScheduleParseError(f"No schedule rows parsed from {path} — PDF layout may have changed")

    # De-dupe (identical rows are never legitimate) and sort.
    seen, unique = set(), []
    for c in calls:
        key = (c["date"], c["vessel"].lower(), c["pier"])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    unique.sort(key=lambda c: (c["date"], c["pier"], c["vessel"]))

    return {
        "year": year,
        "published": published,
        "preliminary": preliminary,
        "source": Path(path).name,
        "calls": unique,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    args = ap.parse_args(argv)

    result = parse_pdf(args.pdf)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
        turnarounds = sum(1 for c in result["calls"] if not c["inTransit"])
        print(f"{result['year']}: {len(result['calls'])} calls ({turnarounds} turnarounds) → {args.output}",
              file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
