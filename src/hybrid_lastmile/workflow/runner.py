"""Batch experiment runner: separates scientific experiments from OSRM infrastructure."""
from __future__ import annotations
import csv
import hashlib
import itertools
import json
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from datetime import datetime
from typing import Any

from hybrid_lastmile.core.scenario import Scenario
from hybrid_lastmile.core.parameters import load_glims_parameters
from hybrid_lastmile.core.loader import load_customers
from hybrid_lastmile.core.facilities import load_all_sites
from hybrid_lastmile.simulation.osrm import GlimsNetwork
from hybrid_lastmile.simulation.engine import simulate
from hybrid_lastmile.workflow.operational_inputs import export_operational_inputs
from hybrid_lastmile.workflow.supply import prepare_pudo_supply, prepare_microhub_supply
from hybrid_lastmile.workflow.supply_routing import plan_pudo_supply, plan_microhub_supply
from hybrid_lastmile.workflow.supply_accessibility import inaccessible_supply_facilities
from hybrid_lastmile.workflow.results import export_run_results
from hybrid_lastmile.workflow.results_adapter import export_glims_metrics, export_glims_summary
from hybrid_lastmile.workflow.result_audit import export_route_audit
from hybrid_lastmile.workflow.geography import export_geography
from hybrid_lastmile.workflow.bike_routing import plan_bike_delivery
from hybrid_lastmile.workflow.van_routing import plan_van_delivery
from hybrid_lastmile.workflow.warehouses import export_warehouse_assignments, resolve_warehouses
from hybrid_lastmile.workflow.warehouse_supply import plan_partitioned_supply
from hybrid_lastmile.workflow.operating_timeline import build_operating_timeline
from hybrid_lastmile.workflow.schedule_audit import reconcile_schedule_diagnostics
from glims.routing.osrm_client import (
    configure_osrm_matrix_cache, get_osrm_cache_stats, reset_osrm_cache_stats,
)


@contextmanager
def _timed_stage(stage_name, records):
    """Record elapsed wall-clock time without retaining routes or OSRM matrices."""
    start = datetime.now().astimezone()
    tick = time.perf_counter()
    try:
        yield
    finally:
        end = datetime.now().astimezone()
        elapsed = round(time.perf_counter() - tick, 3)
        records.append({'stage': stage_name, 'started_at': start.isoformat(timespec='seconds'),
                        'finished_at': end.isoformat(timespec='seconds'), 'elapsed_seconds': elapsed})
        print(f'[M6 timing] {stage_name}: {elapsed:.3f} s', flush=True)

def _write_timing(output, records, started_at, tick):
    finished = datetime.now().astimezone()
    total = round(time.perf_counter() - tick, 3)
    timing = {'started_at': started_at.isoformat(timespec='seconds'),
              'finished_at': finished.isoformat(timespec='seconds'),
              'elapsed_seconds': total, 'stages': records}
    (output / 'execution_timing.json').write_text(json.dumps(timing, indent=2), encoding='utf-8')
    _write_csv(output / 'execution_timing.csv', records)
    print(f'[M6 timing] total experiment: {total:.3f} s', flush=True)
    return timing

def _write_batch_status(batch_dir, records):
    """Write a small, atomic, machine-readable batch progress snapshot."""
    path = batch_dir / 'batch_status.json'
    temp = batch_dir / 'batch_status.json.tmp'
    temp.write_text(json.dumps({'total': len(records), 'runs': records}, indent=2), encoding='utf-8')
    temp.replace(path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _options(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def _experiment_combinations(cfg, choice):
    """Pair replicate seeds by position, or explicitly request their Cartesian product."""
    demand_seeds = _options(cfg['demand_seed'])
    choice_seeds = _options(choice['seed'])
    mode = cfg.get('seed_pairing', 'paired')
    if mode == 'paired':
        if len(demand_seeds) != len(choice_seeds):
            raise ValueError('seed_pairing=paired requires demand_seed and choice.seed lists of equal length')
        seeds = list(zip(demand_seeds, choice_seeds))
    elif mode == 'cartesian':
        seeds = list(itertools.product(demand_seeds, choice_seeds))
    else:
        raise ValueError('seed_pairing must be paired or cartesian')
    return [(scenario, size, dseed, cseed)
            for scenario, size in itertools.product(
                _options(cfg['demand_scenario']), _options(cfg['instance_size']))
            for dseed, cseed in seeds]


def _prepare_accessible_supply(output, city, customers, sites, network, scenario,
                               van_capacity, stage_records):
    """Reassign from scratch after removing disconnected PUDO/microhub facilities."""
    active_sites = {key: list(values) for key, values in sites.items()}
    excluded = []
    maximum_rounds = len(active_sites['PUDO']) + len(active_sites['MICROHUB']) + 1
    for round_index in range(maximum_rounds):
        with _timed_stage('demand_assignment' if round_index == 0 else 'demand_reassignment', stage_records):
            allocations, summary = simulate(customers, active_sites, network, scenario)
        with _timed_stage('pudo_supply_preparation', stage_records):
            pudo_summary = prepare_pudo_supply(output, active_sites, allocations, van_capacity, city)
        with _timed_stage('microhub_supply_preparation', stage_records):
            micro_summary = prepare_microhub_supply(output, active_sites, allocations, van_capacity, city)
        newly_excluded = []
        with _timed_stage('supply_accessibility_preflight', stage_records):
            for facility_type, folder in (('PUDO', 'supply'), ('MICROHUB', 'microhub_supply')):
                bad_ids = set(inaccessible_supply_facilities(output, city, folder_name=folder))
                known = {site.site_id for site in active_sites[facility_type]}
                if not bad_ids <= known:
                    raise ValueError(f'Unknown excluded {facility_type} IDs: {sorted(bad_ids - known)}')
                for site in active_sites[facility_type]:
                    if site.site_id in bad_ids:
                        newly_excluded.append({'round': round_index + 1,
                                               'facility_type': facility_type,
                                               'facility_id': site.site_id,
                                               'latitude': site.latitude,
                                               'longitude': site.longitude,
                                               'reason': 'strongly_disconnected_driving_osrm'})
                active_sites[facility_type] = [site for site in active_sites[facility_type]
                                               if site.site_id not in bad_ids]
        if not newly_excluded:
            if excluded:
                _write_csv(output / 'audit' / 'excluded_supply_facilities.csv', excluded)
            summary['supply_accessibility'] = {'excluded_facility_count': len(excluded),
                                                'reassignment_rounds': round_index,
                                                'excluded_facilities_audit': 'audit/excluded_supply_facilities.csv' if excluded else None}
            return allocations, summary, active_sites, pudo_summary, micro_summary
        excluded.extend(newly_excluded)
        print(f'[M6 supply preflight] Excluded {len(newly_excluded)} inaccessible facilities; '
              f'reassigning all customers (round {round_index + 1})', flush=True)
    raise RuntimeError('Supply accessibility preflight did not converge')


def _select_demand_file(directory: Path, scenario: str, size: int, seed: int) -> Path:
    path = directory / f'demand_{scenario}_{size}_seed_{seed}.csv'
    if not path.is_file():
        raise FileNotFoundError(f'Demand instance missing: {path}')
    return path


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _filter_by_zones(customers, zone_names):
    if not zone_names:
        return customers
    keys = ('barri', 'districte', 'zona', 'neighborhood', 'district')
    available = set().union(*(c.metadata.keys() for c in customers)) if customers else set()
    matches = keys and tuple(k for k in keys if k in available)
    if not matches:
        raise ValueError(f'Zones requested, but demand CSV lacks zone fields: {sorted(available)}')
    wanted = {str(z).casefold().strip() for z in zone_names}
    selected = [c for c in customers if any(str(c.metadata.get(k, '')).casefold().strip() in wanted for k in matches)]
    if not selected:
        raise ValueError(f'No customers match selected zones: {zone_names}')
    return selected


def _filter_facilities_for_zones(sites, city_root: Path, zones: list, config: dict):
    """Use GLIMS' native metric polygon-buffer filter, never a fixed top-N shortlist."""
    if not zones or not config.get('enabled', False):
        return sites
    import geopandas as gpd
    from glims.simulation.facility_filter import FacilityFilterSettings, filter_facilities_for_zone
    import pandas as pd
    geo = city_root / 'geography' / 'neighborhoods_limits.geojson'
    if not geo.exists():
        raise FileNotFoundError(f'Zone facility filtering requires {geo}')
    polygons = gpd.read_file(geo)
    columns = ('zona', 'nom', 'NOM', 'name', 'barri', 'neighborhood')
    name_col = next((c for c in columns if c in polygons.columns), None)
    if name_col is None:
        raise ValueError(f'Cannot identify zone name in {geo}; columns: {list(polygons.columns)}')
    settings = FacilityFilterSettings(**{k:v for k,v in config.items() if k in FacilityFilterSettings.__dataclass_fields__})
    settings.validate()
    filtered = {}
    for category, facilities in sites.items():
        if not facilities:
            filtered[category] = []
            continue
        table = pd.DataFrame([{'Latitude':s.latitude,'Longitude':s.longitude,'site_id':s.site_id} for s in facilities])
        selected_ids = set()
        for zone in zones:
            match = polygons[polygons[name_col].astype(str).str.casefold().str.strip() == str(zone).casefold().strip()]
            if match.empty:
                raise ValueError(f'Zone {zone!r} not found in {geo}')
            for _, polygon in match.iterrows():
                subset = filter_facilities_for_zone(table, polygon, settings=settings,
                    zone_crs=polygons.crs, facility_label=category)
                selected_ids.update(subset['site_id'].astype(str))
        filtered[category] = [s for s in facilities if s.site_id in selected_ids]
    return filtered


def _normalize_experiment(cfg: dict) -> dict:
    """Accept previous GLIMS-prefixed config keys without manual migration."""
    cfg = dict(cfg)
    for old, new in (("glims_supply", "supply"), ("glims_supply_routing", "supply_routing")):
        if old in cfg:
            if new in cfg:
                raise ValueError(f"Both {old} and {new} are present; keep only {new}")
            cfg[new] = cfg.pop(old)
    for key in ("supply", "supply_routing"):
        if key in cfg:
            if not isinstance(cfg[key], dict):
                raise ValueError(f"{key} must be an object")
            cfg[key] = {k: v for k, v in cfg[key].items() if k not in ("enabled", "select_center")}
    return cfg


def run_experiment(config_path: str | Path, data_root: str | Path, output_root: str | Path,
                   infrastructure_path: str | Path | None = None):
    config_path, data_root, output_root = Path(config_path), Path(data_root), Path(output_root)
    cfg = _normalize_experiment(_read_json(config_path))
    city = cfg['city']
    name = cfg['experiment_name']
    infra = _read_json(Path(infrastructure_path)) if infrastructure_path else {}
    cache = infra.get('osrm_cache', {'enabled': True, 'directory': '.glims_cache/osrm'})
    configure_osrm_matrix_cache(enabled=bool(cache['enabled']), directory=Path(cache['directory']).expanduser().resolve())
    block_size = int(infra.get('osrm_block_size', 25))
    if block_size < 1:
        raise ValueError('osrm_block_size must be positive')
    if cfg.get('models', ['M6']) != ['M6']:
        raise ValueError('This runner supports M6 only')
    zones = cfg.get('zones', [])
    city_root = data_root / 'locations' / city
    sites = load_all_sites(city_root / 'facilities' / 'records_classified.csv')
    sites = _filter_facilities_for_zones(sites, city_root, zones, cfg.get('facility_filter', {}))
    choice = cfg['choice']
    parameters = load_glims_parameters(data_root)
    if 'physical_parameters' in cfg:
        raise ValueError('Remove physical_parameters from experiment JSON; capacities come from data/parameters/model_parameters.csv')
    defaults = {
        **parameters.capacities,
        'max_walking_distance_m': float(choice['max_walking_distance_m']),
        'microhub_max_cycling_distance_m': float(cfg.get('microhub_coverage_radius_m', 3000)),
    }
    run_summaries = []
    batch_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    network = GlimsNetwork(city, block_size)
    if 'spatial_candidates' in cfg:
        raise ValueError('Remove spatial_candidates from the experiment JSON; the experimental filter has been retired.')
    batch_dir = output_root / city / name / batch_timestamp
    batch_dir.mkdir(parents=True, exist_ok=True)
    combinations = _experiment_combinations(cfg, choice)
    batch_status = [dict(index=i + 1, demand_scenario=str(combo[0]),
                         instance_size=int(combo[1]), demand_seed=int(combo[2]),
                         choice_seed=int(combo[3]), status='pending',
                         started_at=None, finished_at=None, elapsed_seconds=None,
                         output=None, error=None)
                    for i, combo in enumerate(combinations)]
    _write_batch_status(batch_dir, batch_status)
    print(f'[M6 batch] {len(combinations)} runs queued | {batch_dir}', flush=True)
    for batch_index, (demand_scenario, instance_size, demand_seed, choice_seed) in enumerate(combinations):
        item = batch_status[batch_index]
        item['status'] = 'running'
        item['started_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        run_tick = time.perf_counter()
        _write_batch_status(batch_dir, batch_status)
        print(f'[M6 batch {batch_index + 1}/{len(combinations)}] RUNNING '
              f'{demand_scenario} {instance_size} demand_seed={demand_seed} '
              f'choice_seed={choice_seed}', flush=True)
        try:
            demand_file = _select_demand_file(city_root/'demand', demand_scenario, int(instance_size), int(demand_seed))
            customers = _filter_by_zones(load_customers(demand_file), zones)
            scenario = Scenario(city=city, demand_file=demand_file.name, seed=int(choice_seed),
                baseline_pudo_probability=float(choice['baseline_pudo_probability']),
                distance_penalty_per_km=float(choice['distance_penalty_per_km']),
                osrm_block_size=block_size, osrm_cache_enabled=bool(cache['enabled']),
                osrm_cache_directory=str(cache['directory']), **defaults)
            signature = {'city':city, 'demand_scenario':demand_scenario, 'instance_size':instance_size,
                'demand_seed':demand_seed,'choice_seed':choice_seed,'zones':zones,
                'supply_routing': cfg.get('supply_routing', {}), 'bike_routing': cfg.get('bike_routing', cfg.get('supply_routing', {})), 'van_routing': cfg.get('van_routing', cfg.get('supply_routing', {})), 'supply': cfg.get('supply', {}), 'warehouses': cfg.get('warehouses'), 'operating_hours': cfg.get('operating_hours')}
            run_id = (f'{demand_scenario}_{instance_size}_dseed{demand_seed}_cseed{choice_seed}_'
                      + str(cfg.get('supply_routing', {}).get('routing', {}).get('algorithm', 'cws')) + '_'
                      + hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:8])
            # One timestamp per experiment batch; individual scenarios are nested inside.
            output = batch_dir / run_id
            suffix = 1
            while output.exists():
                output = batch_dir / f'{run_id}_{suffix:02d}'
                suffix += 1
            output.mkdir(parents=True, exist_ok=False)
            item["output"] = str(output)
            _write_batch_status(batch_dir, batch_status)
            reset_osrm_cache_stats()
            timing_started = datetime.now().astimezone()
            timing_tick = time.perf_counter()
            stage_records = []
            warehouse_assignments = None
            resolved_warehouses = None
            if 'warehouses' in cfg:
                resolved_warehouses = resolve_warehouses(cfg['warehouses'], sites)
                warehouse_assignments, summary_warehouses = export_warehouse_assignments(
                    output, customers, resolved_warehouses)
            # Preflight both supply legs before exporting operational inputs or routing.
            # Any excluded facility triggers a full, deterministic reassignment.
            route_cfg = cfg.get('supply_routing', {})
            if not isinstance(route_cfg, dict) or 'enabled' in route_cfg:
                raise ValueError('supply_routing must be an object without enabled')
            allocations, summary, run_sites, pudo_supply, microhub_supply = _prepare_accessible_supply(
                output, city, customers, sites, network, scenario,
                parameters.capacities['van_capacity'], stage_records)
            if warehouse_assignments is not None:
                summary['warehouses'] = summary_warehouses
            summary['supply'] = pudo_supply
            summary['microhub_supply'] = microhub_supply
            with _timed_stage('operational_inputs', stage_records):
                operational_manifest = export_operational_inputs(
                    output, customers, run_sites, allocations, parameters.capacities,
                    warehouse_assignments=warehouse_assignments)
            summary['operational_manifest'] = operational_manifest
            with _timed_stage('pudo_supply_routing', stage_records):
                summary['supply_routing'] = (plan_partitioned_supply(
                    output, city, run_sites, allocations, warehouse_assignments, resolved_warehouses,
                    parameters.capacities['van_capacity'], route_cfg, data_root=data_root, facility_type='PUDO')
                    if resolved_warehouses else plan_pudo_supply(
                    output, city, parameters.capacities['van_capacity'], route_cfg, data_root=data_root))
            with _timed_stage('microhub_supply_routing', stage_records):
                summary['microhub_supply_routing'] = (plan_partitioned_supply(
                    output, city, run_sites, allocations, warehouse_assignments, resolved_warehouses,
                    parameters.capacities['van_capacity'], route_cfg, data_root=data_root, facility_type='MICROHUB')
                    if resolved_warehouses else plan_microhub_supply(
                    output, city, parameters.capacities['van_capacity'], route_cfg, data_root=data_root))
            expected_hub_packages = summary['packages_by_mode'].get('CARGO_BIKE', 0)
            if summary['microhub_supply']['packages'] != expected_hub_packages:
                raise AssertionError('Microhub inbound packages do not match bike delivery demand')
            if summary['microhub_supply_routing'].get('status') == 'planned' and (
                abs(summary['microhub_supply_routing']['packages'] - expected_hub_packages) > 1e-6
            ):
                raise AssertionError('GLIMS microhub supply routing did not conserve packages')
            with _timed_stage('bike_delivery_routing', stage_records):
                summary['bike_routing'] = plan_bike_delivery(
                    output, city, customers, run_sites, allocations, parameters.capacities['cargo_bike_capacity'],
                    cfg.get('bike_routing', route_cfg))
            with _timed_stage('van_delivery_routing', stage_records):
                summary['van_routing'] = plan_van_delivery(
                    output, city, customers, run_sites, allocations, parameters.capacities['van_capacity'],
                    cfg.get('van_routing', route_cfg))
            if 'operating_hours' in cfg:
                with _timed_stage('operating_timeline', stage_records):
                    summary['operating_timeline'] = build_operating_timeline(
                        output, city, run_sites, cfg['operating_hours'], block_size=block_size)
            summary['run_id'] = output.name
            summary['scenario_id'] = run_id
            summary['demand_scenario'] = demand_scenario
            summary['demand_seed'] = demand_seed
            summary['choice_seed'] = choice_seed
            summary['osrm_cache_stats'] = get_osrm_cache_stats()
            with _timed_stage('economics_and_emissions', stage_records):
                summary['economics_environment'] = export_glims_metrics(output, summary, allocations, parameters)
            (output/'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
            (output/'config.json').write_text(json.dumps({'experiment':cfg,'infrastructure':infra,
                'resolved_scenario':asdict(scenario),
                'parameter_sources': {'model_parameters':str(parameters.model_file.resolve()),
                                      'cost_parameters':str(parameters.cost_file.resolve())},
                'loaded_cost_catalogue': parameters.cost_catalogue},indent=2,ensure_ascii=False),encoding='utf-8')
            _write_csv(output/'audit'/'assignment_integrity.csv', [{
                'customers_loaded':len(customers),'customers_assigned':len(allocations),
                'packages_loaded':sum(c.packages for c in customers),
                'packages_assigned':sum(a.packages for a in allocations),
                'valid':len(customers)==len(allocations) and sum(c.packages for c in customers)==sum(a.packages for a in allocations)
            }])
            _write_csv(output/'facilities'/'facility_summary.csv', [
                {'facility_id':s.site_id,'facility_type':category,
                 'customers_assigned':sum(a.facility_id==s.site_id and a.delivery_mode==mode for a in allocations),
                 'packages_assigned':sum(a.packages for a in allocations if a.facility_id==s.site_id and a.delivery_mode==mode)}
                for category, mode in [('PUDO','PUDO'),('MICROHUB','CARGO_BIKE')]
                for s in run_sites[category] if any(a.facility_id==s.site_id and a.delivery_mode==mode for a in allocations)
            ])
            with _timed_stage('result_export_and_audit', stage_records):
                export_run_results(output, summary, allocations)
                (output / "summary.csv").replace(output / "operation_summary.csv")
                export_glims_summary(output, summary, parameters)
                export_route_audit(output, summary, allocations, parameters.capacities)
                if "operating_hours" in cfg:
                    summary["schedule_audit"] = reconcile_schedule_diagnostics(output)
                summary["geography"] = export_geography(output, city_root, customers, run_sites, allocations)
                (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            summary['execution_timing'] = _write_timing(output, stage_records, timing_started, timing_tick)
            (output / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
            print(f'Finished {output.name}: {summary["customers_by_mode"]}; cache={summary["osrm_cache_stats"]}')
            run_summaries.append({'run_id':output.name,'scenario_id':run_id,'city':city,'demand_scenario':demand_scenario,
                'instance_size':instance_size,'demand_seed':demand_seed,'choice_seed':choice_seed,
                **{f'customers_{k}':v for k,v in summary['customers_by_mode'].items()},
                **{f'packages_{k}':v for k,v in summary['packages_by_mode'].items()}})
        except BaseException as exc:
            item['status'] = 'failed'
            item['error'] = f'{type(exc).__name__}: {exc}'
            item['finished_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            item['elapsed_seconds'] = round(time.perf_counter() - run_tick, 3)
            _write_batch_status(batch_dir, batch_status)
            print(f'[M6 batch {batch_index + 1}/{len(combinations)}] FAILED: {item["error"]}', flush=True)
            raise
        else:
            item['status'] = 'completed'
            item['finished_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
            item['elapsed_seconds'] = round(time.perf_counter() - run_tick, 3)
            _write_batch_status(batch_dir, batch_status)
            print(f'[M6 batch {batch_index + 1}/{len(combinations)}] COMPLETED '
                  f'in {item["elapsed_seconds"]:.1f} s', flush=True)
    # The experiment-level file is an index, not a per-run summary.
    aggregate_dir = batch_dir
    legacy_summary = aggregate_dir / 'summary.csv'
    if legacy_summary.exists():
        # Earlier versions incorrectly called the experiment index summary.csv.
        legacy_summary.unlink()
    index_path = aggregate_dir / 'runs.csv'
    previous = []
    if index_path.is_file():
        with index_path.open(newline='', encoding='utf-8-sig') as stream:
            previous = list(csv.DictReader(stream))
    combined = previous + run_summaries
    if combined:
        fields = list(dict.fromkeys(key for item in combined for key in item))
        with index_path.open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(combined)
    return run_summaries
