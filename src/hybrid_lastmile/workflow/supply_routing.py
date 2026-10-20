"""Thin adapter to GLIMS's actual facility-supply route planner (no custom solver)."""
from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

import pandas as pd
from glims.routing.config import RoutingAlgorithmConfig
from glims.routing.osrm_router import calculate_facility_supply_route
from glims.simulation.traffic import TrafficProfile, load_traffic_profile


def plan_facility_supply(run_dir, city, van_capacity, settings, *, data_root, folder_name, facility_label):
    """Plan CC -> PUDO -> CC via GLIMS; writes auditable routes and visits.

    Requires supply center selection. No routes are silently invented.
    """
    run_dir = Path(run_dir)
    folder = run_dir / 'routing' / folder_name
    center_file = folder / 'selected_center.csv'
    demand_file = folder / ('pudo_demand.csv' if folder_name == 'supply' else 'microhub_demand.csv')
    if not demand_file.exists():
        raise FileNotFoundError(f'Missing facility supply demand: {demand_file}')
    used = pd.read_csv(demand_file, dtype={'Location': str})
    if used.empty:
        result = {'status': 'no_pudo_demand', 'route_count': 0, 'packages': 0}
        (folder / 'route_summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result
    if not center_file.exists():
        raise ValueError('Facility supply routing requires a selected center')
    selected = pd.read_csv(center_file, dtype={'Location': str}).iloc[0]
    routing_options = settings.get('routing', {})
    unknown = set(routing_options) - {f.name for f in fields(RoutingAlgorithmConfig)}
    if unknown:
        raise ValueError(f'Unknown GLIMS routing options: {sorted(unknown)}')
    routing = RoutingAlgorithmConfig(**routing_options)
    traffic_options = settings.get('traffic', {})
    if 'csv_path' in traffic_options:
        csv_path = Path(traffic_options['csv_path'])
        if not csv_path.is_absolute():
            csv_path = Path(data_root) / csv_path
        traffic = load_traffic_profile(
            csv_path=csv_path,
            profile_name=traffic_options.get('profile', 'baseline'),
            city=city,
            multiplier_override=traffic_options.get('multiplier_override'),
        )
    else:
        traffic = TrafficProfile(name='baseline', duration_multiplier=1.0, source='baseline_osrm')
    plan, visits, availability = calculate_facility_supply_route(
        city=city,
        selected_cc=selected,
        used_facilities=used,
        truck_capacity=float(van_capacity),
        facility_label=facility_label,
        max_route_duration_min=float(settings.get('max_route_duration_min', 480)),
        routing_config=routing,
        traffic_profile=traffic,
        show_progress=bool(settings.get('show_progress', False)),
    )
    if abs(float(visits['Demand'].sum()) - float(used['Demand'].sum())) > 1e-6:
        raise AssertionError('GLIMS routes input did not conserve facility demand')
    visits.to_csv(folder / 'routed_supply_visits.csv', index=False)
    rows = []
    for index, (stops, distance, duration, load) in enumerate(zip(
        plan.routes, plan.route_distances_km, plan.route_durations_min, plan.route_loads
    ), start=1):
        rows.append({'route_id': index, 'stop_indices_json': json.dumps(stops),
                     'distance_km': distance, 'duration_min': duration, 'packages': load})
    pd.DataFrame(rows, columns=['route_id','stop_indices_json','distance_km','duration_min','packages']).to_csv(
        folder / 'routes.csv', index=False)
    result = {'status': 'planned', 'route_count': plan.route_count,
              'total_distance_km': plan.total_distance_km,
              'total_duration_min': plan.total_duration_min,
              'packages': float(visits['Demand'].sum()),
              'unroutable_customer_count': plan.unroutable_customer_count,
              'unroutable_package_count': plan.unroutable_package_count,
              'routing_algorithm': plan.routing_algorithm,
              'traffic_profile': plan.traffic_profile,
              'facility_available_at': {key: value.isoformat() for key, value in availability.items()},
              'glims_route_plan': asdict(plan)}
    (folder / 'route_summary.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    return {key: value for key, value in result.items() if key != 'glims_route_plan'}


def plan_pudo_supply(run_dir, city, van_capacity, settings, *, data_root):
    return plan_facility_supply(run_dir, city, van_capacity, settings,
        data_root=data_root, folder_name='supply', facility_label='PUDO')


def plan_microhub_supply(run_dir, city, van_capacity, settings, *, data_root):
    return plan_facility_supply(run_dir, city, van_capacity, settings,
        data_root=data_root, folder_name='microhub_supply', facility_label='MICROHUB')
