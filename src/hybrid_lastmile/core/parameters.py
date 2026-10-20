"""Load GLIMS physical parameters and the official economic catalogue."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import pandas as pd
from glims.common.cost_utils import load_cost_parameters

MODEL_CAPACITY_ROWS = {
    "pudo_capacity": "PUDO",
    "microhub_capacity": "MICROHUB",
    "van_capacity": "FURGONETA_CONV",
    "cargo_bike_capacity": "BICICLETA_CARGO",
}

@dataclass(frozen=True)
class GlimsParameters:
    capacities: dict[str, int]
    cost_catalogue: dict[str, float]
    model_file: Path
    cost_file: Path


def load_glims_parameters(data_root: str | Path) -> GlimsParameters:
    directory = Path(data_root) / "parameters"
    model_file = directory / "model_parameters.csv"
    cost_file = directory / "cost_parameters.csv"
    if not model_file.is_file():
        raise FileNotFoundError(f"Missing GLIMS model parameters: {model_file}")
    frame = pd.read_csv(model_file, encoding="utf-8-sig")
    if not {"modelo", "capacidad"}.issubset(frame.columns):
        raise ValueError("model_parameters.csv requires modelo and capacidad columns")
    if frame.modelo.duplicated().any():
        raise ValueError("Duplicate modelo rows in model_parameters.csv")
    indexed = frame.set_index("modelo")
    capacities = {}
    for key, model in MODEL_CAPACITY_ROWS.items():
        if model not in indexed.index:
            raise ValueError(f"Missing capacity row: {model}")
        value = pd.to_numeric(pd.Series([indexed.loc[model, "capacidad"]]), errors="coerce").iloc[0]
        if pd.isna(value) or value <= 0 or not float(value).is_integer():
            raise ValueError(f"Invalid capacity for {model}: {value}")
        capacities[key] = int(value)
    # GLIMS' own loader is authoritative; do not merge the economic fields
    # from model_parameters.csv with this catalogue.
    costs = load_cost_parameters(cost_file)
    return GlimsParameters(capacities, costs, model_file, cost_file)
