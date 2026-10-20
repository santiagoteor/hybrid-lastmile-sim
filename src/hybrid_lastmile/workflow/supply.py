"""M6 -> GLIMS facility supply adapter; delegates splitting and CC choice to GLIMS."""
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path

import pandas as pd
from glims.routing.osrm_router import expand_facility_supply_visits, select_logistics_center
from glims.routing.osrm_client import get_osrm_host


def prepare_facility_supply(output_dir, sites, allocations, van_capacity, city, *, facility_type, delivery_mode, folder_name):
    """Create GLIMS-compatible PUDO demand, capacity visits and one trunk CC.

    The CC is selected for the package-weighted PUDO centroid, not independently
    for every PUDO. This is an explicit single-depot policy, not route optimization.
    """
    output_dir = Path(output_dir) / 'routing' / folder_name
    output_dir.mkdir(parents=True, exist_ok=True)
    pudo = {s.site_id: s for s in sites[facility_type]}
    by_pudo = defaultdict(int)
    for a in allocations:
        if a.delivery_mode == delivery_mode:
            if a.facility_id not in pudo:
                raise ValueError(f'Unknown {facility_type} {a.facility_id!r}')
            by_pudo[a.facility_id] += int(a.packages)
    rows = [{'Location': sid, 'Latitude': pudo[sid].latitude,
             'Longitude': pudo[sid].longitude, 'Demand': n}
            for sid, n in sorted(by_pudo.items())]
    facilities = pd.DataFrame(rows, columns=['Location','Latitude','Longitude','Demand'])
    facilities.to_csv(output_dir / ('pudo_demand.csv' if facility_type == 'PUDO' else 'microhub_demand.csv'), index=False)
    if facilities.empty:
        visits = pd.DataFrame(columns=['Location','Latitude','Longitude','Demand','SupplyVisit'])
    else:
        visits = expand_facility_supply_visits(facilities, float(van_capacity))
    visits.to_csv(output_dir / 'supply_visits.csv', index=False)
    if int(visits['Demand'].sum()) != sum(by_pudo.values()):
        raise AssertionError('GLIMS visit expansion did not conserve facility packages')
    result = {'facility_count': len(facilities), 'supply_visits': len(visits),
              'packages': sum(by_pudo.values()), 'center_policy': 'single_depot_weighted_centroid_roundtrip'}
    if not facilities.empty:
        centers = sites.get('CC', [])
        if not centers:
            raise ValueError('No logistics centers available to select a supply origin')
        candidates = pd.DataFrame([{'Location': s.site_id, 'Latitude': s.latitude,
                                    'Longitude': s.longitude} for s in centers])
        weight = facilities['Demand'].astype(float)
        latitude = float((facilities['Latitude'] * weight).sum() / weight.sum())
        longitude = float((facilities['Longitude'] * weight).sum() / weight.sum())
        center = select_logistics_center(candidates, latitude, longitude,
                                         osrm_host=get_osrm_host(city, 'driving'),
                                         osrm_profile='driving')
        selected = {'Location': str(center['Location']), 'Latitude': float(center['Latitude']),
                    'Longitude': float(center['Longitude']),
                    'distancia_troncal_total_km': float(center['distancia_troncal_total_km'])}
        pd.DataFrame([selected]).to_csv(output_dir / 'selected_center.csv', index=False)
        result['selected_center'] = selected
    return result


def prepare_pudo_supply(output_dir, sites, allocations, van_capacity, city):
    return prepare_facility_supply(output_dir, sites, allocations, van_capacity, city,
        facility_type='PUDO', delivery_mode='PUDO', folder_name='supply')


def prepare_microhub_supply(output_dir, sites, allocations, van_capacity, city):
    return prepare_facility_supply(output_dir, sites, allocations, van_capacity, city,
        facility_type='MICROHUB', delivery_mode='CARGO_BIKE', folder_name='microhub_supply')
