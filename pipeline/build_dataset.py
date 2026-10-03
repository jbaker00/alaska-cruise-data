#!/usr/bin/env python3
"""Expand parsed Port of Seattle schedules + ship templates into the app's cruise dataset.

Inputs
  schedules/<year>.json   output of parse_schedule.py (latest published PDF per year)
  templates/<ship>.json   one per ship: every `Cruise` field except id/dates, plus
                          `aliases` (alternate PDF spellings) and `overrides`:
                            {"dates": ["2027-09-28"], "skip": true, "note": "..."}
                            {"dates": [...], "durationNights": 15, "portsOfCall": [...], "note": "..."}
                          Any template key may be overridden per departure.

Outputs (in --out)
  cruises.json   [Cruise] — decodes directly with the app's JSONDecoder (.secondsSince1970)
  version.json   {version, schema, generatedAt, cruiseCount, sha256, seasons}
  report.md      missing templates, skipped sailings, schedule anomalies for human review

`version` only changes when cruises.json content changes, and is a UTC timestamp
(YYYYMMDDHHMM) so it stays monotonic even if the output directory is lost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = 1  # bump when the Cruise JSON shape changes incompatibly; the app ignores newer schemas
ID_NAMESPACE = uuid.UUID("6f1c2b0e-5a7d-4c1e-9a43-0c1f5e2d7b11")

CABIN_TYPES = {"Interior", "Ocean View", "Balcony", "Suite"}
SHIP_SIZES = {"Small/Boutique", "Mid-Size", "Large/Mega-Ship"}
AMENITIES = {"Specialty Dining", "Entertainment", "Spa & Wellness", "Kids Club", "Adults Only",
             "Casino", "Outdoor Activities", "All Inclusive"}
OPTIONAL_FIELDS = {"imageCredit"}  # optional in the app's Cruise type — safe to add without a schema bump
TEMPLATE_FIELDS = {
    "shipName", "cruiseLine", "durationNights", "portsOfCall", "cabinCategories", "amenityCategories",
    "amenityHighlights", "rating", "reviewCount", "imageURLs", "shipSize", "inclusions", "exclusions",
    "diningOptions", "entertainmentOptions", "hasKidsClub", "hasAdultsOnly", "hasSpa", "hasCasino",
    "isAllInclusive", "reviewSnippet",
}


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^(ms|mv|m/s)\s+", "", name.strip().lower()))


def load_templates(directory: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    errors = []
    for path in sorted(directory.glob("*.json")):
        t = json.loads(path.read_text())
        missing = TEMPLATE_FIELDS - t.keys()
        if missing:
            errors.append(f"{path.name}: missing {sorted(missing)}")
        if t.get("shipSize") not in SHIP_SIZES:
            errors.append(f"{path.name}: bad shipSize {t.get('shipSize')!r}")
        for c in t.get("cabinCategories", []):
            if c.get("type") not in CABIN_TYPES:
                errors.append(f"{path.name}: bad cabin type {c.get('type')!r}")
        for a in t.get("amenityCategories", []):
            if a not in AMENITIES:
                errors.append(f"{path.name}: bad amenity {a!r}")
        for key in [t["shipName"], *t.get("aliases", [])]:
            index[norm(key)] = t
    if errors:
        raise SystemExit("Template errors:\n  " + "\n  ".join(errors))
    return index


def load_schedules(directory: Path, min_year: int) -> list[dict]:
    latest: dict[int, dict] = {}
    for path in sorted(directory.glob("*.json")):
        s = json.loads(path.read_text())
        if s["year"] < min_year:
            continue
        prev = latest.get(s["year"])
        if prev is None or (s.get("published") or "") > (prev.get("published") or ""):
            latest[s["year"]] = s
    return [latest[y] for y in sorted(latest)]


def epoch(d: date) -> int:
    # Noon UTC lands on the same calendar day for every timezone from UTC-11 to UTC+11.
    return int(datetime.combine(d, time(12), tzinfo=timezone.utc).timestamp())


def make_cruise(t: dict, dep: date, preliminary: bool) -> dict:
    cid = uuid.uuid5(ID_NAMESPACE, f"{t['shipName']}|{dep.isoformat()}")
    sub = lambda kind, i: str(uuid.uuid5(cid, f"{kind}{i}")).upper()
    cruise = {k: t[k] for k in TEMPLATE_FIELDS}
    cruise.update({k: t[k] for k in OPTIONAL_FIELDS if t.get(k)})
    # Dates from a "PRELIMINARY" Port schedule — the app badges these until the final schedule replaces them.
    cruise["isPreliminary"] = preliminary
    cruise["id"] = str(cid).upper()
    cruise["departureDate"] = epoch(dep)
    cruise["returnDate"] = epoch(dep + timedelta(days=t["durationNights"]))
    cruise["portsOfCall"] = [{**p, "id": sub("port", i)} for i, p in enumerate(t["portsOfCall"])]
    cruise["cabinCategories"] = [{**c, "id": sub("cabin", i)} for i, c in enumerate(t["cabinCategories"])]
    return cruise


def build(schedules: list[dict], templates: dict[str, dict]):
    cruises: list[dict] = []
    missing: dict[str, list[str]] = defaultdict(list)
    skipped: list[str] = []
    notes: list[str] = []
    by_ship: dict[str, list[tuple[date, int]]] = defaultdict(list)

    for s in schedules:
        for call in s["calls"]:
            if call["inTransit"]:
                continue
            t = templates.get(norm(call["vessel"]))
            if t is None:
                missing[f"{call['vessel']} ({call['cruiseLine']})"].append(call["date"])
                continue
            override = next((o for o in t.get("overrides", []) if call["date"] in o["dates"]), None)
            if override:
                line = f"{t['shipName']} {call['date']}: {override.get('note', '')}"
                if override.get("skip"):
                    skipped.append(line)
                    continue
                notes.append(line)
                t = {**t, **{k: v for k, v in override.items() if k in TEMPLATE_FIELDS | OPTIONAL_FIELDS}}
            dep = date.fromisoformat(call["date"])
            cruises.append(make_cruise(t, dep, s["preliminary"]))
            by_ship[t["shipName"]].append((dep, t["durationNights"]))

    # Anomalies: a ship can't depart again before its previous voyage returns.
    anomalies = []
    for ship, deps in by_ship.items():
        deps.sort()
        for (d1, n1), (d2, _) in zip(deps, deps[1:]):
            gap = (d2 - d1).days
            if gap < n1:
                anomalies.append(f"{ship}: {d1} is {n1} nights but next departure is {d2} ({gap} days later)")

    cruises.sort(key=lambda c: (c["departureDate"], c["shipName"]))
    return cruises, missing, skipped, notes, anomalies


def write_outputs(out: Path, schedules, cruises, missing, skipped, notes, anomalies) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cruises, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    digest = hashlib.sha256(payload).hexdigest()

    version_path = out / "version.json"
    prev = json.loads(version_path.read_text()) if version_path.exists() else {}
    now = datetime.now(timezone.utc)
    changed = prev.get("sha256") != digest
    version = max(int(now.strftime("%Y%m%d%H%M")), prev.get("version", 0) + 1) if changed else prev["version"]

    meta = {
        "version": version,
        "schema": SCHEMA,
        "generatedAt": now.isoformat(timespec="seconds") if changed else prev.get("generatedAt"),
        "cruiseCount": len(cruises),
        "sha256": digest,
        "seasons": [{k: s[k] for k in ("year", "published", "preliminary", "source")} for s in schedules],
    }
    (out / "cruises.json").write_bytes(payload)
    version_path.write_text(json.dumps(meta, indent=2) + "\n")

    lines = [f"# Cruise dataset v{version}", "",
             f"{len(cruises)} cruises from " + ", ".join(
                 f"{s['year']}{' (preliminary)' if s['preliminary'] else ''} published {s['published']}"
                 for s in schedules), ""]
    if missing:
        lines += ["## ⚠️ Ships with no template (sailings omitted)", ""]
        lines += [f"- **{k}** — {len(v)} departures: {', '.join(v)}" for k, v in sorted(missing.items())] + [""]
    if anomalies:
        lines += ["## ⚠️ Schedule anomalies (check itinerary/duration)", ""] + [f"- {a}" for a in anomalies] + [""]
    if skipped:
        lines += ["## Skipped by override", ""] + [f"- {s}" for s in skipped] + [""]
    if notes:
        lines += ["## Overridden / flagged departures", ""] + [f"- {n}" for n in notes] + [""]
    (out / "report.md").write_text("\n".join(lines))
    return {**meta, "changed": changed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schedules", type=Path, default=HERE / "schedules")
    ap.add_argument("--templates", type=Path, default=HERE / "templates")
    ap.add_argument("--out", type=Path, default=HERE / "public", help="output dir (default: pipeline/public, gitignored preview; the watcher writes to the repo root)")
    ap.add_argument("--min-year", type=int, default=date.today().year,
                    help="drop seasons before this year (default: current year)")
    ap.add_argument("--strict", action="store_true", help="exit 2 if any ship lacks a template")
    args = ap.parse_args(argv)

    schedules = load_schedules(args.schedules, args.min_year)
    if not schedules:
        print("No schedules found", file=sys.stderr)
        return 1
    templates = load_templates(args.templates)
    cruises, missing, skipped, notes, anomalies = build(schedules, templates)
    meta = write_outputs(args.out, schedules, cruises, missing, skipped, notes, anomalies)

    print(f"{'NEW' if meta['changed'] else 'unchanged'} v{meta['version']}: {meta['cruiseCount']} cruises, "
          f"{len(missing)} ships missing templates, {len(anomalies)} anomalies → {args.out}")
    return 2 if (args.strict and missing) else 0


if __name__ == "__main__":
    sys.exit(main())
