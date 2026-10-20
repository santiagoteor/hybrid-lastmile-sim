"""Van home deliveries: M6 selects one CC; GLIMS solves driving routes."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json
import pandas as pd
from glims.routing.osrm_client import get_osrm_host
from glims.routing.osrm_router import select_logistics_center
from hybrid_lastmile.workflow.bike_routing import plan_home_delivery

@dataclass(frozen=True)
class Depot:
    site_id: str
    latitude: float
    longitude: float

def plan_van_delivery(run_dir, city, customers, sites, allocations, capacity, settings):
    assigned = [a for a in allocations if a.delivery_mode == 'VAN']
    folder = Path(run_dir) / 'routing' / 'van_delivery'
    folder.mkdir(parents=True, exist_ok=True)
    customer_map = {c.customer_id: c for c in customers}
    if assigned:
        centers = sites.get('CC', [])
        if not centers:
            raise ValueError('Van home delivery requires logistics centers')
        weight = sum(a.packages for a in assigned)
        if weight <= 0:
            raise ValueError('Van demand must be positive')
        latitude = sum(customer_map[a.customer_id].latitude*a.packages for a in assigned)/weight
        longitude = sum(customer_map[a.customer_id].longitude*a.packages for a in assigned)/weight
        candidates = pd.DataFrame([{'Location': c.site_id, 'Latitude': c.latitude,
                                    'Longitude': c.longitude} for c in centers])
        center = select_logistics_center(candidates, latitude, longitude,
            osrm_host=get_osrm_host(city, 'driving'), osrm_profile='driving')
        depot = Depot(str(center['Location']), float(center['Latitude']), float(center['Longitude']))
        pd.DataFrame([dict(center)]).to_csv(folder/'selected_center.csv', index=False)
        prepared = [a.__class__(**{**a.__dict__, 'facility_id': depot.site_id}) if a.delivery_mode == 'VAN' else a
                    for a in allocations]
        depots = {depot.site_id: depot}
    else:
        prepared = allocations
        depots = {}
    result = plan_home_delivery(run_dir, city, customers, prepared, capacity, settings,
        depots=depots, mode='VAN', transport='driving',
        folder_name='van_delivery', prefix='VAN', vehicle_type='van')
    if assigned:
        result['selected_center'] = depot.site_id
        (folder/'route_summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
