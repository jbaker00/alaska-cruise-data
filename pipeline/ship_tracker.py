#!/usr/bin/env python3
"""Live positions for the Seattle cruise fleet → public positions.json (app "Ships" tab).

Long-running service on pi5 (cruise-tracker.service). aisstream.io requires that clients
never connect directly — "proxy only the information your clients need from your own
server" — so this process is that proxy:

  • main stream: PositionReport + ShipStaticData for the fleet's MMSIs (pipeline/ships.json),
  • discovery stream (only while some MMSIs are unknown): ShipStaticData around the Pacific
    Northwest + Alaska, matching broadcast names to fill in missing MMSIs,
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
import time
from datetime import datetime, timezone
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
NAV_STATUS = {0: "Under way", 1: "At anchor", 5: "Moored", 8: "Under way sailing", 15: None}


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def api_key() -> str:
    key = subprocess.run([str(Path.home() / "bin/sm-get"), "AISSTREAM_API_KEY"],
                         check=True, capture_output=True, text=True).stdout.strip()
    if not key:
        raise SystemExit("AISSTREAM_API_KEY is empty")
    return key


class Fleet:
    def __init__(self) -> None:
        reg = {k: v for k, v in json.loads((HERE / "ships.json").read_text()).items() if not k.startswith("_")}
        learned_path = STATE / "learned_mmsi.json"
        learned = json.loads(learned_path.read_text()) if learned_path.exists() else {}
        self.learned_path = learned_path
        self.by_name = {name: (info.get("mmsi") or learned.get(name)) for name, info in reg.items()}
        self.pos_path = STATE / "positions_state.json"
        self.positions: dict[str, dict] = json.loads(self.pos_path.read_text()) if self.pos_path.exists() else {}
        self.changed = asyncio.Event()

    @property
    def mmsis(self) -> list[str]:
        return [m for m in self.by_name.values() if m]

    @property
    def unknown(self) -> dict[str, str]:
        return {norm(n): n for n, m in self.by_name.items() if not m}

    def name_for(self, mmsi: str) -> str | None:
        return next((n for n, m in self.by_name.items() if m == mmsi), None)

    def learn(self, name: str, mmsi: str) -> None:
        self.by_name[name] = mmsi
        learned = {n: m for n, m in self.by_name.items() if m}
        self.learned_path.write_text(json.dumps(learned, indent=1))
        log(f"learned MMSI {mmsi} for {name}")
        self.changed.set()


async def stream(fleet: Fleet, key: str, discovery: bool) -> None:
    """One aisstream connection; reconnects forever with backoff."""
    backoff = 5
    while True:
        try:
            if discovery and not fleet.unknown:
                return
            sub = {"APIKey": key, "BoundingBoxes": DISCOVERY_BOXES if discovery else WORLD,
                   "FilterMessageTypes": ["ShipStaticData"] if discovery else ["PositionReport", "ShipStaticData"]}
            if not discovery:
                sub["FiltersShipMMSI"] = fleet.mmsis
            async with websockets.connect(WS_URL, ping_interval=30, max_size=2**20) as ws:
                await ws.send(json.dumps(sub))
                log(f"{'discovery' if discovery else 'fleet'} stream connected ({len(fleet.mmsis)} MMSIs)")
                backoff = 5
                fleet.changed.clear()
                async for raw in ws:
                    msg = json.loads(raw)
                    meta = msg.get("MetaData", {})
                    mmsi = str(meta.get("MMSI", ""))
                    kind = msg.get("MessageType")
                    if discovery:
                        target = fleet.unknown.get(norm(meta.get("ShipName", "")))
                        if target and mmsi:
                            fleet.learn(target, mmsi)
                            if not fleet.unknown:
                                return
                        continue
                    if not discovery and fleet.changed.is_set():
                        break  # an MMSI was learned — resubscribe with the new filter
                    name = fleet.name_for(mmsi)
                    if not name:
                        continue
                    rec = fleet.positions.setdefault(name, {"shipName": name, "mmsi": mmsi})
                    body = msg.get("Message", {}).get(kind, {})
                    if kind == "PositionReport":
                        lat, lon = meta.get("latitude"), meta.get("longitude")
                        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
                            continue
                        rec.update({"lat": round(lat, 5), "lon": round(lon, 5),
                                    "speedKnots": body.get("Sog"), "courseDeg": body.get("Cog"),
                                    "headingDeg": None if body.get("TrueHeading") == 511 else body.get("TrueHeading"),
                                    "navStatus": NAV_STATUS.get(body.get("NavigationalStatus")),
                                    "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")})
                    elif kind == "ShipStaticData":
                        eta = body.get("Eta") or {}
                        rec["destination"] = (body.get("Destination") or "").strip() or None
                        if eta.get("Month"):
                            rec["etaText"] = f"{eta.get('Month'):02d}-{eta.get('Day', 0):02d} {eta.get('Hour', 0):02d}:{eta.get('Minute', 0):02d} UTC"
        except Exception as e:
            log(f"{'discovery' if discovery else 'fleet'} stream error: {type(e).__name__}: {e} — retry in {backoff}s")
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


async def publisher(fleet: Fleet) -> None:
    while True:
        await asyncio.sleep(PUBLISH_SECONDS)
        try:
            fleet.pos_path.write_text(json.dumps(fleet.positions))
            ships = sorted((p for p in fleet.positions.values() if "lat" in p), key=lambda p: p["shipName"])
            payload = json.dumps({"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                  "source": "AIS via aisstream.io (terrestrial coverage; offshore positions may be stale)",
                                  "ships": ships}, separators=(",", ":")).encode()
            await asyncio.to_thread(upload, payload)
            log(f"published {len(ships)} ship positions")
        except Exception as e:
            log(f"publish failed: {type(e).__name__}: {e}")


async def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    fleet = Fleet()
    key = api_key()
    log(f"tracking {len(fleet.mmsis)} known MMSIs; discovering {sorted(fleet.unknown.values())}")
    await asyncio.gather(stream(fleet, key, discovery=False), stream(fleet, key, discovery=True), publisher(fleet))


if __name__ == "__main__":
    asyncio.run(main())
