# Cruise data pipeline

Turns the Port of Seattle's published cruise schedule PDFs into the JSON dataset the app
downloads at launch (`RemoteDataService` in the app repo), so new seasons ship without an App
Store release. Every Seattle departure is included — Alaska, Hawaii, one-way repositioning,
transpacific — as long as the ship has a template.

```
Port of Seattle PDF ──parse_schedule.py──▶ schedules/<year>.json ─┐
                                                                  ├─build_dataset.py──▶ /cruises.json + /version.json ──git push──▶ app
                      templates/<ship>.json (itinerary, cabins, …)┘
```

| File | Purpose |
|---|---|
| `parse_schedule.py` | PDF → `{year, published, preliminary, calls:[{date, vessel, pier, cruiseLine, inTransit}]}`. Parses by word coordinates (pdfplumber) because the PDF's merged same-day cells scramble plain text extraction. `*` vessels are in-transit calls, not departures. |
| `build_dataset.py` | Expands every turnaround call × ship template into `Cruise` JSON. Deterministic UUIDs (ship + date), dates at noon UTC as `secondsSince1970`. Writes `report.md` listing ships with no template, overridden/skipped sailings, and duration overlaps. |
| `watcher.py` | Daily job on the Pi: `git pull`, scrape the Port's cruise pages for schedule PDFs, parse new ones into `schedules/`, rebuild the root JSON, commit + push if anything changed, notify via ntfy. |
| `verify_sailings.py` | Nightly 2:30 AM slice (`cruise-verify.timer`) — ~25 dates/night, 25 s apart, so each sailing is re-checked about weekly without tripping the site's rate limit: compares every upcoming sailing's length + end port with cruisetimetables.com and writes `verification.md` (mismatches first) + `verification.json`. **Flags only — never edits data.** Matches get `verifiedOn`, shown in the app as "Itinerary checked …". The Monday marketing report summarizes it. |
| `templates/` | One JSON per ship — all `Cruise` fields except `id`/dates, plus `aliases` and per-date `overrides`. **Source of truth for remote data.** |
| `schedules/` | Parsed schedules (the watcher commits new ones here). |
| `pi5/` | systemd user timer + `install.sh`. |

## When a new schedule names a ship with no template

The watcher still publishes (that ship's sailings are omitted) and the notification lists it.
To add it:

1. Copy a similar ship in `templates/` to `templates/<snake_case_ship_name>.json` and edit.
   Add `aliases` if the PDF spells the name differently (matching is already case-insensitive).
2. In the app repo, add `ship_<snake_case_ship_name>.imageset` to `Assets.xcassets` (name must
   match `Cruise.localImageName`). Until an app update ships, the template's `imageURLs` is used.
3. `python3 pipeline/build_dataset.py` locally and read `pipeline/public/report.md`.
4. Commit + push the template only; the Pi pulls on its next run and republishes the root JSON
   (or `ssh pi5 systemctl --user start cruise-watcher` to do it now).

## Where the data comes from (and how much to trust it)

- **Dates, ships, piers** — Port of Seattle PDF (official; "PRELIMINARY" schedules can change).
- **Itineraries** — ship templates + overrides, researched by hand from secondhand sites
  (cruisetimetables.com etc.). Season-edge sailings are flagged `VERIFY` in `report.md` until checked.
- **Fares** — estimates captured at research time; shown in the app as estimates. Live fares need a
  cruise-line agent portal or a licensed feed (Traveltek / Widgety / Cruise Factory).
- **Weekly second opinion** — `verify_sailings.py`; fix mismatches in the templates after confirming
  with the cruise line.

## Overrides

Departures that don't fit the ship's usual itinerary (repositioning, Hawaii, one-way), or Port
calls that aren't bookable departures (e.g. a ship arriving home with no onward sailing):

```json
"overrides": [
  {"dates": ["2027-10-07"], "skip": true, "note": "Arrival only — no bookable departure"},
  {"dates": ["2027-09-28"], "durationNights": 15, "portsOfCall": [...], "note": "One-way to Yokohama"}
]
```

Any template field can be overridden. Check `report.md` → *Schedule anomalies* after each new
schedule: a departure whose duration overlaps the ship's next departure usually means the
itinerary changed and needs an override.

## Local use

```bash
pip install -r requirements.txt
python3 parse_schedule.py "2027 Preliminary Cruise Schedule 9.18.26.pdf" -o schedules/2027.json
python3 build_dataset.py --min-year 2026        # → pipeline/public/ (gitignored preview)
STATE_DIR=/tmp/cw DRY_RUN=1 python3 watcher.py  # full run, no commit/push/notify
```

## Pi setup (already done on pi5)

```bash
# Deploy key with write access to this repo only, via an SSH host alias:
#   ~/.ssh/config:  Host github-cruisedata / HostName github.com / IdentityFile ~/.ssh/id_cruisedata
git clone git@github-cruisedata:jbaker00/alaska-cruise-data.git ~/alaska-cruise-data
~/alaska-cruise-data/pipeline/pi5/install.sh       # venv + systemd user timer (daily ~07:15)
systemctl --user start cruise-watcher               # run now
journalctl --user -u cruise-watcher -n 50           # logs
```

Optional phone alerts: set `NTFY_TOPIC=<hard-to-guess-topic>` in `~/.config/cruise-watcher.env`
and subscribe to that topic in the ntfy app.

## Compatibility

`version.json.schema` must be ≤ `RemoteDataService.supportedSchema` or the app ignores the
update. If `Cruise` gains a non-optional field, bump `SCHEMA` in `build_dataset.py` *and*
`supportedSchema` in the app — old installs then keep their cached data instead of failing to decode.
