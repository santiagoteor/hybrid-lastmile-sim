"""Warehouse-specific PUDO and microhub replenishment for M6 only."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json

import pandas as pd

from hybrid_lastmile.workflow.supply_routing import plan_facility_supply
from hybrid_lastmile.workflow.supply_accessibility import inaccessible_supply_facilities


def plan_partitioned_supply(output, city, sites, allocations, assignments, warehouses, capacity, settings, *, data_root, facility_type):
    """Route each warehouse's allocated packages from that exact CC using GLIMS."""
    folder_name = 'supply' if facility_type == 'PUDO' else 'microhub_supply'
    mode = 'PUDO' if facility_type == 'PUDO' else 'CARGO_BIKE'
    facility_map = {s.site_id: s for s in sites[facility_type]}
    demand = defaultdict(int)
    for a in allocations:
        if a.delivery_mode != mode:
            continue
        wid = assignments[a.customer_id][0]
        if a.facility_id not in facility_map:
            raise ValueError(f'Unknown {facility_type}: {a.facility_id}')
        demand[(wid, a.facility_id)] += int(a.packages)
    root = Path(output) / 'routing' / folder_name
    summaries = {}
    total_packages = 0.0
    total_routes = 0
    total_distance = 0.0
    total_duration = 0.0
    all_rows = []
    merged_visits = []
    merged_routes = []
    for warehouse in warehouses:
        wid = warehouse['id']
        site_rows = []
        for (origin, facility_id), packages in sorted(demand.items()):
            if origin != wid:
                continue
            site = facility_map[facility_id]
            site_rows.append({'Location': facility_id, 'Latitude': site.latitude,
                              'Longitude': site.longitude, 'Demand': packages})
        # Use the same GLIMS input/output structure in a separate directory per CC.
        subdir = root / 'warehouses' / wid
        subdir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(site_rows, columns=['Location','Latitude','Longitude','Demand']).to_csv(
            subdir / ('pudo_demand.csv' if facility_type == 'PUDO' else 'microhub_demand.csv'), index=False)
        pd.DataFrame([{'Location': wid, 'Latitude': warehouse['latitude'],
                       'Longitude': warehouse['longitude']}]).to_csv(subdir / 'selected_center.csv', index=False)
        # The adapter expects run_dir/routing/{folder_name}. Use a scoped run root
        # rather than changing the GLIMS planner or sharing a single depot file.
        scoped_run = root / '_partition_runs' / wid
        scoped_folder = scoped_run / 'routing' / folder_name
        scoped_folder.mkdir(parents=True, exist_ok=True)
        for name in ('selected_center.csv', 'pudo_demand.csv' if facility_type == 'PUDO' else 'microhub_demand.csv'):
            (scoped_folder / name).write_bytes((subdir / name).read_bytes())
        if site_rows:
            # A disconnected destination must not silently vanish from the route plan.
            inaccessible = inaccessible_supply_facilities(scoped_run, city, folder_name=folder_name)
            if inaccessible:
                raise ValueError(f'Warehouse {wid} has inaccessible {facility_type} facilities: {inaccessible}')
        result = plan_facility_supply(scoped_run, city, capacity, settings,
            data_root=data_root, folder_name=folder_name, facility_label=facility_type)
        for name in ('routed_supply_visits.csv', 'routes.csv', 'route_summary.json'):
            source = scoped_folder / name
            if source.exists():
                (subdir / name).write_bytes(source.read_bytes())
        # Merge route/visit exports for the existing M6 audit and results adapters.
        visit_file = scoped_folder / 'routed_supply_visits.csv'
        route_file = scoped_folder / 'routes.csv'
        local_visits = pd.read_csv(visit_file, dtype={'Location': str}).to_dict('records') if visit_file.exists() else []
        local_routes = pd.read_csv(route_file).to_dict('records') if route_file.exists() else []
        offset = len(merged_visits)
        for visit in local_visits:
            merged_visits.append({**visit, 'warehouse_id': wid})
        for route in local_routes:
            indices = json.loads(route['stop_indices_json'])
            if any(int(idx) < 1 or int(idx) > len(local_visits) for idx in indices):
                raise AssertionError(f'Invalid supply visit index for {wid}')
            merged_routes.append({**route, 'route_id': len(merged_routes) + 1,
                                  'stop_indices_json': json.dumps([int(idx) + offset for idx in indices]),
                                  'origin_id': wid, 'warehouse_id': wid})
        packages = float(result.get('packages', 0))
        expected = sum(r['Demand'] for r in site_rows)
        if abs(packages - expected) > 1e-6:
            raise AssertionError(f'Warehouse {wid}: {packages} routed vs {expected} assigned packages')
        summaries[wid] = result
        total_packages += packages
        total_routes += int(result.get('route_count', 0))
        total_distance += float(result.get('total_distance_km', 0))
        total_duration += float(result.get('total_duration_min', 0))
        all_rows.append({'warehouse_id': wid, 'facility_type': facility_type,
                         'packages': packages, 'route_count': result.get('route_count', 0),
                         'distance_km': result.get('total_distance_km', 0),
                         'duration_min': result.get('total_duration_min', 0)})
    expected_total = sum(a.packages for a in allocations if a.delivery_mode == mode)
    if abs(total_packages - expected_total) > 1e-6:
        raise AssertionError(f'{facility_type} supply not conserved: {total_packages} != {expected_total}')
    pd.DataFrame(merged_visits, columns=['Location','Latitude','Longitude','Demand','SupplyVisit','warehouse_id']).to_csv(
        root / 'routed_supply_visits.csv', index=False)
    pd.DataFrame(merged_routes, columns=['route_id','stop_indices_json','distance_km','duration_min','packages','origin_id','warehouse_id']).to_csv(
        root / 'routes.csv', index=False)
    if abs(sum(float(v['Demand']) for v in merged_visits) - expected_total) > 1e-6:
        raise AssertionError('Merged warehouse supply visits do not conserve demand')
    pd.DataFrame(all_rows).to_csv(root / 'warehouse_route_summary.csv', index=False)
    aggregate = {'status': 'planned' if total_packages else 'no_pudo_demand',
                 'route_count': total_routes, 'packages': total_packages,
                 'total_distance_km': total_distance, 'total_duration_min': total_duration,
                 'warehouse_partition_applied': True,
                 'routing_algorithm': settings.get('routing', {}).get('algorithm', 'cws'),
                 'warehouse_routes': summaries,
                 'warehouse_route_summary_csv': f'routing/{folder_name}/warehouse_route_summary.csv'}
    (root / 'route_summary.json').write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding='utf-8')
    return aggregate
