# Seattle cruise data

Public dataset of cruise departures from Seattle, used by the **Alaska Cruise Finder – Seattle**
iOS app (Global Vibes Travel). Built automatically from the
[Port of Seattle cruise schedule](https://www.portseattle.org/page/port-seattle-cruise-schedule) PDFs.

| File | |
|---|---|
| [`version.json`](version.json) | `{version, schema, generatedAt, cruiseCount, sha256, seasons}` — the app polls this |
| [`cruises.json`](cruises.json) | Every Seattle departure × ship template, in the app's `Cruise` JSON shape |
| [`report.md`](report.md) | Build report: ships missing templates, overridden sailings, schedule anomalies |
| [`pipeline/`](pipeline/) | Parser, builder, daily watcher, ship templates — see [pipeline/README.md](pipeline/README.md) |

**Do not hand-edit the root JSON files** — they're regenerated and pushed by the watcher on the Pi.
Edit `pipeline/templates/` instead and push; the next run republishes.

Prices, ratings and itineraries are approximate and for browsing only; always confirm with the
cruise line or a travel advisor. Schedule data © Port of Seattle, "subject to change".
