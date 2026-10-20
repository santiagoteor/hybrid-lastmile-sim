"""Territorial reporting without partitioning demand, facilities, or OSRM routing.

Uses the same neighborhood polygons shipped with the GLIMS input data.
No road-network clipping or neighborhood-based facility exclusion is performed.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path


def _write(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def export_geography(output_dir, city_root, customers, sites, allocations):
    """Attach neighborhood labels to demand, facilities and routed stops.

    All polygons are loaded; only neighborhoods with customer demand appear in
    neighborhood_summary. Facilities outside active neighborhoods remain eligible.
    Traversed neighborhoods are NOT inferred from stops; they need road geometry.
    """
    from shapely.geometry import Point, shape
    from shapely.prepared import prep

    output = Path(output_dir)
    geo = Path(city_root) / 'geography' / 'neighborhoods_limits.geojson'
    if not geo.is_file():
        raise FileNotFoundError(f'Neighborhood polygons missing: {geo}')
    features = json.loads(geo.read_text(encoding='utf-8'))['features']
    polygons = []
    for feature in features:
        properties = feature.get('properties', {})
        name = next((str(properties[k]) for k in ('neighborhood','barri','name','nom','NOM') if properties.get(k)), None)
        if not name:
            raise ValueError(f'Neighborhood feature missing name in {geo}')
        geometry = shape(feature['geometry'])
        polygons.append((name, geometry, prep(geometry)))

    def neighborhood(lat, lon):
        point = Point(float(lon), float(lat))
        for name, geometry, prepared in polygons:
            if prepared.intersects(point):
                return name
        return ''

    root = output / 'geography'
    customer_zone = {c.customer_id: neighborhood(c.latitude, c.longitude) for c in customers}
    customer_rows = [dict(customer_id=c.customer_id, neighborhood=customer_zone[c.customer_id], packages=c.packages,
                          latitude=c.latitude, longitude=c.longitude) for c in customers]
    _write(root/'customer_neighborhoods.csv', customer_rows,
           ['customer_id','neighborhood','packages','latitude','longitude'])
    facility_zone = {}
    facility_rows = []
    for kind, group in sites.items():
        for site in group:
            zone = neighborhood(site.latitude, site.longitude)
            facility_zone[site.site_id] = zone
            facility_rows.append(dict(facility_id=site.site_id, facility_type=kind, neighborhood=zone,
                                      latitude=site.latitude, longitude=site.longitude))
    _write(root/'facility_neighborhoods.csv', facility_rows,
           ['facility_id','facility_type','neighborhood','latitude','longitude'])

    stats = defaultdict(lambda: dict(customers=0, packages=0, pudo_customers=0, pudo_packages=0,
                                     bike_customers=0, bike_packages=0, van_customers=0, van_packages=0))
    flows = defaultdict(lambda: dict(customers=0, packages=0))
    for a in allocations:
        zone = customer_zone[a.customer_id]
        row = stats[zone]
        row['customers'] += 1
        row['packages'] += int(a.packages)
        mode = {'PUDO':'pudo','CARGO_BIKE':'bike','VAN':'van'}[a.delivery_mode]
        row[f'{mode}_customers'] += 1
        row[f'{mode}_packages'] += int(a.packages)
        if a.facility_id:
            key = (zone, facility_zone.get(a.facility_id, ''), a.delivery_mode, a.facility_id)
            flows[key]['customers'] += 1
            flows[key]['packages'] += int(a.packages)
    summary_rows = [dict(neighborhood=k, **v) for k,v in sorted(stats.items())]
    _write(root/'neighborhood_summary.csv', summary_rows,
           ['neighborhood','customers','packages','pudo_customers','pudo_packages',
            'bike_customers','bike_packages','van_customers','van_packages'])
    flow_rows = [dict(customer_neighborhood=k[0], facility_neighborhood=k[1],
                      delivery_mode=k[2], facility_id=k[3], **v) for k,v in sorted(flows.items())]
    _write(root/'neighborhood_flows.csv', flow_rows,
           ['customer_neighborhood','facility_neighborhood','delivery_mode','facility_id','customers','packages'])

    # Annotate GLIMS-style route stops and route summaries without changing their columns.
    stop_path = output/'routes'/'route_stops.csv'
    route_path = output/'routes'/'m6_routes.csv'
    route_zones = defaultdict(set)
    stops = []
    if stop_path.is_file():
        with stop_path.open(encoding='utf-8-sig', newline='') as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames
            for row in reader:
                if row.get('latitude') and row.get('longitude'):
                    zone = neighborhood(row['latitude'], row['longitude'])
                else:
                    zone = ''
                row['neighborhood'] = zone
                if zone:
                    route_zones[row['route_id']].add(zone)
                stops.append(row)
        _write(stop_path, stops, fields)
    route_rows = []
    if route_path.is_file():
        with route_path.open(encoding='utf-8-sig', newline='') as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames
            for row in reader:
                zones = sorted(route_zones.get(row['route_id'], set()))
                row['neighborhood'] = zones[0] if len(zones)==1 else ('MULTIPLE' if zones else '')
                route_rows.append(row)
        _write(route_path, route_rows, fields)
    route_neighborhood_rows = [dict(route_id=r['route_id'], leg=r.get('leg',''),
                                    served_neighborhoods=json.dumps(sorted(route_zones.get(r['route_id'],set())),ensure_ascii=False),
                                    served_neighborhood_count=len(route_zones.get(r['route_id'],set())),
                                    traversed_neighborhoods='', traversal_status='not_available_without_route_geometry')
                               for r in route_rows]
    _write(root/'route_neighborhoods.csv', route_neighborhood_rows,
           ['route_id','leg','served_neighborhoods','served_neighborhood_count',
            'traversed_neighborhoods','traversal_status'])
    if len(customer_rows) != sum(r['customers'] for r in summary_rows):
        raise AssertionError('Geographic customer count mismatch')
    if sum(c.packages for c in customers) != sum(r['packages'] for r in summary_rows):
        raise AssertionError('Geographic package count mismatch')
    return dict(status='complete', polygons=len(polygons), active_neighborhoods=sum(bool(r['neighborhood']) for r in summary_rows),
                unmatched_customers=sum(not r['neighborhood'] for r in customer_rows),
                route_count=len(route_rows), traversal_status='not_available_without_route_geometry')
