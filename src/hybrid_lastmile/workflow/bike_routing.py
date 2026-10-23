"""Cargo-bike last-mile routes from M6 microhubs, using GLIMS's native router."""
from __future__ import annotations
from dataclasses import asdict, fields
from pathlib import Path
import json
import pandas as pd
from glims.routing.config import RoutingAlgorithmConfig
from glims.routing.osrm_router import CapacityAwareOsrmRouter, expand_facility_supply_visits


def plan_home_delivery(run_dir, city, customers, allocations, capacity, settings, *, depots, mode, transport, folder_name, prefix, vehicle_type):
    folder = Path(run_dir) / 'routing' / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    customer_map = {c.customer_id: c for c in customers}
    hub_map = depots
    groups = {}
    for a in allocations:
        if a.delivery_mode == mode:
            if a.facility_id not in hub_map or a.customer_id not in customer_map:
                raise ValueError(f'Invalid {mode} assignment: {a.customer_id} -> {a.facility_id}')
            groups.setdefault(a.facility_id, []).append(a)
    options = settings.get('routing', {})
    unknown = set(options) - {f.name for f in fields(RoutingAlgorithmConfig)}
    if unknown:
        raise ValueError(f'Unknown GLIMS routing options: {sorted(unknown)}')
    cfg = RoutingAlgorithmConfig(**options)
    router = CapacityAwareOsrmRouter(city)
    route_rows, stop_rows, excluded_rows, totals = [], [], [] , {'route_count': 0, 'total_distance_km': 0.0, 'total_duration_min': 0.0, 'packages': 0.0, 'unroutable_customer_count': 0, 'unroutable_package_count': 0.0, 'route_start_time_per_route_min': None, 'cws_initial_distance_km': 0.0, 'cws_runtime_seconds': 0.0, 'routing_runtime_seconds': 0.0}
    for hub_id, group in sorted(groups.items()):
        hub = hub_map[hub_id]
        original = pd.DataFrame([{'Location': a.customer_id, 'Latitude': customer_map[a.customer_id].latitude,
                                  'Longitude': customer_map[a.customer_id].longitude, 'Demand': float(a.packages)}
                                 for a in sorted(group, key=lambda a: a.customer_id)])
        visits = expand_facility_supply_visits(original, float(capacity))
        if abs(float(visits.Demand.sum()) - float(original.Demand.sum())) > 1e-6:
            raise AssertionError('Bike visit expansion did not conserve parcels')
        clients = visits.rename(columns={'Location': 'customer_id'})
        plan = router.build_capacity_plan(
            depot_latitude=hub.latitude, depot_longitude=hub.longitude,
            clients=clients, transport_mode=transport, vehicle_capacity=float(capacity),
            client_demands=clients['Demand'].to_numpy(),
            max_route_duration_min=float(settings.get('max_route_duration_min', 480)),
            routing_algorithm=cfg.algorithm, cws_allow_route_reversal=cfg.cws_allow_route_reversal,
            ils_max_iterations=cfg.ils_max_iterations,
            ils_max_iterations_without_improvement=cfg.ils_max_iterations_without_improvement,
            ils_destruction_percentage_step=cfg.ils_destruction_percentage_step,
            ils_max_destruction_percentage=cfg.ils_max_destruction_percentage,
            ils_max_full_destruction_attempts=cfg.ils_max_full_destruction_attempts,
            ils_biased_cws_alpha_min=cfg.ils_biased_cws_alpha_min,
            ils_biased_cws_alpha_max=cfg.ils_biased_cws_alpha_max,
            ils_biased_cws_sampling_batch_size=cfg.ils_biased_cws_sampling_batch_size,
            ils_restricted_relocate=cfg.ils_restricted_relocate,
            ils_relocate_candidate_fraction=cfg.ils_relocate_candidate_fraction,
            ils_relocate_neighbor_routes=cfg.ils_relocate_neighbor_routes,
            ils_relocate_max_insertions=cfg.ils_relocate_max_insertions,
            ils_random_seed=cfg.ils_random_seed,
            show_progress=bool(settings.get('show_progress', False)),
            exclude_unroutable_clients=(mode == 'VAN'))
        if mode != 'VAN' and (plan.unroutable_customer_count or plan.unroutable_package_count):
            raise ValueError(f'Unroutable {mode} visits for depot {hub_id}')
        excluded_positions = list(plan.unroutable_client_positions)
        if len(set(excluded_positions)) != len(excluded_positions) or any(
            not 0 <= int(i) < len(visits) for i in excluded_positions
        ):
            raise AssertionError('Invalid GLIMS unroutable visit positions')
        excluded_set = set(excluded_positions)
        routed_visits = visits.iloc[[i for i in range(len(visits)) if i not in excluded_set]].reset_index(drop=True)
        for i in excluded_positions:
            v = visits.iloc[int(i)]
            excluded_rows.append({'leg': folder_name, 'origin_id': hub_id,
                                  'customer_id': str(v.Location), 'latitude': v.Latitude,
                                  'longitude': v.Longitude, 'packages': float(v.Demand),
                                  'supply_visit': int(v.SupplyVisit), 'reason': 'osrm_unroutable'})
        if abs(sum(row['packages'] for row in excluded_rows if row['origin_id'] == hub_id)
               - float(plan.unroutable_package_count)) > 1e-6:
            raise AssertionError('GLIMS excluded package total mismatch')
        seen = [int(idx) for route in plan.routes for idx in route]
        if sorted(seen) != list(range(1, len(routed_visits) + 1)):
            raise AssertionError(f'GLIMS {mode} routes do not cover all visits at {hub_id}')
        for n, (route, km, minutes, load) in enumerate(zip(plan.routes, plan.route_distances_km, plan.route_durations_min, plan.route_loads), 1):
            rid = f'{prefix}_{hub_id}_{n}'
            route_rows.append({'route_id': rid, 'microhub_id': hub_id, 'vehicle_type': vehicle_type, 'distance_km': km,
                               'duration_min': minutes, 'packages': load, 'stop_indices_json': json.dumps(route)})
            for pos, idx in enumerate(route, 1):
                v = routed_visits.iloc[int(idx)-1]
                stop_rows.append({'leg': folder_name, 'route_id': rid, 'route_number': n, 'vehicle_type': vehicle_type,
                                  'origin_id': hub_id, 'stop_position': pos, 'stop_type': 'HOME',
                                  'stop_id': str(v.Location), 'latitude': v.Latitude, 'longitude': v.Longitude,
                                  'package_load': v.Demand, 'supply_visit': int(v.SupplyVisit)})
        if totals['route_start_time_per_route_min'] is None:
            totals['route_start_time_per_route_min'] = float(plan.route_start_time_per_route_min)
        elif totals['route_start_time_per_route_min'] != float(plan.route_start_time_per_route_min):
            raise ValueError('Inconsistent GLIMS route preparation times across depots')
        totals['cws_initial_distance_km'] += float(plan.cws_initial_distance_km)
        totals['cws_runtime_seconds'] += float(plan.cws_runtime_seconds)
        totals['routing_runtime_seconds'] += float(plan.routing_runtime_seconds)
        totals['route_count'] += int(plan.route_count)
        totals['total_distance_km'] += float(plan.total_distance_km)
        totals['total_duration_min'] += float(plan.total_duration_min)
        totals['packages'] += float(routed_visits.Demand.sum())
        totals['unroutable_customer_count'] += int(plan.unroutable_customer_count)
        totals['unroutable_package_count'] += float(plan.unroutable_package_count)
    pd.DataFrame(excluded_rows, columns=['leg','origin_id','customer_id','latitude','longitude','packages','supply_visit','reason']).to_csv(folder/'unroutable_visits.csv', index=False)
    pd.DataFrame(route_rows, columns=['route_id','microhub_id','vehicle_type','distance_km','duration_min','packages','stop_indices_json']).to_csv(folder/'routes.csv', index=False)
    pd.DataFrame(stop_rows, columns=['leg','route_id','route_number','vehicle_type','origin_id','stop_position','stop_type','stop_id','latitude','longitude','package_load','supply_visit']).to_csv(folder/'route_stops.csv', index=False)
    expected = sum(a.packages for a in allocations if a.delivery_mode == mode)
    if abs(totals['packages'] + totals['unroutable_package_count'] - expected) > 1e-6:
        raise AssertionError(f'{mode} routes did not conserve assigned parcels')
    totals['ils_improvement_distance_km'] = max(0.0, totals['cws_initial_distance_km'] - totals['total_distance_km']) if groups else 0.0
    totals['ils_improvement_percent'] = (100.0 * totals['ils_improvement_distance_km'] / totals['cws_initial_distance_km'] if totals['cws_initial_distance_km'] > 0 else 0.0)
    result = {'status': 'planned' if groups else 'not_required', 'routing_algorithm': cfg.algorithm, 'depot_count': len(groups), **totals}
    (folder/'route_summary.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def plan_bike_delivery(run_dir, city, customers, sites, allocations, capacity, settings):
    depots = {s.site_id: s for s in sites['MICROHUB']}
    return plan_home_delivery(run_dir, city, customers, allocations, capacity, settings,
        depots=depots, mode='CARGO_BIKE', transport='cycling',
        folder_name='bike_delivery', prefix='BIKE', vehicle_type='cargo_bike')
