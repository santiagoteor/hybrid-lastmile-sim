"""Preflight directed OSRM accessibility of facility supply before routing.

Uses GLIMS's own conservative matrix sanitizer. A disconnected facility is
removed from candidate sites *before* rerunning customer assignment, never
silently removed from an already allocated supply plan.
"""
from __future__ import annotations

import pandas as pd

from glims.routing.osrm_router import CapacityAwareOsrmRouter
from glims.routing.osrm_validation import sanitize_osrm_routing_inputs


def inaccessible_supply_facilities(run_dir, city, *, folder_name: str) -> list[str]:
    """Return strongly disconnected facility IDs for a prepared supply leg.

    Sparse invalid relations remain fatal under GLIMS's conservative sanitizer.
    Facility-level coordinates are used instead of duplicated capacity visits,
    so the disconnection threshold reflects distinct destinations.
    """
    folder = run_dir / 'routing' / folder_name
    demand_name = 'pudo_demand.csv' if folder_name == 'supply' else 'microhub_demand.csv'
    facilities = pd.read_csv(folder / demand_name, dtype={'Location': str})
    if facilities.empty:
        return []
    center = pd.read_csv(folder / 'selected_center.csv').iloc[0]
    router = CapacityAwareOsrmRouter(city)
    coords = facilities[['Latitude', 'Longitude']].reset_index(drop=True)
    distance, duration = router.get_matrices(
        depot_latitude=float(center['Latitude']),
        depot_longitude=float(center['Longitude']),
        clients=coords,
        transport_mode='driving',
    )
    _, _, _, _, positions, _ = sanitize_osrm_routing_inputs(
        distance, duration, clients=coords,
        client_demands=facilities['Demand'].to_numpy(dtype=float),
        transport_mode='driving',
    )
    return facilities.iloc[positions]['Location'].astype(str).tolist()
