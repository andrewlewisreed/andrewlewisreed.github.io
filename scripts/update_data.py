#!/usr/bin/env python3
"""Fetch public CoreScope data for the REED mesh repeaters and write data.json.

REED score matches the meshcore-bot's local/reed_score.py (defaults):
  Two-way (0-50): bal = sum over reach links with bidir and max(we,they) >= 20 of
                  min(we,they)/max(we,they);  two = min(50, bal/30*50)
  Quality (0-50): avg_snr of neighbors (avg_snr not null, count >= 20), best 5,
                  padded with -10 dB to 5 values; med = median;
                  q = clamp((med + 7)/15*50, 0, 50)
  Total = round(two + q); grade A>=85 B>=70 C>=55 D>=40 else F.
Only public CoreScope data is used. No locations are written.
Stdlib only (no pip install needed).
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://corescope.wcmesh.com"
USER_AGENT = "reed-mesh-status/1.0 (+https://andrewlewisreed.github.io)"
REPEATERS = [
    # (display name, short name, slug, public key)
    ("Mr. DuPont", "DuPont", "dupont", "2af8f8abe8cdebecf0a1226f77a329a2df0e0be1e503c4540a057877032e91c0"),
    ("Saigon Westminster", "Saigon", "saigon", "01d6facbe50e8a082f50f68827ab6773c2a86226fc87b57fddce21012d36a6a1"),
    ("Funky Pair", "Funky", "funky", "2823823887846a995da49684adc7c0c9f64a7708d94c706d3435311eee96a6b0"),
    ("Wardcamp", "Wardcamp", "wardcamp", "f3e203f3938f67e8415b5ae4d2f301a90b8f94970367b46158a0172fb1f28571"),
]
S = {"min_pkts": 20, "target_links": 30.0, "snr_floor": -7.0, "snr_full": 8.0,
     "top_n": 5, "pad_snr": -10.0, "grades": (("A", 85), ("B", 70), ("C", 55), ("D", 40))}
OFFLINE_AFTER_H = 24
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data.json")


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


def grade(total):
    for g, cut in S["grades"]:
        if total >= cut:
            return g
    return "F"


def reed_score(reach, neigh):
    bal = 0.0
    for ln in (reach or {}).get("links") or []:
        try:
            we, they = float(ln.get("we_hear") or 0), float(ln.get("they_hear") or 0)
        except (TypeError, ValueError):
            continue
        hi = max(we, they)
        if ln.get("bidir") and hi >= S["min_pkts"]:
            bal += min(we, they) / hi
    two = min(50.0, bal / S["target_links"] * 50.0)
    snrs = []
    for n in (neigh or {}).get("neighbors") or []:
        v, c = n.get("avg_snr"), n.get("count") or 0
        if v is None:
            continue
        try:
            if float(c) >= S["min_pkts"]:
                snrs.append(float(v))
        except (TypeError, ValueError):
            continue
    snrs.sort(reverse=True)
    top = snrs[: S["top_n"]]
    top += [S["pad_snr"]] * (S["top_n"] - len(top))
    med = statistics.median(top) if top else S["pad_snr"]
    span = (S["snr_full"] - S["snr_floor"]) or 1.0
    q = max(0.0, min(50.0, (med - S["snr_floor"]) / span * 50.0))
    total = int(round(two + q))
    return {"total": total, "grade": grade(total), "two_way": round(two, 1), "quality": round(q, 1),
            "links": round(bal, 1), "median_snr": round(med, 1), "snr_links": len(snrs)}


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def main():
    now = datetime.now(timezone.utc)
    urls = []
    for _n, _s, _g, pk in REPEATERS:
        b = f"{BASE}/api/nodes/{pk}"
        urls += [b, b + "/reach", b + "/neighbors"]
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
        with open(OUT, encoding="utf-8") as fh:
            old = {r["slug"]: r for r in json.load(fh).get("repeaters", [])}
    except Exception:  # noqa: BLE001
        pass

    reps, errors = [], []
    for name, short, slug, pk in REPEATERS:
        b = f"{BASE}/api/nodes/{pk}"
        node_d, node_e = results[b]
        reach_d, reach_e = results[b + "/reach"]
        neigh_d, neigh_e = results[b + "/neighbors"]
        prev = old.get(slug, {})
        e = {"name": name, "short": short, "slug": slug, "pubkey": pk,
             "corescope_url": f"{BASE}/#/nodes/{pk}"}
        if node_d and isinstance(node_d.get("node"), dict):
            n = node_d["node"]
            lh = parse_ts(n.get("last_heard") or n.get("last_seen"))
            e.update({
                "last_heard": lh.isoformat().replace("+00:00", "Z") if lh else None,
                "online": bool(lh and (now - lh).total_seconds() < OFFLINE_AFTER_H * 3600),
                "usefulness_score": round(float(n["usefulness_score"]), 3) if n.get("usefulness_score") is not None else None,
                "usefulness_grade": n.get("usefulness_grade"),
                "relays_24h": n.get("relay_count_24h"),
                "relays_1h": n.get("relay_count_1h"),
                "status_stale": False,
            })
        else:
            errors.append(f"{short} node: {node_e}")
            for k in ("last_heard", "usefulness_score", "usefulness_grade", "relays_24h", "relays_1h"):
                e[k] = prev.get(k)
            lh = parse_ts(e.get("last_heard"))
            e["online"] = bool(lh and (now - lh).total_seconds() < OFFLINE_AFTER_H * 3600)
            e["status_stale"] = True
        if reach_d is not None and neigh_d is not None:
            e["reed"] = reed_score(reach_d, neigh_d)
            e["reed_stale"] = False
        else:
            errors.append(f"{short} score: {reach_e or neigh_e}")
            e["reed"] = prev.get("reed")
            e["reed_stale"] = True
        reps.append(e)

    scored = sorted([r for r in reps if r.get("reed")], key=lambda r: (-r["reed"]["total"], r["short"].lower()))
    payload = {
        "repeaters": reps,
        "reed_order": [r["slug"] for r in scored],
        "summary": "REED score: " + ", ".join(f"{r['short']} {r['reed']['total']}{r['reed']['grade']}" for r in scored),
        "errors": errors,
        "offline_after_hours": OFFLINE_AFTER_H,
        "source": BASE,
    }
    # Only bump updated_at (and so commit) when the data itself changed.
    old_full = {}
    try:
        with open(OUT, encoding="utf-8") as fh:
            old_full = json.load(fh)
    except Exception:  # noqa: BLE001
        pass
    same = {k: v for k, v in old_full.items() if k != "updated_at"} == payload
    payload["updated_at"] = old_full.get("updated_at") if same and old_full.get("updated_at") else now.isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {"updated_at": payload.pop("updated_at"), **payload}
    if all(r.get("reed") is None and r.get("last_heard") is None for r in reps):
        print("All fetches failed; keeping old data.json", file=sys.stderr)
        return 1
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, indent=1)
        fh.write("\n")
    print(payload["summary"])
    for err in errors:
        print("warn:", err, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
