#!/usr/bin/env python3
"""Export one representative route per M6 leg as GeoJSON for QGIS. Standard library only."""
import argparse,csv,io,json,zipfile,collections,pathlib,statistics

def main():
 p=argparse.ArgumentParser();p.add_argument('result_zip');p.add_argument('--out',default='qgis_audit');a=p.parse_args()
 out=pathlib.Path(a.out);out.mkdir(parents=True,exist_ok=True)
 z=zipfile.ZipFile(a.result_zip)
 def rows(suffix):
  names=[n for n in z.namelist() if n.endswith('/'+suffix)]
  if not names:return []
  return list(csv.DictReader(io.StringIO(z.read(names[0]).decode('utf-8-sig'))))
 def point(lat,lon):
  try:
   x,y=float(lon),float(lat)
   if -180<=x<=180 and -90<=y<=90:return [x,y]
  except (TypeError,ValueError):pass
 def feature(geom,props):return {'type':'Feature','geometry':geom,'properties':props}
 def geom(t,coords):return {'type':t,'coordinates':coords}
 def save(leg,layer,fs):
  path=out/f'{leg}_{layer}.geojson';path.write_text(json.dumps({'type':'FeatureCollection','features':fs},ensure_ascii=False),encoding='utf8')
 customers={r['customer_id']:r for r in rows('geography/customer_neighborhoods.csv')}
 assignments=rows('allocations/customer_assignments.csv')
 by_facility=collections.defaultdict(list)
 for r in assignments:
  if r.get('facility_id'):by_facility[r['facility_id']].append(r)
 facilities={r['facility_id']:r for r in rows('geography/facility_neighborhoods.csv')}
 route_rows=rows('routes/m6_routes.csv');stops=rows('routes/route_stops.csv')
 by_route=collections.defaultdict(list)
 for r in stops:by_route[r['route_id']].append(r)
 # Full per-route duration audit: no OSRM calls and no dense matrices.
 audit_fields=['leg','route_id','vehicle_type','stop_count','package_load','vehicle_capacity','load_ratio','distance_km','duration_min','network_duration_min','route_start_datetime','route_end_datetime','has_schedule']
 with (out/'route_duration_audit.csv').open('w',newline='',encoding='utf-8') as stream:
  writer=csv.DictWriter(stream,fieldnames=audit_fields);writer.writeheader()
  for route in route_rows:
   try: ratio=float(route['package_load'])/float(route['vehicle_capacity']) if float(route['vehicle_capacity'])>0 else ''
   except (ValueError,TypeError,ZeroDivisionError):ratio=''
   writer.writerow({**{k:route.get(k,'') for k in audit_fields},'load_ratio':ratio,'has_schedule':bool(route.get('route_start_datetime') and route.get('route_end_datetime'))})
 summary=[]
 for leg in ['pudo_supply','microhub_supply','bike_delivery','van_delivery']:
  eligible=[]
  for r in route_rows:
   if r.get('leg')!=leg:continue
   ss=sorted(by_route.get(r['route_id'],[]),key=lambda v:int(float(v.get('stop_position') or 0)))
   valid=[s for s in ss if point(s.get('latitude'),s.get('longitude'))]
   if valid:eligible.append((r,valid))
  if not eligible:continue
  eligible.sort(key=lambda t:(len(t[1]),t[0]['route_id']))
  r,ss=eligible[len(eligible)//2] # typical rather than extreme route
  depot=facilities.get(r.get('depot',''),{})
  dep=point(depot.get('latitude'),depot.get('longitude'))
  pts=[point(s['latitude'],s['longitude']) for s in ss]
  coords=([dep] if dep else [])+pts+([dep] if dep else [])
  base={'leg':leg,'route_id':r['route_id'],'duration_min':r.get('duration_min'),'distance_km':r.get('distance_km'),'package_load':r.get('package_load'),'vehicle_capacity':r.get('vehicle_capacity'),'geometry_note':'Straight-line stop sequence; NOT a road-network route'}
  save(leg,'route_reference',[feature(geom('LineString',coords),base)] if len(coords)>=2 else [])
  fs=[]
  if dep:fs.append(feature(geom('Point',dep),{'role':'depot','stop_id':r.get('depot'),'order':0}))
  for i,s in enumerate(ss,1):fs.append(feature(geom('Point',pts[i-1]),{'role':'stop','order':i,'stop_id':s.get('stop_id'),'stop_type':s.get('stop_type'),'packages':s.get('package_load')}))
  save(leg,'stops',fs)
  # For supply, show demand assigned to visited facilities. For home delivery, stops are demand points.
  selected=[]
  if leg in ('pudo_supply','microhub_supply'):
   visited={s.get('stop_id') for s in ss}
   for fid in visited:selected.extend(by_facility.get(fid,[]))
  else:
   ids={s.get('stop_id') for s in ss}
   selected=[{'customer_id':cid,'facility_id':''} for cid in ids]
  demand=[];missing=0
  for item in selected:
   c=customers.get(item['customer_id']); xy=point(c.get('latitude'),c.get('longitude')) if c else None
   if not xy:missing+=1;continue
   demand.append(feature(geom('Point',xy),{'customer_id':item['customer_id'],'facility_id':item.get('facility_id'),'packages':c.get('packages'),'neighborhood':c.get('neighborhood')}))
  save(leg,'demand_points',demand)
  summary.append({'leg':leg,'route_id':r['route_id'],'stops':len(ss),'demand_points':len(demand),'missing_demand_coordinates':missing,'duration_min':r.get('duration_min'),'distance_km':r.get('distance_km'),'geometry':'straight_line_reference_only'})
 (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf8')
 print('Exported',len(summary),'sample routes to',out)
 for x in summary:print(x)
if __name__=='__main__':main()
