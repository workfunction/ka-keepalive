#!/usr/bin/env python3
"""ka_report: daily summary of ka.log (metadata only).

Usage: ka_report.py [--day YYYY-MM-DD] [--log PATH]   (default: today, local time; KA_STATE_DIR/ka.log)

"Potential avoided rewarm": for each idle stretch (pings between two real main-loop requests of a
session) that had >=1 successful ping, the returning real request counts as an avoided rewarm worth
the last successful ping's avoided_rewarm_usd_if_returned. Credited to the day of the returning request.
"""
import argparse, collections, datetime, json, os, sys


def day_of(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def load(path):
    evs = []
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and "t" in e:
                evs.append(e)
    evs.sort(key=lambda e: e["t"])
    return evs


def report(evs, day):
    pings = collections.defaultdict(lambda: {"n": 0, "ok": 0, "cost": 0.0, "model": None})
    stops = collections.Counter()
    total_cost = 0.0
    avoided, returns = 0.0, 0
    stretch = {}   # sid -> {"first_t", "last_ok_avoided"} for the open idle stretch
    for e in evs:
        ev, sid = e.get("event"), e.get("sid")
        today = day_of(e["t"]) == day
        if ev == "ping":
            st = stretch.setdefault(sid, {"first_t": e["t"], "last_ok_avoided": None})
            if e.get("ok") and e.get("avoided_rewarm_usd_if_returned") is not None:
                st["last_ok_avoided"] = e["avoided_rewarm_usd_if_returned"]
            if today:
                p = pings[sid]
                p["n"] += 1
                p["ok"] += 1 if e.get("ok") else 0
                p["cost"] += e.get("est_cost_usd") or 0.0
                p["model"] = e.get("model")
                total_cost += e.get("est_cost_usd") or 0.0
        elif ev == "real" and e.get("captured"):
            st = stretch.pop(sid, None)
            if st and today and st["last_ok_avoided"]:
                avoided += st["last_ok_avoided"]
                returns += 1
        elif ev == "decision" and e.get("action") == "stop" and today:
            stops[e.get("reason")] += 1
    return pings, total_cost, stops, avoided, returns


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.date.today().isoformat())
    ap.add_argument("--log", default=os.path.join(
        os.path.expanduser(os.environ.get("KA_STATE_DIR", "~/.claude/ka")), "ka.log"))
    a = ap.parse_args(argv)
    if not os.path.exists(a.log):
        print(f"no log at {a.log}")
        return 0
    pings, total, stops, avoided, returns = report(load(a.log), a.day)
    print(f"ka report {a.day}  ({a.log})")
    print("pings per session:")
    if not pings:
        print("  (none)")
    for sid, p in sorted(pings.items(), key=lambda kv: -kv[1]["n"]):
        print(f"  {sid[:8]}  {p['model'] or '?':<18} pings {p['n']:>3} (ok {p['ok']})  cost ${p['cost']:.4f}")
    print(f"total ping cost: ${total:.4f}")
    print("stop reasons: " + (", ".join(f"{k} x{v}" for k, v in stops.most_common()) or "(none)"))
    print(f"potential avoided rewarm: ${avoided:.4f} over {returns} return(s) after >=1 ok ping")
    print(f"net (avoided - ping cost): ${avoided - total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
