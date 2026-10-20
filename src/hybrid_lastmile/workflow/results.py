"""GLIMS-aligned result exports for M6, without fabricating unfinished metrics."""
from __future__ import annotations
import csv
import json
from pathlib import Path


def _csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def export_run_results(run_dir, summary, allocations):
    """Write one summary.csv per run and GLIMS-style assignment/stop tables.

    Distance and duration currently refer ONLY to CC->PUDO supply; they are not
    whole-system metrics. Missing delivery legs are explicitly marked pending.
    """
    run_dir = Path(run_dir)
    supply = summary.get('supply_routing', {})
    bike = summary.get('bike_routing', {})
    modes = ('PUDO', 'CARGO_BIKE', 'VAN')
    rows = []
    for mode in modes:
        metrics = supply if mode == 'PUDO' else bike if mode == 'CARGO_BIKE' else summary.get('van_routing', {})
        rows.append({
            'ciudad': summary['city'], 'model_code': 'M6',
            'delivery_mode': mode,
            'customers': summary['customers_by_mode'].get(mode, 0),
            'paquetes': summary['packages_by_mode'].get(mode, 0),
            'routing_status': metrics.get('status', 'pending'),
            'numero_viajes': metrics.get('route_count'),
            'km_recorridos': metrics.get('total_distance_km'),
            'duration_min': metrics.get('total_duration_min'),
            'routing_algorithm': metrics.get('routing_algorithm'),
        })
    columns = list(rows[0])
    hub_supply = summary.get('microhub_supply_routing', {})
    rows.append({
        'ciudad': summary['city'], 'model_code': 'M6',
        'delivery_mode': 'MICROHUB_SUPPLY',
        'customers': None, 'paquetes': summary.get('microhub_supply', {}).get('packages', 0),
        'routing_status': hub_supply.get('status', 'pending'),
        'numero_viajes': hub_supply.get('route_count'),
        'km_recorridos': hub_supply.get('total_distance_km'),
        'duration_min': hub_supply.get('total_duration_min'),
        'routing_algorithm': hub_supply.get('routing_algorithm'),
    })
    _csv(run_dir / 'summary.csv', rows, columns)

    # Customer assignments are M6's authoritative choice-to-delivery linkage.
    assignment_rows = [{
        'city': summary['city'], 'model': 'M6', 'experiment_id': summary['run_id'],
        'customer_id': a.customer_id, 'packages': a.packages,
        'customer_choice': a.choice, 'delivery_mode': a.delivery_mode,
        'facility_id': a.facility_id, 'network_distance_m': a.network_distance_m,
        'pudo_probability': a.pudo_probability,
    } for a in allocations]
    _csv(run_dir / 'allocations' / 'customer_assignments.csv', assignment_rows,
         ['city','model','experiment_id','customer_id','packages','customer_choice',
          'delivery_mode','facility_id','network_distance_m','pudo_probability'])

    # Each supply operation has its own GLIMS visit-to-route mapping.
    stop_rows = []
    for folder_name, leg, stop_type, prefix in (
        ('supply', 'pudo_supply', 'PUDO', 'PUDO'),
        ('microhub_supply', 'microhub_supply', 'MICROHUB', 'MICROHUB'),
    ):
        visits_path = run_dir / 'routing' / folder_name / 'routed_supply_visits.csv'
        routes_path = run_dir / 'routing' / folder_name / 'routes.csv'
        if not (visits_path.exists() and routes_path.exists()):
            continue
        with visits_path.open(encoding='utf-8-sig', newline='') as handle:
            visits = list(csv.DictReader(handle))
        with routes_path.open(encoding='utf-8-sig', newline='') as handle:
            routes = list(csv.DictReader(handle))
        all_indices = [int(i) for route in routes for i in json.loads(route['stop_indices_json'])]
        if len(all_indices) != len(visits) or sorted(all_indices) != list(range(1, len(visits)+1)):
            raise ValueError(f'GLIMS {leg} routes do not cover each visit exactly once')
        for route in routes:
            for pos, idx in enumerate(json.loads(route['stop_indices_json']), 1):
                v = visits[int(idx)-1]
                stop_rows.append({
                    'city': summary['city'], 'model': 'M6', 'experiment_id': summary['run_id'],
                    'leg': leg, 'route_id': f"M6_{prefix}_{route['route_id']}",
                    'route_number': route['route_id'], 'vehicle_type': 'van',
                    'stop_position': pos, 'stop_type': stop_type, 'stop_id': v['Location'],
                    'latitude': v['Latitude'], 'longitude': v['Longitude'],
                    'package_load': v['Demand'],
                })
    for delivery_leg in ('bike_delivery', 'van_delivery'):
        bike_stops = run_dir / 'routing' / delivery_leg / 'route_stops.csv'
        if not bike_stops.exists():
            continue
        with bike_stops.open(encoding='utf-8-sig', newline='') as handle:
            for item in csv.DictReader(handle):
                stop_rows.append({'city': summary['city'], 'model': 'M6',
                                  'experiment_id': summary['run_id'], **item})
    _csv(run_dir / 'routes' / 'route_stops.csv', stop_rows,
         ['city','model','experiment_id','leg','route_id','route_number','vehicle_type',
          'stop_position','stop_type','stop_id','latitude','longitude','package_load'])
    return rows
