"""Prepare route-ready stop manifests from M6 assignments; no routing claims."""
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path


def export_operational_inputs(output_dir, customers, sites, allocations, capacities):
    """Write stop manifests and capacity lower bounds, preserving all package counts.

    These manifests are *inputs* to GLIMS routing, not optimized routes.
    """
    root = Path(output_dir) / 'routing'
    root.mkdir(parents=True, exist_ok=True)
    customer_by_id = {c.customer_id: c for c in customers}
    facility_by_id = {s.site_id: s for group in sites.values() for s in group}
    if len(customer_by_id) != len(customers):
        raise ValueError('Duplicate customer IDs')
    records = []
    grouped = defaultdict(lambda: {'stops': 0, 'packages': 0})
    for a in allocations:
        c = customer_by_id[a.customer_id]
        if a.delivery_mode == 'CARGO_BIKE':
            if not a.facility_id or a.facility_id not in facility_by_id:
                raise ValueError(f'Missing microhub for {a.customer_id}')
            origin = a.facility_id
            destination_id = a.customer_id
            latitude, longitude = c.latitude, c.longitude
            operation = 'MICROHUB_TO_HOME'
        elif a.delivery_mode == 'VAN':
            origin = 'UNASSIGNED_CC'
            destination_id = a.customer_id
            latitude, longitude = c.latitude, c.longitude
            operation = 'CC_TO_HOME'
        elif a.delivery_mode == 'PUDO':
            if not a.facility_id or a.facility_id not in facility_by_id:
                raise ValueError(f'Missing PUDO for {a.customer_id}')
            origin = 'UNASSIGNED_CC'
            destination_id = a.facility_id
            f = facility_by_id[destination_id]
            latitude, longitude = f.latitude, f.longitude
            operation = 'CC_TO_PUDO'
        else:
            raise ValueError(f'Unknown mode {a.delivery_mode}')
        records.append({'operation': operation, 'origin_id': origin, 'destination_id': destination_id,
                        'customer_id': a.customer_id, 'latitude': latitude, 'longitude': longitude,
                        'packages': a.packages})
        grouped[(operation, origin)]['packages'] += a.packages
        grouped[(operation, origin)]['stops'] += 1
    with (root / 'stop_manifest.csv').open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=['operation','origin_id','destination_id','customer_id','latitude','longitude','packages'])
        writer.writeheader()
        writer.writerows(records)
    from math import ceil
    bounds = []
    for (operation, origin), value in sorted(grouped.items()):
        capacity_key = 'cargo_bike_capacity' if operation == 'MICROHUB_TO_HOME' else 'van_capacity'
        cap = int(capacities[capacity_key])
        bounds.append({'operation': operation, 'origin_id': origin, 'stops': value['stops'],
                       'packages': value['packages'], 'vehicle_capacity': cap,
                       'minimum_capacity_loads': ceil(value['packages'] / cap)})
    with (root / 'capacity_lower_bounds.csv').open('w', encoding='utf-8', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=['operation','origin_id','stops','packages','vehicle_capacity','minimum_capacity_loads'])
        writer.writeheader()
        writer.writerows(bounds)
    return {'stops': len(records), 'packages': sum(r['packages'] for r in records), 'groups': len(bounds)}
