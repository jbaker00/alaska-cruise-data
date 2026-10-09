#!/usr/bin/env python3
"""Live positions for the Seattle cruise fleet → public positions.json (app "Ships" tab).

Long-running service on pi5 (cruise-tracker.service). aisstream.io requires that clients
never connect directly — "proxy only the information your clients need from your own
server" — so this process is that proxy:

  • fleet stream: PositionReport + ShipStaticData for the fleet's MMSIs (pipeline/ships.json),
  • discovery stream (always on): ShipStaticData around the Pacific Northwest + Alaska.
    Broadcast IMO numbers (or names, for ships without one) fill in unknown MMSIs and
    replace stale ones — ships get reflagged and change MMSI, but never their IMO.
    It also remembers which MMSIs are large passenger ships (cruise ships, not ferries),
  • region stream: PositionReport in Puget Sound, published for those large passenger
    ships even when they aren't in ships.json ("fleet": false) — so a ship off the Port's
    schedule still shows up at the pier,
  • every PUBLISH_SECONDS: upload positions.json to gs://globalvibes-ship-positions
    (public read, Cache-Control 60 s). Last positions persist across restarts.

Secrets: AISSTREAM_API_KEY via Secret Manager (`~/bin/sm-get`), GCS write via the pi5
service-account JSON (roles/storage.objectUser on that bucket only).
Coverage caveat: aisstream is mostly terrestrial AIS — positions far offshore go stale;
every ship carries its own `updatedAt` so the app can say "last seen 9 h ago".
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import google.auth.transport.requests
import requests
import websockets
from google.oauth2 import service_account

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("STATE_DIR", Path.home() / ".local/state/cruise-watcher"))
SA_JSON = os.environ.get("TRACKER_SA_JSON", str(Path.home() / "home-console/secrets/firebase-viewer-sa.json"))
BUCKET = os.environ.get("TRACKER_BUCKET", "globalvibes-ship-positions")
WS_URL = "wss://stream.aisstream.io/v0/stream"
PUBLISH_SECONDS = 120
WORLD = [[[-90, -180], [90, 180]]]
DISCOVERY_BOXES = [[[44, -170], [72, -120]]]  # WA, BC, Alaska
# Puget Sound + Admiralty Inlet. Stops east of Victoria/Port Angeles and south of the
# San Juans, so BC Ferries' 167 m Spirit class never qualifies.
REGION_BOXES = [[[47.0, -123.2], [48.3, -122.2]]]
PASSENGER_TYPES = range(60, 70)   # AIS ship type "Passenger" (includes ferries — hence the length floor)
REGION_MIN_LENGTH_M = 150         # largest WA State Ferry is ~140 m; Seattle's cruise ships are 200 m+
REGION_TTL = timedelta(hours=12)  # drop a non-fleet ship this long after it leaves the Sound
NAV_STATUS = {0: "Under way", 1: "At anchor", 5: "Moored", 8: "Under way sailing", 15: None}


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def display_name(ais_name: str) -> str:
    return " ".join(w.capitalize() for w in ais_name.replace("@", " ").split())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api_key() -> str:
    key = subprocess.run([str(Path.home() / "bin/sm-get"), "AISSTREAM_API_KEY"],
                         check=True, capture_output=True, text=True).stdout.strip()
    if not key:
        raise SystemExit("AISSTREAM_API_KEY is empty")
    return key


class Fleet:
    def __init__(self) -> None:
        reg = {k: v for k, v in json.loads((HERE / "ships.json").read_text()).items() if not k.startswith("_")}
        self.learned_path = STATE / "learned_mmsi.json"
        # Learned MMSIs come from the ship's own broadcast, so they win over ships.json.
        self.learned: dict[str, str] = json.loads(self.learned_path.read_text()) if self.learned_path.exists() else {}
        self.by_name = {name: (self.learned.get(name) or info.get("mmsi")) for name, info in reg.items()}
        self.by_imo = {str(info["imo"]): name for name, info in reg.items() if info.get("imo")}
        self.no_imo = {norm(name): name for name, info in reg.items() if not info.get("imo")}
        self.region: dict[str, str] = {}  # MMSI → display name of large passenger ships not in the fleet
        self.pos_path = STATE / "positions_state.json"
        self.positions: dict[str, dict] = json.loads(self.pos_path.read_text()) if self.pos_path.exists() else {}
        self.changed = asyncio.Event()

    @property
    def mmsis(self) -> list[str]:
        return [m for m in self.by_name.values() if m]

    def name_for(self, mmsi: str) -> str | None:
        return next((n for n, m in self.by_name.items() if m == mmsi), None)

    def learn(self, name: str, mmsi: str) -> None:
        old = self.by_name.get(name)
        self.by_name[name] = mmsi
        self.learned[name] = mmsi
        self.learned_path.write_text(json.dumps(self.learned, indent=1))
        self.region.pop(mmsi, None)
        # Drop a record the region stream filed under the AIS spelling of the name.
        for key, rec in list(self.positions.items()):
            if rec.get("mmsi") == mmsi and key != name:
                del self.positions[key]
        log(f"learned MMSI {mmsi} for {name}" + (f" (was {old} — update ships.json)" if old else ""))
        self.changed.set()

    def static_data(self, mmsi: str, body: dict, meta: dict) -> None:
        """Discovery: match a ShipStaticData broadcast to the fleet, else note big passenger ships."""
        name = (body.get("Name") or meta.get("ShipName") or "").strip()
        imo = str(body.get("ImoNumber") or "")
        passenger = body.get("Type") in PASSENGER_TYPES
        # Name-only matches (no IMO on file) must at least be a passenger ship — fishing
        # boats share names too.
        target = self.by_imo.get(imo) or (self.no_imo.get(norm(name)) if passenger else None)
        if target:
            if self.by_name.get(target) != mmsi:
                self.learn(target, mmsi)
            return
        if mmsi in self.mmsis or not name:
            return
        dim = body.get("Dimension") or {}
        length = (dim.get("A") or 0) + (dim.get("B") or 0)
        if passenger and length >= REGION_MIN_LENGTH_M:
            shown = display_name(name)
            if mmsi not in self.region:
                log(f"region ship: {shown} ({mmsi}, {length} m, type {body.get('Type')})")
            self.region[mmsi] = shown
            if self.positions.get(shown, {}).get("mmsi") == mmsi:
                self.record(shown, mmsi, "ShipStaticData", body, meta)
        else:
            self.region.pop(mmsi, None)

    def record(self, name: str, mmsi: str, kind: str, body: dict, meta: dict) -> None:
        rec = self.positions.setdefault(name, {"shipName": name})
        rec["mmsi"] = mmsi
        if kind == "PositionReport":
            lat, lon = meta.get("latitude"), meta.get("longitude")
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                return
            rec.update({"lat": round(lat, 5), "lon": round(lon, 5),
                        "speedKnots": body.get("Sog"), "courseDeg": body.get("Cog"),
                        "headingDeg": None if body.get("TrueHeading") == 511 else body.get("TrueHeading"),
                        "navStatus": NAV_STATUS.get(body.get("NavigationalStatus")),
                        "updatedAt": now_iso()})
        elif kind == "ShipStaticData":
            eta = body.get("Eta") or {}
            rec["destination"] = (body.get("Destination") or "").strip() or None
            if eta.get("Month"):
                rec["etaText"] = f"{eta.get('Month'):02d}-{eta.get('Day', 0):02d} {eta.get('Hour', 0):02d}:{eta.get('Minute', 0):02d} UTC"


async def stream(fleet: Fleet, key: str, mode: str) -> None:
    """One aisstream connection ("fleet" | "discovery" | "region"); reconnects forever with backoff."""
    backoff = 5
    while True:
        try:
            if mode == "fleet":
                sub = {"BoundingBoxes": WORLD, "FiltersShipMMSI": fleet.mmsis,
                       "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
            elif mode == "discovery":
                sub = {"BoundingBoxes": DISCOVERY_BOXES, "FilterMessageTypes": ["ShipStaticData"]}
            else:
                sub = {"BoundingBoxes": REGION_BOXES, "FilterMessageTypes": ["PositionReport"]}
            async with websockets.connect(WS_URL, ping_interval=30, max_size=2**20) as ws:
                await ws.send(json.dumps({"APIKey": key, **sub}))
                log(f"{mode} stream connected" + (f" ({len(fleet.mmsis)} MMSIs)" if mode == "fleet" else ""))
                backoff = 5
                if mode == "fleet":
                    fleet.changed.clear()
                async for raw in ws:
                    msg = json.loads(raw)
                    meta = msg.get("MetaData", {})
                    mmsi = str(meta.get("MMSI", ""))
                    kind = msg.get("MessageType")
                    body = msg.get("Message", {}).get(kind, {})
                    if not mmsi:
                        continue
                    if mode == "discovery":
                        fleet.static_data(mmsi, body, meta)
                    elif mode == "region":
                        if mmsi in fleet.region:
                            rec = fleet.positions.get(fleet.region[mmsi], {})
                            if rec.get("mmsi") in (None, mmsi):
                                fleet.record(fleet.region[mmsi], mmsi, kind, body, meta)
                    else:
                        if fleet.changed.is_set():
                            break  # an MMSI was learned — resubscribe with the new filter
                        if name := fleet.name_for(mmsi):
                            fleet.record(name, mmsi, kind, body, meta)
        except Exception as e:
            log(f"{mode} stream error: {type(e).__name__}: {e} — retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)


def upload(payload: bytes) -> None:
    creds = service_account.Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
    creds.refresh(google.auth.transport.requests.Request())
    meta = {"name": "positions.json", "contentType": "application/json", "cacheControl": "public, max-age=60"}
    boundary = "cruiseTrackerBoundary"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n").encode() + payload + f"\r\n--{boundary}--".encode()
    r = requests.post(f"https://storage.googleapis.com/upload/storage/v1/b/{BUCKET}/o?uploadType=multipart",
                      data=body, timeout=30,
                      headers={"Authorization": f"Bearer {creds.token}",
                               "Content-Type": f"multipart/related; boundary={boundary}"})
    r.raise_for_status()


def prune(fleet: Fleet) -> None:
    """Forget non-fleet ships that have left the Sound (fleet ships are kept wherever they are)."""
    cutoff = datetime.now(timezone.utc) - REGION_TTL
    for name, rec in list(fleet.positions.items()):
        if name in fleet.by_name:
            continue
        updated = rec.get("updatedAt")
        if not updated or datetime.fromisoformat(updated) < cutoff:
            del fleet.positions[name]


async def publisher(fleet: Fleet) -> None:
    while True:
        await asyncio.sleep(PUBLISH_SECONDS)
        try:
            prune(fleet)
            fleet.pos_path.write_text(json.dumps(fleet.positions))
            ships = sorted(({**p, "fleet": p["shipName"] in fleet.by_name}
                            for p in fleet.positions.values() if "lat" in p), key=lambda p: p["shipName"])
            payload = json.dumps({"generatedAt": now_iso(),
                                  "source": "AIS via aisstream.io (terrestrial coverage; offshore positions may be stale)",
                                  "ships": ships}, separators=(",", ":")).encode()
            await asyncio.to_thread(upload, payload)
            log(f"published {len(ships)} ship positions ({sum(not s['fleet'] for s in ships)} non-fleet)")
        except Exception as e:
            log(f"publish failed: {type(e).__name__}: {e}")


async def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    fleet = Fleet()
    key = api_key()
    unknown = sorted(n for n, m in fleet.by_name.items() if not m)
    log(f"tracking {len(fleet.mmsis)} known MMSIs; still unknown: {unknown}")
    await asyncio.gather(*(stream(fleet, key, mode) for mode in ("fleet", "discovery", "region")), publisher(fleet))


if __name__ == "__main__":
    asyncio.run(main())
