"""Reconcile M6's legacy schedule diagnostics with the computed operating timeline.

No schedule is inferred from route duration or a nominal shift. Missing/inconsistent
records fail closed; this module does not modify routing or timetable calculations.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LEG_NAMES = {
    "supply": "PUDO_SUPPLY",
    "microhub_supply": "MICROHUB_SUPPLY",
    "bike_delivery": "CARGO_BIKE",
    "van_delivery": "VAN",
}


def _read(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _true(value):
    return str(value).strip().lower() == "true"


def reconcile_schedule_diagnostics(run_dir):
    """Replace legacy not_evaluated entries using actual route/stop clocks.

    Returns summary counts. Raises ValueError if clocks are missing or inconsistent.
    Shift violations are reported as nonviable rather than hidden or raised.
    """
    root = Path(run_dir)
    route_file = root / "routes" / "operating_route_timeline.csv"
    stop_file = root / "routes" / "operating_stop_timeline.csv"
    diagnostics_file = root / "routing" / "schedule_diagnostics.csv"
    if not (route_file.is_file() and stop_file.is_file() and diagnostics_file.is_file()):
        raise ValueError("Missing operating timeline or schedule_diagnostics.csv")
    routes, stops, legacy = _read(route_file), _read(stop_file), _read(diagnostics_file)
    if not routes:
        raise ValueError("Operating timeline has no routes")
    by_leg = defaultdict(list)
    route_keys = set()
    for route in routes:
        leg = route["leg"]
        if leg not in LEG_NAMES:
            raise ValueError(f"Unknown timeline leg: {leg}")
        key = (leg, route["route_id"])
        if key in route_keys:
            raise ValueError(f"Duplicate timeline route: {key}")
        route_keys.add(key)
        begin = datetime.fromisoformat(route["route_start_datetime"])
        finish = datetime.fromisoformat(route["route_end_datetime"])
        if finish < begin:
            raise ValueError(f"Route finishes before starting: {key}")
        by_leg[leg].append(route)

    stops_by_route = defaultdict(list)
    hub_ready = {}
    for stop in stops:
        key = (stop["leg"], stop["route_id"])
        if key not in route_keys:
            raise ValueError(f"Stop references missing route: {key}")
        arrival = datetime.fromisoformat(stop["arrival_datetime"])
        service_start = datetime.fromisoformat(stop["service_start_datetime"])
        service_end = datetime.fromisoformat(stop["service_end_datetime"])
        if not arrival <= service_start <= service_end:
            raise ValueError(f"Inconsistent stop clock: {key}")
        stops_by_route[key].append((int(stop["stop_position"]), arrival, service_end))
        if stop["leg"] == "microhub_supply":
            hub = stop["stop_id"]
            hub_ready[hub] = max(hub_ready.get(hub, service_end), service_end)

    dependency_ok = {}
    for route in routes:
        key = (route["leg"], route["route_id"])
        route_start = datetime.fromisoformat(route["route_start_datetime"])
        route_end = datetime.fromisoformat(route["route_end_datetime"])
        ordered = sorted(stops_by_route[key])
        if any(a[1] > b[1] for a, b in zip(ordered, ordered[1:])):
            raise ValueError(f"Stops out of chronological order: {key}")
        if any(arrival < route_start or service_end > route_end for _, arrival, service_end in ordered):
            raise ValueError(f"Stop falls outside route: {key}")
        if route["leg"] == "bike_delivery":
            depot = route["depot"]
            if depot not in hub_ready:
                raise ValueError(f"Bike depot {depot} has no microhub supply arrival")
            dependency_ok[key] = route_start >= hub_ready[depot]
        else:
            dependency_ok[key] = True

    existing = {row["leg"]: row for row in legacy}
    if set(existing) != set(LEG_NAMES.values()):
        raise ValueError("Legacy diagnostics do not contain exactly the four M6 operations")
    output = []
    for leg, label in LEG_NAMES.items():
        leg_routes = by_leg[leg]
        clock_ok = bool(leg_routes) and all(_true(r["shift_feasible"]) for r in leg_routes)
        dependencies = all(dependency_ok[(leg, r["route_id"])] for r in leg_routes)
        starts = {r["shift_start_time"] for r in leg_routes}
        ends = {r["shift_end_time"] for r in leg_routes}
        if len(starts) > 1 or len(ends) > 1:
            raise ValueError(f"Inconsistent shift configuration for {leg}")
        if leg_routes:
            status = "shift_checked" if clock_ok and dependencies else "shift_infeasible"
        else:
            status = "no_routes"  # e.g. a zero-demand VAN leg
        output.append({"leg": label, "shift_start_time": next(iter(starts), ""),
                       "shift_end_time": next(iter(ends), ""),
                       "clock_schedule_verified": clock_ok,
                       "supply_dependency_verified": dependencies,
                       "status": status})
    with diagnostics_file.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["leg", "shift_start_time", "shift_end_time",
                            "clock_schedule_verified", "supply_dependency_verified", "status"])
        writer.writeheader()
        writer.writerows(output)
    return {"checked_routes": len(routes), "checked_stops": len(stops),
            "infeasible_routes": sum(not _true(r["shift_feasible"]) for r in routes),
            "status_by_leg": {r["leg"]: r["status"] for r in output}}
