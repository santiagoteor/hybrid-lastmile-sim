"""M6 operational clock. GLIMS evaluates each route's OSRM travel segments."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, date, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from glims.routing.osrm_client import get_osrm_host, osrm_distance_duration_table_rectangular
from glims.traffic.timeline import evaluate_route_timeline
from glims.common.constants import (
    SERVICE_TIME_PER_STOP_MIN, DIRECT_VAN_LOADING_TIME_PER_ROUTE_MIN,
    TRUCK_LOADING_TIME_PER_ROUTE_MIN, BIKE_PREPARATION_TIME_PER_ROUTE_MIN,
)

LEGS = (("supply", "driving"), ("microhub_supply", "driving"),
        ("bike_delivery", "cycling"), ("van_delivery", "driving"))


class _NeutralTraffic:
    """No synthetic congestion; use OSRM durations unchanged."""
    def get_multiplier(self, *, zone, departure_datetime):
        return SimpleNamespace(multiplier=1.0, source="osrm_unadjusted")


def _clock(value):
    return time.fromisoformat(value)


def _read(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _timestamp(value):
    return value.isoformat(timespec="seconds")


def _config(settings):
    """GLIMS semantics: last supply service <= end - margin; route return <= end."""
    if not isinstance(settings, dict):
        raise ValueError("operating_hours must be an object")
    allowed = {"simulation_date", "start", "end", "last_service_margin_minutes",
               "van_loading_minutes", "truck_loading_minutes", "bike_preparation_minutes",
               "service_time_per_stop_minutes"}
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"Unknown operating_hours options: {sorted(unknown)}")
    if "simulation_date" not in settings:
        raise ValueError("operating_hours.simulation_date is required for reproducible timestamps")
    day = date.fromisoformat(settings["simulation_date"])
    start = datetime.combine(day, _clock(settings.get("start", "09:00")))
    end = datetime.combine(day, _clock(settings.get("end", "17:00")))
    if end <= start:
        raise ValueError("Operating shift end must be after start")
    margin = float(settings.get("last_service_margin_minutes", 30))
    prep = {
        "supply": float(settings.get("truck_loading_minutes", TRUCK_LOADING_TIME_PER_ROUTE_MIN)),
        "microhub_supply": float(settings.get("truck_loading_minutes", TRUCK_LOADING_TIME_PER_ROUTE_MIN)),
        "bike_delivery": float(settings.get("bike_preparation_minutes", BIKE_PREPARATION_TIME_PER_ROUTE_MIN)),
        "van_delivery": float(settings.get("van_loading_minutes", DIRECT_VAN_LOADING_TIME_PER_ROUTE_MIN)),
    }
    service = float(settings.get("service_time_per_stop_minutes", SERVICE_TIME_PER_STOP_MIN))
    values = (margin, service, *prep.values())
    if not all(np.isfinite(x) and x >= 0 for x in values):
        raise ValueError("Margin, preparation and service durations must be finite nonnegative minutes")
    if margin >= (end - start).total_seconds() / 60:
        raise ValueError("last_service_margin_minutes must be smaller than the shift duration")
    return start, end, margin, prep, service


def _route_stops(folder, routes, is_home):
    if is_home:
        stops = defaultdict(list)
        for s in _read(folder / "route_stops.csv"):
            stops[str(s["route_id"])].append(s)
        return {key: sorted(rows, key=lambda s: int(s["stop_position"])) for key, rows in stops.items()}
    visits = _read(folder / "routed_supply_visits.csv")
    result = {}
    for route in routes:
        result[str(route["route_id"])] = [dict(stop_id=visits[int(i)-1]["Location"],
            latitude=visits[int(i)-1]["Latitude"], longitude=visits[int(i)-1]["Longitude"],
            package_load=visits[int(i)-1]["Demand"], stop_position=j)
            for j, i in enumerate(json.loads(route["stop_indices_json"]), 1)]
    return result


def _diagonal_edge_minutes(durations):
    """GLIMS rectangular OSRM durations are already expressed in minutes."""
    matrix = np.asarray(durations, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise ValueError("Expected square diagonal-pair OSRM duration block")
    return [float(matrix[i, i]) for i in range(len(matrix))]


def build_operating_timeline(output, city, sites, settings, *, block_size=100):
    """Calculate route/stop clocks and microhub readiness; never modify route allocation."""
    start, end, margin, preparation, service = _config(settings)
    root = Path(output) / "routing"
    coords = {s.site_id: (float(s.longitude), float(s.latitude))
              for category in sites.values() for s in category}
    rows_routes, rows_stops = [], []
    hub_ready = {}
    for leg, mode in LEGS:
        folder = root / leg
        routes = _read(folder / "routes.csv")
        if not routes:
            continue
        stop_map = _route_stops(folder, routes, leg in ("bike_delivery", "van_delivery"))
        tasks = []
        sources, destinations = [], []
        for route in routes:
            rid = str(route["route_id"])
            depot = str(route.get("microhub_id") or route.get("origin_id") or "")
            if not depot:
                centers = _read(folder / "selected_center.csv")
                if centers:
                    depot = str(centers[0].get("Location") or "")
            if depot not in coords:
                raise ValueError(f"Timeline: unknown depot {depot!r} for {leg} route {rid}")
            stops = stop_map.get(rid, [])
            nodes = [coords[depot]] + [(float(s["longitude"]), float(s["latitude"])) for s in stops]
            edges = list(zip(nodes, nodes[1:] + nodes[:1]))
            offset = len(sources)
            for a, b in edges:
                sources.append(a)
                destinations.append(b)
            tasks.append((route, rid, depot, stops, offset, len(edges)))
        # Chunk edge pairs to avoid a quadratic full-leg matrix in memory.
        edge_minutes_all = []
        for first in range(0, len(sources), int(block_size)):
            left = sources[first:first + int(block_size)]
            right = destinations[first:first + int(block_size)]
            _, durations = osrm_distance_duration_table_rectangular(
                left, right, host=get_osrm_host(city, mode), profile=mode,
                block_size=int(block_size))
            # GLIMS OSRM client already returns durations in MINUTES (see
            # glims.routing.osrm_client._fetch_table_block: seconds / 60).
            # Dividing again would underestimate travel by a factor of 60.
            edge_minutes_all.extend(_diagonal_edge_minutes(durations))
        for route, rid, depot, stops, offset, count in tasks:
            edge_minutes = edge_minutes_all[offset:offset + count]
            if not all(np.isfinite(x) and x >= 0 for x in edge_minutes):
                raise ValueError(f"Unreachable OSRM segment in {leg} route {rid}")
            matrix = np.zeros((count, count), dtype=float)
            for i, minutes in enumerate(edge_minutes):
                matrix[i, (i + 1) % count] = minutes
            departure = max(start, hub_ready.get(depot, start)) if leg == "bike_delivery" else start
            timeline = evaluate_route_timeline(
                route=list(range(1, count)), duration_matrix=matrix,
                route_start=departure, shift_end=end,
                traffic_provider=_NeutralTraffic(), traffic_zone=None,
                service_time_per_stop_min=service,
                route_preparation_time_min=preparation[leg])
            within_shift = timeline.route_end <= end
            cutoff = end - timedelta(minutes=margin)
            last_service_end = (timeline.segments[-2].arrival_datetime + timedelta(minutes=service)
                                if stops else timeline.route_start)
            # GLIMS applies the cutoff to supply visits; last-mile routes must return by shift end.
            supply_leg = leg in ("supply", "microhub_supply")
            last_service_feasible = (not supply_leg or last_service_end <= cutoff)
            feasible = within_shift and last_service_feasible
            rows_routes.append(dict(leg=leg, route_id=rid, depot=depot,
                simulation_date=start.date().isoformat(), shift_start_time=start.time().isoformat(timespec="minutes"),
                shift_end_time=end.time().isoformat(timespec="minutes"), last_service_margin_minutes=margin,
                last_service_cutoff_datetime=_timestamp(cutoff) if supply_leg else "",
                last_service_end_datetime=_timestamp(last_service_end) if stops else "",
                last_service_feasible=last_service_feasible,
                route_start_datetime=_timestamp(timeline.route_start),
                route_end_datetime=_timestamp(timeline.route_end),
                base_travel_time_min=timeline.base_travel_time_min,
                time_dependent_travel_time_min=timeline.adjusted_travel_time_min,
                traffic_delay_min=timeline.traffic_delay_min,
                service_time_min=timeline.service_time_min,
                preparation_time_min=timeline.preparation_time_min,
                timeline_duration_min=timeline.total_duration_min,
                within_ordinary_shift=within_shift, shift_feasible=feasible, traffic_source="osrm_unadjusted"))
            for stop, segment in zip(stops, timeline.segments[:-1]):
                arrival = segment.arrival_datetime
                finished = arrival + timedelta(minutes=service)
                rows_stops.append(dict(leg=leg, route_id=rid,
                    stop_position=stop["stop_position"], stop_id=stop["stop_id"],
                    arrival_datetime=_timestamp(arrival),
                    service_start_datetime=_timestamp(arrival),
                    service_end_datetime=_timestamp(finished),
                    base_service_time_min=service, effective_service_time_min=service))
                if leg == "microhub_supply":
                    hub_ready[str(stop["stop_id"])] = max(hub_ready.get(str(stop["stop_id"]), start), finished)
    dest = Path(output) / "routes"
    dest.mkdir(parents=True, exist_ok=True)
    _write(dest / "operating_route_timeline.csv", rows_routes,
        ["leg", "route_id", "depot", "simulation_date", "shift_start_time", "shift_end_time",
         "last_service_margin_minutes", "last_service_cutoff_datetime",
         "last_service_end_datetime", "last_service_feasible",
         "route_start_datetime", "route_end_datetime",
         "base_travel_time_min", "time_dependent_travel_time_min", "traffic_delay_min",
         "service_time_min", "preparation_time_min", "timeline_duration_min",
         "within_ordinary_shift", "shift_feasible", "traffic_source"])
    _write(dest / "operating_stop_timeline.csv", rows_stops,
        ["leg", "route_id", "stop_position", "stop_id", "arrival_datetime",
         "service_start_datetime", "service_end_datetime", "base_service_time_min", "effective_service_time_min"])
    return {"route_count": len(rows_routes), "stop_count": len(rows_stops),
            "shift_infeasible_routes": sum(not r["shift_feasible"] for r in rows_routes),
            "last_service_cutoff_violations": sum(not r["last_service_feasible"] for r in rows_routes),
            "route_timeline_csv": "routes/operating_route_timeline.csv",
            "stop_timeline_csv": "routes/operating_stop_timeline.csv",
            "traffic_source": "osrm_unadjusted"}
