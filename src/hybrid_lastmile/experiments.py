"""Batch experiment runner: separates scientific experiments from OSRM infrastructure."""
from __future__ import annotations
import csv
import hashlib
import itertools
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .scenario import Scenario
from .parameters import load_glims_parameters
from .data_loader import load_customers
from .facilities import load_all_sites
from .network import GlimsNetwork
from .engine import simulate
from .operational_inputs import export_operational_inputs
from glims.routing.osrm_client import (
    configure_osrm_matrix_cache, get_osrm_cache_stats, reset_osrm_cache_stats,
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _options(value: Any) -> list:
    return value if isinstance(value, list) else [value]


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


def run_experiment(config_path: str | Path, data_root: str | Path, output_root: str | Path,
                   infrastructure_path: str | Path | None = None):
    config_path, data_root, output_root = Path(config_path), Path(data_root), Path(output_root)
    cfg = _read_json(config_path)
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
    network = GlimsNetwork(city, block_size)
    for demand_scenario, instance_size, demand_seed, choice_seed in itertools.product(
        _options(cfg['demand_scenario']), _options(cfg['instance_size']),
        _options(cfg['demand_seed']), _options(choice['seed'])):
        demand_file = _select_demand_file(city_root/'demand', demand_scenario, int(instance_size), int(demand_seed))
        customers = _filter_by_zones(load_customers(demand_file), zones)
        scenario = Scenario(city=city, demand_file=demand_file.name, seed=int(choice_seed),
            baseline_pudo_probability=float(choice['baseline_pudo_probability']),
            distance_penalty_per_km=float(choice['distance_penalty_per_km']),
            osrm_block_size=block_size, osrm_cache_enabled=bool(cache['enabled']),
            osrm_cache_directory=str(cache['directory']), **defaults)
        signature = {'city':city, 'demand_scenario':demand_scenario, 'instance_size':instance_size,
            'demand_seed':demand_seed,'choice_seed':choice_seed,'zones':zones}
        run_id = (f'{demand_scenario}_{instance_size}_dseed{demand_seed}_cseed{choice_seed}_'
                  + hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:8])
        output = output_root / city / name / run_id
        output.mkdir(parents=True, exist_ok=True)
        reset_osrm_cache_stats()
        allocations, summary = simulate(customers, sites, network, scenario)
        _write_csv(output/'allocations'/'customer_assignments.csv', [asdict(a) for a in allocations])
        operational_manifest = export_operational_inputs(output, customers, sites, allocations, parameters.capacities)
        summary['operational_manifest'] = operational_manifest
        summary['run_id'] = run_id
        summary['demand_scenario'] = demand_scenario
        summary['demand_seed'] = demand_seed
        summary['choice_seed'] = choice_seed
        summary['osrm_cache_stats'] = get_osrm_cache_stats()
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
            for s in sites[category] if any(a.facility_id==s.site_id and a.delivery_mode==mode for a in allocations)
        ])
        print(f'Finished {run_id}: {summary["customers_by_mode"]}; cache={summary["osrm_cache_stats"]}')
        run_summaries.append({'run_id':run_id,'city':city,'demand_scenario':demand_scenario,
            'instance_size':instance_size,'demand_seed':demand_seed,'choice_seed':choice_seed,
            **{f'customers_{k}':v for k,v in summary['customers_by_mode'].items()},
            **{f'packages_{k}':v for k,v in summary['packages_by_mode'].items()}})
    _write_csv(output_root/city/name/'summary.csv',run_summaries)
    return run_summaries
