"""Deterministic territorial warehouse assignment (M6 only; no OSRM calls)."""
from __future__ import annotations

import csv
from collections import defaultdict
from math import asin, cos, radians, sin, sqrt
from pathlib import Path


def haversine_m(lat1, lon1, lat2, lon2):
    a1, a2 = radians(lat1), radians(lat2)
    dlat, dlon = a2 - a1, radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(a1) * cos(a2) * sin(dlon / 2) ** 2
    return 12742000 * asin(min(1.0, sqrt(a)))


def resolve_warehouses(warehouse_ids, sites):
    """Resolve selected CC IDs against authoritative infrastructure coordinates."""
    if not isinstance(warehouse_ids, list) or len(warehouse_ids) != 3:
        raise ValueError("warehouses must contain exactly three CC IDs")
    ids = [str(item).strip() for item in warehouse_ids]
    if any(not item for item in ids) or len(set(ids)) != 3:
        raise ValueError("Warehouse IDs must be three unique nonempty values")
    available = {s.site_id: s for s in sites.get("CC", [])}
    missing = sorted(set(ids) - set(available))
    if missing:
        raise ValueError(f"Selected warehouses are not CC in the infrastructure: {missing}")
    return [{"id": wid, "latitude": available[wid].latitude,
             "longitude": available[wid].longitude} for wid in ids]


def assign_warehouses(customers, warehouses):
    """Return per-customer assignments and load; never imposes balanced quotas."""
    if not isinstance(warehouses, list) or len(warehouses) != 3:
        raise ValueError('warehouses must contain exactly three entries')
    normalized = []
    for item in warehouses:
        wid = str(item['id']).strip()
        lat, lon = float(item['latitude']), float(item['longitude'])
        if not wid or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f'Invalid warehouse: {item}')
        normalized.append((wid, lat, lon))
    if len({w[0] for w in normalized}) != 3:
        raise ValueError('Warehouse identifiers must be unique')
    normalized.sort()  # deterministic tie breaking
    assigned = {}
    loads = defaultdict(lambda: {'customers': 0, 'packages': 0})
    for customer in customers:
        if customer.customer_id in assigned:
            raise ValueError(f'Duplicate customer: {customer.customer_id}')
        distance, wid = min((haversine_m(customer.latitude, customer.longitude, lat, lon), wid)
                            for wid, lat, lon in normalized)
        assigned[customer.customer_id] = (wid, distance)
        loads[wid]['customers'] += 1
        loads[wid]['packages'] += customer.packages
    total_customers = len(customers)
    total_packages = sum(c.packages for c in customers)
    rows = []
    for wid, lat, lon in normalized:
        load = loads[wid]
        rows.append({'warehouse_id': wid, 'latitude': lat, 'longitude': lon,
                     'customers': load['customers'], 'customer_percentage': round(100 * load['customers'] / total_customers, 6) if total_customers else 0,
                     'packages': load['packages'], 'package_percentage': round(100 * load['packages'] / total_packages, 6) if total_packages else 0})
    assert sum(r['customers'] for r in rows) == total_customers
    assert sum(r['packages'] for r in rows) == total_packages
    return assigned, rows


def export_warehouse_assignments(output, customers, warehouses):
    assignments, loads = assign_warehouses(customers, warehouses)
    folder = Path(output) / 'warehouses'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'customer_warehouse_assignments.csv').open('w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=['customer_id', 'warehouse_id', 'distance_m', 'packages'])
        writer.writeheader()
        for customer in customers:
            wid, distance = assignments[customer.customer_id]
            writer.writerow({'customer_id': customer.customer_id, 'warehouse_id': wid,
                             'distance_m': round(distance, 3), 'packages': customer.packages})
    with (folder / 'warehouse_load.csv').open('w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(loads[0]))
        writer.writeheader()
        writer.writerows(loads)
    top_customers = min(loads, key=lambda r: (-r['customers'], r['warehouse_id']))['warehouse_id']
    top_packages = min(loads, key=lambda r: (-r['packages'], r['warehouse_id']))['warehouse_id']
    return assignments, {'method': 'nearest_haversine', 'loads': loads,
                         'most_customers_warehouse': top_customers,
                         'most_packages_warehouse': top_packages,
                         'routing_partition_applied': True}
