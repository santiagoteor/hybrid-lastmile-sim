"""GLIMS-compatible route exports and auditable conservation checks for M6.

Schemas mirror the supplied GLIMS example. Fields unavailable in M6 remain blank.
This module does not invent stop arrival timestamps, geometry, or traffic metrics.
"""
from __future__ import annotations
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

SCHEMAS = {'routes/m5_routes.csv': ['city', 'neighborhood', 'model', 'leg', 'route_id', 'vehicle_type', 'depot', 'route_number', 'stop_count', 'package_load', 'vehicle_capacity', 'distance_km', 'network_distance_km', 'last_meter_access_distance_km', 'system_distance_km', 'duration_min', 'start_handling_min', 'base_stop_service_min', 'last_meter_access_time_min', 'stop_service_min', 'routing_algorithm', 'cws_allow_route_reversal', 'traffic_profile', 'traffic_duration_multiplier', 'traffic_source', 'time_traffic_profile', 'simulation_date', 'shift_start_time', 'shift_end_time', 'route_start_datetime', 'route_end_datetime', 'base_travel_time_min', 'time_dependent_travel_time_min', 'traffic_delay_min', 'shift_feasible', 'stop_sequence', 'geometry_wkt', 'supply_vehicle_type', 'network_duration_min', 'experiment_id', 'selected_traffic_profile'], 'routes/route_stops.csv': ['city', 'neighborhood', 'model', 'leg', 'route_id', 'route_number', 'vehicle_type', 'supply_vehicle_type', 'depot', 'stop_position', 'stop_type', 'stop_id', 'latitude', 'longitude', 'package_load', 'snap_distance_m', 'last_meter_access_distance_m', 'last_meter_access_time_min', 'base_service_time_min', 'effective_service_time_min', 'arrival_datetime', 'service_start_datetime', 'service_end_datetime', 'experiment_id'], 'audit/routing_integrity_summary.csv': ['city', 'neighborhood', 'model', 'leg', 'input_customers', 'excluded_unroutable_customers', 'expected_routable_customers', 'assigned_unique_customers', 'assigned_customer_occurrences', 'unassigned_customers', 'duplicated_customers', 'unexpected_customer_ids', 'input_packages', 'excluded_unroutable_packages', 'expected_routable_packages', 'assigned_package_occurrences', 'customer_balance_ok', 'package_balance_ok', 'audit_status', 'missing_customer_ids', 'duplicated_customer_ids', 'unexpected_customer_id_values', 'experiment_id', 'routing_algorithm'], 'audit/route_customer_summary.csv': ['city', 'neighborhood', 'model', 'leg', 'route_id', 'route_number', 'customer_count', 'unique_customer_count', 'duplicate_customer_occurrences', 'package_count', 'experiment_id', 'routing_algorithm'], 'audit/unroutable_customers.csv': ['city', 'model', 'leg', 'experiment_id', 'routing_algorithm', 'customer_id', 'latitude', 'longitude', 'packages', 'reason', 'origin_id', 'supply_visit']}
OPERATIONS = (
    ('supply', 'pudo_supply', 'conventional_van', 'PUDO'),
    ('microhub_supply', 'microhub_supply', 'conventional_van', 'MICROHUB'),
    ('bike_delivery', 'bike_delivery', 'cargo_bike', 'HOME'),
    ('van_delivery', 'van_delivery', 'conventional_van', 'HOME'),
)


def _read(path):
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8-sig') as stream:
        return list(csv.DictReader(stream))


def _write(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def _number(value):
    return float(value or 0)


def export_route_audit(run_dir, summary, allocations, capacities):
    run_dir = Path(run_dir)
    base = dict(city=summary['city'], neighborhood='', model='M6', experiment_id=summary['run_id'])
    route_rows, stop_rows, audit_rows, per_route, unroutable = [], [], [], [], []
    for folder, leg, vehicle, stop_type in OPERATIONS:
        source = run_dir / 'routing' / folder
        routes = _read(source / 'routes.csv')
        visits = _read(source / 'routed_supply_visits.csv') if stop_type != 'HOME' else []
        home_stops = _read(source / 'route_stops.csv') if stop_type == 'HOME' else []
        metadata_path = source / 'route_summary.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else {}
        if not routes and metadata.get('status') == 'planned':
            raise ValueError(f'{leg}: marked planned but has no routes')
        expected_mode = {'pudo_supply':'PUDO','microhub_supply':'CARGO_BIKE',
                         'bike_delivery':'CARGO_BIKE','van_delivery':'VAN'}[leg]
        selected = [a for a in allocations if a.delivery_mode == expected_mode]
        expected_packages = sum(float(a.packages) for a in selected)
        # Supply visits are facility visits, not individual customers. Audit package conservation.
        customer_leg = stop_type == 'HOME'
        expected_customers = len(selected) if customer_leg else 0
        expected_ids = {str(a.customer_id) for a in selected} if customer_leg else set()
        excluded_visits = _read(source / 'unroutable_visits.csv') if customer_leg else []
        excluded_ids = {str(v['customer_id']) for v in excluded_visits}
        if excluded_visits and leg != 'van_delivery':
            raise ValueError(f'{leg}: unexpected exclusions outside VAN')
        if len(excluded_ids) != len(excluded_visits):
            raise ValueError(f'{leg}: duplicate excluded customer visits; review split visits')
        excluded_packages = sum(_number(v['packages']) for v in excluded_visits)
        if excluded_ids - expected_ids:
            raise ValueError(f'{leg}: exclusions not present in assigned demand')
        for v in excluded_visits:
            unroutable.append(dict(**base, leg=leg, routing_algorithm=metadata.get('routing_algorithm'),
                customer_id=v['customer_id'], latitude=v['latitude'], longitude=v['longitude'],
                packages=v['packages'], reason=v['reason'], origin_id=v['origin_id'],
                supply_visit=v['supply_visit']))
        stop_by_route = defaultdict(list)
        if customer_leg:
            for item in home_stops:
                stop_by_route[str(item['route_id'])].append(item)
        else:
            indices = []
            for route in routes:
                indices.extend(json.loads(route['stop_indices_json']))
            if sorted(int(x) for x in indices) != list(range(1,len(visits)+1)):
                raise ValueError(f'{leg}: supply visits are missing or duplicated')
            for route in routes:
                for position, idx in enumerate(json.loads(route['stop_indices_json']), 1):
                    v = visits[int(idx)-1]
                    stop_by_route[str(route['route_id'])].append(dict(
                        stop_position=position, stop_id=v['Location'], latitude=v['Latitude'],
                        longitude=v['Longitude'], package_load=v['Demand'], stop_type=stop_type))
        seen = Counter()
        assigned_packages = 0.0
        for route in routes:
            raw_id = str(route['route_id'])
            route_id = f'M6_{leg}_{raw_id}'
            stops = sorted(stop_by_route.get(raw_id, []), key=lambda s:int(s['stop_position']))
            load = _number(route.get('packages'))
            cap = float(capacities['cargo_bike_capacity'] if vehicle == 'cargo_bike' else capacities['van_capacity'])
            if load > cap + 1e-6:
                raise ValueError(f'{leg}: route {raw_id} exceeds capacity {cap} ({load})')
            stop_load = sum(_number(s.get('package_load')) for s in stops)
            if abs(stop_load-load)>1e-6:
                raise ValueError(f'{leg}: route {raw_id} package load differs from stops')
            assigned_packages += load
            depot = route.get('microhub_id') or route.get('origin_id') or (stops[0].get('origin_id') if stops else '')
            if not depot:
                center = _read(source/'selected_center.csv')
                if center:
                    depot = next((str(center[0][k]) for k in ('Location','site_id','center_id','Name') if center[0].get(k)), '')
            route_rows.append(dict(**base, leg=leg, route_id=route_id, route_number=raw_id,
                vehicle_type=vehicle, depot=depot, stop_count=len(stops), package_load=load,
                vehicle_capacity=cap, distance_km=route.get('distance_km'),
                network_distance_km=route.get('distance_km'),
                system_distance_km=route.get('distance_km'), duration_min=route.get('duration_min'),
                routing_algorithm=metadata.get('routing_algorithm'),
                traffic_profile=metadata.get('traffic_profile'),
                stop_sequence=json.dumps([s.get('stop_id') for s in stops])))
            ids=[]
            for stop in stops:
                sid = str(stop.get('stop_id',''))
                if customer_leg:
                    seen[sid]+=1
                    ids.append(sid)
                stop_rows.append(dict(**base, leg=leg, route_id=route_id, route_number=raw_id,
                    vehicle_type=vehicle, depot=depot, stop_position=stop.get('stop_position'),
                    stop_type='customer' if customer_leg else 'facility', stop_id=sid,
                    latitude=stop.get('latitude'), longitude=stop.get('longitude'),
                    package_load=stop.get('package_load')))
            if customer_leg:
                per_route.append(dict(**base, leg=leg, route_id=route_id, route_number=raw_id,
                    customer_count=len(ids), unique_customer_count=len(set(ids)),
                    duplicate_customer_occurrences=len(ids)-len(set(ids)), package_count=load,
                    routing_algorithm=metadata.get('routing_algorithm')))
        missing = sorted(expected_ids - set(seen) - excluded_ids)
        unexpected = sorted(set(seen) - expected_ids)
        duplicates = sorted(k for k,v in seen.items() if v>1)
        package_ok = abs(assigned_packages + excluded_packages - expected_packages)<1e-6
        customer_ok = (not missing and not unexpected and not duplicates and not (excluded_ids & set(seen))) if customer_leg else True
        status = 'OK' if customer_ok and package_ok else 'FAIL'
        audit_rows.append(dict(**base, leg=leg, input_customers=expected_customers,
            excluded_unroutable_customers=len(excluded_ids), expected_routable_customers=expected_customers-len(excluded_ids),
            assigned_unique_customers=len(seen) if customer_leg else 0,
            assigned_customer_occurrences=sum(seen.values()) if customer_leg else 0,
            unassigned_customers=len(missing), duplicated_customers=len(duplicates),
            unexpected_customer_ids=len(unexpected), input_packages=expected_packages,
            excluded_unroutable_packages=excluded_packages, expected_routable_packages=expected_packages-excluded_packages,
            assigned_package_occurrences=assigned_packages, customer_balance_ok=customer_ok,
            package_balance_ok=package_ok, audit_status=status,
            missing_customer_ids=';'.join(missing), duplicated_customer_ids=';'.join(duplicates),
            unexpected_customer_id_values=';'.join(unexpected),
            routing_algorithm=metadata.get('routing_algorithm')))
        if status != 'OK':
            raise ValueError(f'{leg}: route integrity audit failed: missing={missing}, duplicates={duplicates}, unexpected={unexpected}, packages={assigned_packages}/{expected_packages}')
    _write(run_dir/'routes'/'m6_routes.csv',route_rows,SCHEMAS['routes/m5_routes.csv'])
    _write(run_dir/'routes'/'route_stops.csv',stop_rows,SCHEMAS['routes/route_stops.csv'])
    _write(run_dir/'audit'/'routing_integrity_summary.csv',audit_rows,SCHEMAS['audit/routing_integrity_summary.csv'])
    _write(run_dir/'audit'/'route_customer_summary.csv',per_route,SCHEMAS['audit/route_customer_summary.csv'])
    _write(run_dir/'audit'/'unroutable_customers.csv',unroutable,SCHEMAS['audit/unroutable_customers.csv'])
    return {'route_count':len(route_rows),'stop_count':len(stop_rows),'audit_status':'OK'}
