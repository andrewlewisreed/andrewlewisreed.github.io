#!/usr/bin/env python3
"""Weekly baseline for relays/24h and two-way link counts.

Does not compute scores. Reads data.json and snapshots relays_24h + two_way
into data/week_baseline.json on the first run on or after Monday 8:05 AM
America/Los_Angeles. If the file is missing, it is seeded with current values
(badges show ±0).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data.json")
OUT = os.path.join(ROOT, "data", "week_baseline.json")


def week_of(now: datetime) -> datetime:
    now = now.astimezone(PT)
    monday = (now - timedelta(days=now.weekday())).replace(hour=8, minute=5, second=0, microsecond=0)
    if now < monday:
        monday -= timedelta(days=7)
    return monday


def snapshot(payload: dict) -> dict:
    out = {}
    for rep in payload.get("repeaters") or []:
        slug = rep.get("slug")
        if not slug:
            continue
        item = {}
        if isinstance(rep.get("relays_24h"), (int, float)):
            item["relays_24h"] = int(rep["relays_24h"])
        if isinstance(rep.get("two_way"), (int, float)):
            item["two_way"] = int(rep["two_way"])
        if item:
            out[slug] = item
    return out


def parse_week(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PT)
    return dt.astimezone(PT)


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        with open(DATA, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"No data.json to baseline: {exc}", file=sys.stderr)
        return 0
    scores = snapshot(data)
    if not scores:
        print("No relay/link totals in data.json; leaving baseline unchanged.", file=sys.stderr)
        return 0
    this_week = week_of(now)
    try:
        with open(OUT, encoding="utf-8") as fh:
            old = json.load(fh)
    except Exception:  # noqa: BLE001
        old = {}
    old_week = parse_week(old.get("week_of"))
    old_scores = old.get("repeaters") if isinstance(old.get("repeaters"), dict) else {}
    need = (not old_scores) or (old_week is None) or (old_week < this_week)
    if not need:
        print(f"Baseline current for week_of {old_week.isoformat()}")
        return 0
    seeded = not old_scores
    captured = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "week_of": this_week.isoformat(),
        "captured_at": captured,
        "timezone": "America/Los_Angeles",
        "note": (
            "Seeded with current relays/24h and two-way counts; first weekly refresh is the first run on or after Monday 8:05 AM PT."
            if seeded
            else "Weekly baseline captured at the first update on or after Monday 8:05 AM PT."
        ),
        "repeaters": scores if seeded else {**old_scores, **scores},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")
    print(("Seeded" if seeded else "Refreshed") + f" baseline week_of {this_week.isoformat()} {scores}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
