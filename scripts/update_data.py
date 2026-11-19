#!/usr/bin/env python3
"""Fetch public CoreScope data for the REED mesh repeaters and write data.json.

Metrics (no REED score, no grades, no coordinates or neighbor names):
  relays_24h / relays_1h, last_heard, online (24h window)
  heard_nodes: unique 7-day reach links with we_hear > 0
  two_way: bidir reach links with max(we_hear, they_hear) >= 20
  max_link_mi: max CoreScope distance_km among those links, in miles (omitted if none)
  observers: /health relayObserverCount (fallback: observer list length)
  avail_pct: /analytics?days=7 computedStats.availabilityPct
  snr_median / snr_top / snr_hist: neighbor avg_snr values only
  relays_spark: recent relays_24h samples accumulated in data/relays_history.json

Only public CoreScope data is used. Coordinates, neighbor names, and locations
are never written. Stdlib only. Max 2 parallel requests with 429/503 backoff.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = "https://corescope.wcmesh.com"
USER_AGENT = "reed-mesh-status/1.0 (+https://andrewlewisreed.github.io)"
REPEATERS = [
    ("Mr. DuPont", "DuPont", "dupont", "2af8f8abe8cdebecf0a1226f77a329a2df0e0be1e503c4540a057877032e91c0"),
    ("Saigon Westminster", "Saigon", "saigon", "01d6facbe50e8a082f50f68827ab6773c2a86226fc87b57fddce21012d36a6a1"),
    ("Funky Pair", "Funky", "funky", "2823823887846a995da49684adc7c0c9f64a7708d94c706d3435311eee96a6b0"),
    ("Wardcamp", "Wardcamp", "wardcamp", "f3e203f3938f67e8415b5ae4d2f301a90b8f94970367b46158a0172fb1f28571"),
]
MIN_PKTS = 20
OFFLINE_AFTER_H = 24
KM_TO_MI = 0.621371
SNR_BINS = (-20, -10, -5, 0, 5, 10, 20)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "data.json")
HISTORY = os.path.join(ROOT, "data", "relays_history.json")
HISTORY_DAYS = 14
MAX_SPARK = 48  # ~24 hours of :07/:37 samples, or more if kept longer; we cap display at 48


def get_json(url, timeout=40):
    status = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            if exc.code not in (429, 503) or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            status = str(exc)
            if attempt == 3:
                raise
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"HTTP {status}")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def reach_metrics(reach):
    links = (reach or {}).get("links") or []
    heard = 0
    two_way = 0
    dists = []
    for ln in links:
        try:
            we, they = float(ln.get("we_hear") or 0), float(ln.get("they_hear") or 0)
        except (TypeError, ValueError):
            we, they = 0.0, 0.0
        if we > 0:
            heard += 1
        if ln.get("bidir") and max(we, they) >= MIN_PKTS:
            two_way += 1
        dk = ln.get("distance_km")
        if dk is not None:
            try:
                dists.append(float(dk))
            except (TypeError, ValueError):
                pass
    farthest = round(max(dists) * KM_TO_MI, 1) if dists else None
    observers = (reach or {}).get("importance", {}).get("direct_observers")
    try:
        observers = int(observers) if observers is not None else None
    except (TypeError, ValueError):
        observers = None
    return {"heard_nodes": heard, "two_way": two_way, "max_link_mi": farthest, "reach_observers": observers}


def snr_metrics(neigh):
    vals = []
    for n in (neigh or {}).get("neighbors") or []:
        v = n.get("avg_snr")
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return {"snr_median": None, "snr_top": None, "snr_count": 0, "snr_hist": [0] * (len(SNR_BINS) - 1)}
    hist = [0] * (len(SNR_BINS) - 1)
    for v in vals:
        placed = False
        for i in range(len(SNR_BINS) - 1):
            if v < SNR_BINS[i + 1] or i == len(SNR_BINS) - 2:
                hist[i] += 1
                placed = True
                break
        if not placed:
            hist[-1] += 1
    return {
        "snr_median": round(statistics.median(vals), 1),
        "snr_top": round(max(vals), 1),
        "snr_count": len(vals),
        "snr_hist": hist,
    }


def health_observers(health):
    if not health:
        return None
    v = health.get("relayObserverCount")
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    obs = health.get("observers")
    if isinstance(obs, list):
        return len(obs)
    return None


def avail_pct(analytics):
    cs = (analytics or {}).get("computedStats") or {}
    v = cs.get("availabilityPct")
    if v is None:
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def write_json(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")


def update_history(history, slug, now, relays):
    if relays is None:
        return history.get(slug) or []
    cutoff = (now - timedelta(days=HISTORY_DAYS)).isoformat().replace("+00:00", "Z")
    rows = [r for r in (history.get(slug) or []) if r.get("t") and r["t"] >= cutoff]
    last = rows[-1] if rows else None
    stamp = now.isoformat(timespec="seconds").replace("+00:00", "Z")
    if last and last.get("relays_24h") == relays:
        # refresh timestamp on same value at most every 3 hours to keep a readable sparkline
        last_t = parse_ts(last.get("t"))
        if last_t and (now - last_t).total_seconds() < 3 * 3600:
            return rows
    rows.append({"t": stamp, "relays_24h": int(relays)})
    return rows[-200:]


def fmt_rly(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "—"
    if n >= 1000:
        s = f"{n / 1000:.1f}k"
        return s.replace(".0k", "k")
    return str(n)


def main():
    now = datetime.now(timezone.utc)
    urls = []
    for _n, _s, _g, pk in REPEATERS:
        b = f"{BASE}/api/nodes/{pk}"
        urls += [b, b + "/reach", b + "/neighbors", b + "/health", b + "/analytics?days=7"]
    results = {}

    def job(u):
        try:
            return u, get_json(u), None
        except Exception as exc:  # noqa: BLE001
            return u, None, f"{type(exc).__name__}: {exc}"[:120]

    with ThreadPoolExecutor(max_workers=2) as ex:
        for u, data, err in ex.map(job, urls):
            results[u] = (data, err)

    old = {}
    try:
        old = {r["slug"]: r for r in load_json(OUT, {}).get("repeaters", [])}
    except Exception:  # noqa: BLE001
        old = {}
    history = load_json(HISTORY, {})
    if not isinstance(history, dict):
        history = {}

    reps, errors = [], []
    for name, short, slug, pk in REPEATERS:
        b = f"{BASE}/api/nodes/{pk}"
        node_d, node_e = results[b]
        reach_d, reach_e = results[b + "/reach"]
        neigh_d, neigh_e = results[b + "/neighbors"]
        health_d, health_e = results[b + "/health"]
        anal_d, anal_e = results[b + "/analytics?days=7"]
        prev = old.get(slug, {}) or {}
        e = {"name": name, "short": short, "slug": slug, "pubkey": pk,
             "corescope_url": f"{BASE}/#/nodes/{pk}"}
        if node_d and isinstance(node_d.get("node"), dict):
            n = node_d["node"]
            lh = parse_ts(n.get("last_heard") or n.get("last_seen"))
            e.update({
                "last_heard": lh.isoformat().replace("+00:00", "Z") if lh else None,
                "online": bool(lh and (now - lh).total_seconds() < OFFLINE_AFTER_H * 3600),
                "relays_24h": n.get("relay_count_24h"),
                "relays_1h": n.get("relay_count_1h"),
                "status_stale": False,
            })
        else:
            errors.append(f"{short} node: {node_e}")
            for k in ("last_heard", "relays_24h", "relays_1h"):
                e[k] = prev.get(k)
            lh = parse_ts(e.get("last_heard"))
            e["online"] = bool(lh and (now - lh).total_seconds() < OFFLINE_AFTER_H * 3600)
            e["status_stale"] = True

        if reach_d is not None:
            e.update(reach_metrics(reach_d))
            e["reach_stale"] = False
        else:
            errors.append(f"{short} reach: {reach_e}")
            for k in ("heard_nodes", "two_way", "max_link_mi"):
                e[k] = prev.get(k)
            e["reach_stale"] = True

        if neigh_d is not None:
            e.update(snr_metrics(neigh_d))
            e["snr_stale"] = False
        else:
            errors.append(f"{short} neighbors: {neigh_e}")
            for k in ("snr_median", "snr_top", "snr_count", "snr_hist"):
                e[k] = prev.get(k)
            e["snr_stale"] = True

        obs = health_observers(health_d)
        if obs is not None:
            e["observers"] = obs
            e["observers_stale"] = False
        else:
            if health_e:
                errors.append(f"{short} health: {health_e}")
            # fall back to reach direct_observers, then previous
            e["observers"] = e.pop("reach_observers", None)
            if e["observers"] is None:
                e["observers"] = prev.get("observers")
            e["observers_stale"] = e["observers"] is not None and health_d is None
        e.pop("reach_observers", None)

        av = avail_pct(anal_d)
        if av is not None:
            e["avail_pct"] = av
            e["avail_stale"] = False
        else:
            if anal_e:
                errors.append(f"{short} analytics: {anal_e}")
            e["avail_pct"] = prev.get("avail_pct")
            e["avail_stale"] = True

        history[slug] = update_history(history, slug, now, e.get("relays_24h"))
        e["relays_spark"] = [int(r["relays_24h"]) for r in history[slug][-MAX_SPARK:] if r.get("relays_24h") is not None]
        reps.append(e)

    write_json(HISTORY, history)
    order = ["saigon", "dupont", "funky", "wardcamp"]
    ordered = [next(r for r in reps if r["slug"] == s) for s in order if any(r["slug"] == s for r in reps)]
    summary = "REED mesh: " + ", ".join(
        f"{r['short']} {fmt_rly(r.get('relays_24h'))} rly/24h" if i == 0 else f"{r['short']} {fmt_rly(r.get('relays_24h'))}"
        for i, r in enumerate(ordered)
    )
    payload = {
        "repeaters": reps,
        "order": order,
        "summary": summary,
        "errors": errors,
        "offline_after_hours": OFFLINE_AFTER_H,
        "snr_bins": list(SNR_BINS),
        "source": BASE,
    }
    old_full = load_json(OUT, {})
    same = {k: v for k, v in old_full.items() if k != "updated_at"} == payload
    payload["updated_at"] = old_full.get("updated_at") if same and old_full.get("updated_at") else now.isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {"updated_at": payload.pop("updated_at"), **payload}
    if all(r.get("last_heard") is None and r.get("relays_24h") is None for r in reps):
        print("All fetches failed; keeping old data.json", file=sys.stderr)
        return 1
    write_json(OUT, payload)
    print(payload["summary"])
    for err in errors:
        print("warn:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
