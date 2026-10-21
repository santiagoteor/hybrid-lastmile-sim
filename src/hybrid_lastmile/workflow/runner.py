"""Batch experiment runner: separates scientific experiments from OSRM infrastructure."""
from __future__ import annotations
import csv
import hashlib
import itertools
import json
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
from hybrid_lastmile.workflow.results import export_run_results
from hybrid_lastmile.workflow.results_adapter import export_glims_metrics, export_glims_summary
from hybrid_lastmile.workflow.result_audit import export_route_audit
from hybrid_lastmile.workflow.geography import export_geography
from hybrid_lastmile.workflow.bike_routing import plan_bike_delivery
from hybrid_lastmile.workflow.van_routing import plan_van_delivery
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
            'demand_seed':demand_seed,'choice_seed':choice_seed,'zones':zones,
            'supply_routing': cfg.get('supply_routing', {}), 'bike_routing': cfg.get('bike_routing', cfg.get('supply_routing', {})), 'van_routing': cfg.get('van_routing', cfg.get('supply_routing', {})), 'supply': cfg.get('supply', {})}
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
        reset_osrm_cache_stats()
        allocations, summary = simulate(customers, sites, network, scenario)
        operational_manifest = export_operational_inputs(output, customers, sites, allocations, parameters.capacities)
        summary['operational_manifest'] = operational_manifest
        # GLIMS is a required dependency, not a configurable backend.
        # PUDO supply and routing always run when PUDO demand exists.
        supply_settings = cfg.get('supply', {})
        if not isinstance(supply_settings, dict):
            raise ValueError('supply must be an object')
        if 'enabled' in supply_settings or 'select_center' in supply_settings:
            raise ValueError('Remove supply.enabled/select_center: PUDO supply always uses GLIMS')
        route_cfg = cfg.get('supply_routing', {})
        if not isinstance(route_cfg, dict) or 'enabled' in route_cfg:
            raise ValueError('supply_routing must be an object without enabled')
        if 'glims_supply' in cfg or 'glims_supply_routing' in cfg:
            raise ValueError('Rename glims_supply -> supply and glims_supply_routing -> supply_routing; remove enabled/select_center')
        summary['supply'] = prepare_pudo_supply(
            output, sites, allocations, parameters.capacities['van_capacity'], city)
        summary['supply_routing'] = plan_pudo_supply(
            output, city, parameters.capacities['van_capacity'], route_cfg, data_root=data_root)
        summary['microhub_supply'] = prepare_microhub_supply(
            output, sites, allocations, parameters.capacities['van_capacity'], city)
        summary['microhub_supply_routing'] = plan_microhub_supply(
            output, city, parameters.capacities['van_capacity'], route_cfg, data_root=data_root)
        expected_hub_packages = summary['packages_by_mode'].get('CARGO_BIKE', 0)
        if summary['microhub_supply']['packages'] != expected_hub_packages:
            raise AssertionError('Microhub inbound packages do not match bike delivery demand')
        if summary['microhub_supply_routing'].get('status') == 'planned' and (
            abs(summary['microhub_supply_routing']['packages'] - expected_hub_packages) > 1e-6
        ):
            raise AssertionError('GLIMS microhub supply routing did not conserve packages')
        summary['bike_routing'] = plan_bike_delivery(
            output, city, customers, sites, allocations, parameters.capacities['cargo_bike_capacity'],
            cfg.get('bike_routing', route_cfg))
        summary['van_routing'] = plan_van_delivery(
            output, city, customers, sites, allocations, parameters.capacities['van_capacity'],
            cfg.get('van_routing', route_cfg))
        summary['run_id'] = output.name
        summary['scenario_id'] = run_id
        summary['demand_scenario'] = demand_scenario
        summary['demand_seed'] = demand_seed
        summary['choice_seed'] = choice_seed
        summary['osrm_cache_stats'] = get_osrm_cache_stats()
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
            for s in sites[category] if any(a.facility_id==s.site_id and a.delivery_mode==mode for a in allocations)
        ])
        export_run_results(output, summary, allocations)
        (output / "summary.csv").replace(output / "operation_summary.csv")
        export_glims_summary(output, summary, parameters)
        export_route_audit(output, summary, allocations, parameters.capacities)
        summary["geography"] = export_geography(output, city_root, customers, sites, allocations)
        (output / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f'Finished {output.name}: {summary["customers_by_mode"]}; cache={summary["osrm_cache_stats"]}')
        run_summaries.append({'run_id':output.name,'scenario_id':run_id,'city':city,'demand_scenario':demand_scenario,
            'instance_size':instance_size,'demand_seed':demand_seed,'choice_seed':choice_seed,
            **{f'customers_{k}':v for k,v in summary['customers_by_mode'].items()},
            **{f'packages_{k}':v for k,v in summary['packages_by_mode'].items()}})
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
