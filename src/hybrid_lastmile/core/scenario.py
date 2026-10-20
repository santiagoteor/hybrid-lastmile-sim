"""Scenario parameters in JSON; never embed city coordinates or scenario inputs."""
from dataclasses import dataclass, MISSING
from pathlib import Path
import json

@dataclass(frozen=True)
class Scenario:
    city: str
    demand_file: str
    seed: int
    baseline_pudo_probability: float
    distance_penalty_per_km: float
    max_walking_distance_m: float
    pudo_capacity: int
    microhub_capacity: int
    microhub_max_cycling_distance_m: float
    van_capacity: int
    cargo_bike_capacity: int
    osrm_block_size: int = 25
    osrm_cache_enabled: bool = True
    osrm_cache_directory: str = ".glims_cache/osrm"

    @classmethod
    def from_json(cls, path: str | Path) -> 'Scenario':
        values = json.loads(Path(path).read_text(encoding='utf-8'))
        fields = cls.__dataclass_fields__
        required = {k for k, f in fields.items() if f.default is MISSING and f.default_factory is MISSING}
        unknown = set(values) - set(fields)
        missing = required - set(values)
        if missing or unknown:
            raise ValueError(f"Scenario fields missing={sorted(missing)}, unknown={sorted(unknown)}")
        cfg = cls(**values)
        if cfg.city not in ('barcelona','madrid','valencia'):
            raise ValueError('Unsupported city')
        if not 0 < cfg.baseline_pudo_probability < 1 or cfg.distance_penalty_per_km < 0:
            raise ValueError('Invalid choice model parameters')
        if any(getattr(cfg,k) <= 0 for k in ('max_walking_distance_m','pudo_capacity','microhub_capacity','microhub_max_cycling_distance_m','van_capacity','cargo_bike_capacity','osrm_block_size')):
            raise ValueError('Capacities, distances and block size must be positive')
        if not isinstance(cfg.osrm_cache_enabled, bool) or not isinstance(cfg.osrm_cache_directory, str) or not cfg.osrm_cache_directory.strip():
            raise ValueError('Invalid OSRM cache configuration')
        return cfg
