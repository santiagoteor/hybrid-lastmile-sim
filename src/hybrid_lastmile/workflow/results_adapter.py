"""Economic and environmental adapter: reuse GLIMS' authoritative formulas.

No missing emissions factor or route-start duration is silently treated as zero.
"""
from __future__ import annotations
import csv
from pathlib import Path
import pandas as pd
from glims.common.cost_utils import (calculate_direct_route_operating_cost,
                                     calculate_additional_costs)
from glims.simulation.exporters import build_routing_plan_metrics_frame

OPERATIONS = (
    ('PUDO_SUPPLY', 'supply_routing', 'conventional_van', 'FURGONETA_CONV'),
    ('MICROHUB_SUPPLY', 'microhub_supply_routing', 'conventional_van', 'FURGONETA_CONV'),
    ('CARGO_BIKE', 'bike_routing', 'cargo_bike', 'BICICLETA_CARGO'),
    ('VAN', 'van_routing', 'conventional_van', 'FURGONETA_CONV'),
)

def _write(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding='utf-8-sig')

def export_glims_metrics(run_dir, summary, allocations, parameters):
    """Export per-leg cost audit and GLIMS plan metrics; return system totals.

    Preparation duration is taken from GLIMS route metadata when present; absent
    duration makes labor cost unknown (not zero). Emissions use GLIMS model factors.
    """
    run_dir = Path(run_dir)
    model_table = pd.read_csv(parameters.model_file, encoding='utf-8-sig').set_index('modelo')
    costs = parameters.cost_catalogue
    rows, plan_frames = [], {}
    total_cost = 0.0
    total_co2 = 0.0
    total_nox = 0.0
    complete_cost = complete_co2 = complete_nox = True
    for leg, key, vehicle, model_name in OPERATIONS:
        metrics = summary.get(key, {})
        routes = int(metrics.get('route_count') or 0)
        distance = float(metrics.get('total_distance_km') or 0)
        duration = float(metrics.get('total_duration_min') or 0)
        packages = (summary['packages_by_mode'].get('PUDO', 0) if leg == 'PUDO_SUPPLY'
                    else summary['packages_by_mode'].get('CARGO_BIKE', 0) if leg == 'MICROHUB_SUPPLY'
                    else summary['packages_by_mode'].get(leg, 0))
        if leg == 'VAN':
            packages -= float(metrics.get('unroutable_package_count', 0))
        if routes == 0 and packages:
            raise ValueError(f'{leg}: {packages} packages but no routes')
        if model_name not in model_table.index:
            raise ValueError(f'GLIMS model factor missing: {model_name}')
        model = model_table.loc[model_name]
        def factor(field):
            value = model.get(field)
            return None if value is None or pd.isna(value) else float(value)
        co2_factor, nox_factor = factor('co2_km'), factor('nox_km')
        co2 = distance * co2_factor / 1000 if co2_factor is not None else (0.0 if not routes else None)
        nox = distance * nox_factor / 1000 if nox_factor is not None else (0.0 if not routes else None)
        # GLIMS route-start preparation time may be provided by the native route plan.
        plan = run_dir / 'routing' / ('supply' if leg == 'PUDO_SUPPLY' else 'microhub_supply' if leg == 'MICROHUB_SUPPLY' else 'bike_delivery' if leg == 'CARGO_BIKE' else 'van_delivery') / 'route_summary.json'
        import json
        details = json.loads(plan.read_text(encoding='utf-8')) if plan.exists() else {}
        start_time = details.get('glims_route_plan', {}).get('route_start_time_per_route_min')
        if start_time is None:
            start_time = details.get('route_start_time_per_route_min')
        distance_cost = distance * float(costs[f'{vehicle}_cost_per_km'])
        labor = None
        if not routes:
            labor = 0.0
        elif start_time is not None:
            distance_cost, labor, _ = calculate_direct_route_operating_cost(
                distance_km=distance, total_duration_min=duration, route_count=routes,
                route_start_time_per_route_min=float(start_time),
                cost_per_km=float(costs[f'{vehicle}_cost_per_km']),
                labor_cost_per_hour=float(costs[f'{vehicle}_labor_cost_per_hour']))
        # Facility commissions are charged once per delivered package, not per transport leg.
        commission = (float(costs['pudo_commission_per_package']) * packages if leg == 'PUDO_SUPPLY'
                      else float(costs['microhub_commission_per_package']) * packages if leg == 'MICROHUB_SUPPLY' else 0.0)
        fixed = calculate_additional_costs(
            package_count=0, route_count=routes, used_facility_count=0,
            co2_kg=co2 if co2 is not None else 0, customer_travel_min=0,
            vehicle_fixed_cost_per_route=float(costs.get(f'{vehicle}_fixed_cost_per_route', 0)),
            vehicle_capex_allocation_per_route=float(costs.get(f'{vehicle}_capex_allocation_per_route', 0)))
        other = fixed['vehicle_fixed_cost'] + fixed['capital_allocation_cost']
        total = distance_cost + labor + commission + other if labor is not None else None
        rows.append(dict(city=summary['city'], model='M6', leg=leg, vehicle_type=vehicle,
            paquetes_transportados=packages, numero_viajes=routes, km_recorridos=distance,
            duration_min=duration, route_start_time_per_route_min=start_time,
            costo_distancia_eur=distance_cost, costo_laboral_eur=labor,
            costo_servicio_instalacion_eur=commission, costo_fijo_vehiculos_eur=other,
            costo_total_eur=total, emisiones_co2_kg=co2, emisiones_nox_kg=nox,
            status='complete' if total is not None and co2 is not None and nox is not None else 'partial'))
        if total is None: complete_cost = False
        else: total_cost += total
        if co2 is None: complete_co2 = False
        else: total_co2 += co2
        if nox is None: complete_nox = False
        else: total_nox += nox
        plan_frames[leg] = pd.DataFrame([dict(city=summary['city'], neighborhood='', model='M6',
            leg=leg, depot=metrics.get('selected_center', ''), routing_algorithm=metrics.get('routing_algorithm'),
            vehicle_type=vehicle, routing_runtime_seconds=metrics.get('routing_runtime_seconds'),
            unroutable_customer_count=metrics.get('unroutable_customer_count', 0),
            unroutable_package_count=metrics.get('unroutable_package_count', 0))])
    _write(run_dir/'costs'/'operation_costs_emissions.csv', rows, list(rows[0]))
    routing_metrics = build_routing_plan_metrics_frame(plan_frames)
    routing_path = run_dir/'routing'/'routing_plan_metrics.csv'
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_metrics.to_csv(routing_path, index=False, encoding='utf-8-sig')
    totals = dict(costo_total_eur=total_cost if complete_cost else None,
                  emisiones_co2_kg=total_co2 if complete_co2 else None,
                  emisiones_nox_kg=total_nox if complete_nox else None,
                  costo_parcial_conocido_eur=total_cost,
                  cost_complete=complete_cost, emissions_complete=complete_co2 and complete_nox)
    return totals

def export_glims_summary(run_dir, summary, parameters, reference_columns=None):
    """Single M6 system row, with GLIMS' principal summary field names."""
    run_dir = Path(run_dir)
    audit = pd.read_csv(run_dir/'costs'/'operation_costs_emissions.csv', encoding='utf-8-sig')
    costs = summary['economics_environment']
    total_packages = sum(summary['packages_by_mode'].values())
    total_km = float(audit.km_recorridos.sum())
    total_routes = int(audit.numero_viajes.sum())
    value = lambda name: (float(audit[name].sum()) if audit[name].notna().all() else None)
    row = dict(ciudad=summary['city'], barrio='', model_code='M6', modelo='Hybrid last-mile',
        paquetes=total_packages, numero_viajes=total_routes, km_recorridos=total_km,
        network_km=total_km, emisiones_co2_kg=costs['emisiones_co2_kg'],
        emisiones_nox_kg=costs['emisiones_nox_kg'],
        costo_distancia_eur=value('costo_distancia_eur'),
        costo_laboral_eur=value('costo_laboral_eur'),
        costo_servicio_facility_eur=value('costo_servicio_instalacion_eur'),
        costo_vehiculo_fijo_eur=value('costo_fijo_vehiculos_eur'),
        costo_operacion_ruta_eur=(value('costo_distancia_eur') + value('costo_laboral_eur')
                                  if value('costo_laboral_eur') is not None else None),
        costo_total_eur=costs['costo_total_eur'],
        costo_por_paquete=(costs['costo_total_eur']/total_packages if costs['costo_total_eur'] is not None and total_packages else None),
        co2_kg_por_paquete=(costs['emisiones_co2_kg']/total_packages if costs['emisiones_co2_kg'] is not None and total_packages else None),
        nox_g_por_paquete=(costs['emisiones_nox_kg']*1000/total_packages if costs['emisiones_nox_kg'] is not None and total_packages else None),
        co2_kg_por_km=(costs['emisiones_co2_kg']/total_km if costs['emisiones_co2_kg'] is not None and total_km else None),
        direct_km=float(audit.loc[audit.leg=='VAN','km_recorridos'].sum()),
        direct_trip_count=int(audit.loc[audit.leg=='VAN','numero_viajes'].sum()),
        supply_km=float(audit.loc[audit.leg.isin(['PUDO_SUPPLY','MICROHUB_SUPPLY']),'km_recorridos'].sum()),
        supply_trip_count=int(audit.loc[audit.leg.isin(['PUDO_SUPPLY','MICROHUB_SUPPLY']),'numero_viajes'].sum()),
        last_mile_km=float(audit.loc[audit.leg=='CARGO_BIKE','km_recorridos'].sum()),
        last_mile_trip_count=int(audit.loc[audit.leg=='CARGO_BIKE','numero_viajes'].sum()),
        demand_scenario=summary.get('demand_scenario'), demand_seed=summary.get('demand_seed'),
        experiment_id=summary.get('run_id'))
    _write(run_dir/'summary.csv', [row], list(row))
    return row
