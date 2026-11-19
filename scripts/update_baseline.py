#!/usr/bin/env python3
"""Maintain data/reed_baseline.json for weekly REED score-change badges.

Does not compute or change REED scores. Reads totals from data.json (written
by update_data.py) and snapshots them into a committed baseline file.

The baseline refreshes on the first run on or after Monday 8:05 AM
America/Los_Angeles (same time as the weekly REED post). If the file is
missing, it is seeded with the current scores (badges show ±0).
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
OUT = os.path.join(ROOT, "data", "reed_baseline.json")
CUTOVER_HOUR = 8
CUTOVER_MINUTE = 5


def week_of(now: datetime) -> datetime:
    now = now.astimezone(PT)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=CUTOVER_HOUR, minute=CUTOVER_MINUTE, second=0, microsecond=0
    )
    if now < monday:
        monday -= timedelta(days=7)
    return monday


def current_scores(payload: dict) -> dict[str, int]:
    scores: dict[str, int] = {}
    for rep in payload.get("repeaters") or []:
        slug = rep.get("slug")
        reed = rep.get("reed") or {}
        total = reed.get("total")
        if slug and isinstance(total, (int, float)):
            scores[slug] = int(total)
    return scores


def parse_week(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PT)
    return dt.astimezone(PT)


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        data = load_json(DATA)
    except Exception as exc:  # noqa: BLE001
        print(f"No data.json to baseline: {exc}", file=sys.stderr)
        return 0

    scores = current_scores(data)
    if not scores:
        print("No REED totals in data.json; leaving baseline unchanged.", file=sys.stderr)
        return 0

    this_week = week_of(now)
    old: dict = {}
    try:
        old = load_json(OUT)
    except Exception:  # noqa: BLE001
        old = {}

    old_week = parse_week(old.get("week_of"))
    old_scores = old.get("scores") if isinstance(old.get("scores"), dict) else {}
    need = (not old_scores) or (old_week is None) or (old_week < this_week)
    if not need:
        print(f"Baseline current for week_of {old_week.isoformat()}")
        return 0

    captured = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    seeded = not old_scores
    # On a mid-week first seed, keep this_week so the next refresh is next Monday 8:05 PT.
    payload = {
        "week_of": this_week.isoformat(),
        "captured_at": captured,
        "timezone": "America/Los_Angeles",
        "note": (
            "Seeded with current scores; first weekly refresh is the first run on or after Monday 8:05 AM PT."
            if seeded
            else "Weekly baseline captured at the first update on or after Monday 8:05 AM PT."
        ),
        "scores": scores if seeded else {**{k: int(v) for k, v in old_scores.items() if isinstance(v, (int, float))}, **scores},
    }
    write_json(OUT, payload)
    print(("Seeded" if seeded else "Refreshed") + f" baseline week_of {this_week.isoformat()} scores={scores}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
