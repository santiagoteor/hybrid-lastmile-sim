"""Carga de entradas M6, independientes de los resultados de GLIMS.

Estructura: data/locations/<city>/{facilities,demand,geography}/.
No calcula distancias ni asigna clientes.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

City = Literal["barcelona", "madrid", "valencia"]


@dataclass(frozen=True)
class Customer:
    customer_id: str
    latitude: float
    longitude: float
    packages: int
    metadata: dict[str, str]


@dataclass(frozen=True)
class Facility:
    facility_id: str
    name: str
    facility_type: str  # PUDO, MICROHUB, CC
    latitude: float
    longitude: float
    metadata: dict[str, str]


@dataclass(frozen=True)
class CityInputs:
    city: str
    customers: list[Customer]
    facilities: list[Facility]
    geography: dict[str, Path]

    def facilities_of_type(self, kind: str) -> list[Facility]:
        return [facility for facility in self.facilities if facility.facility_type == kind.upper()]


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"No existe el archivo de entrada: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV sin cabecera: {path}")
        return list(reader)


def _number(value: str, field: str, source: Path) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Valor inválido para {field} en {source}: {value!r}") from exc


def _coordinates(row: dict[str, str], source: Path, lat_key: str, lon_key: str) -> tuple[float, float]:
    latitude = _number(row.get(lat_key, ""), lat_key, source)
    longitude = _number(row.get(lon_key, ""), lon_key, source)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError(f"Coordenadas fuera de rango en {source}: {latitude}, {longitude}")
    return latitude, longitude


def load_customers(path: str | Path) -> list[Customer]:
    source = Path(path)
    customers = []
    seen = set()
    for row in _rows(source):
        customer_id = str(row.get("customer_id", "")).strip()
        if not customer_id or customer_id in seen:
            raise ValueError(f"ID de cliente vacío o duplicado en {source}: {customer_id!r}")
        seen.add(customer_id)
        lat, lon = _coordinates(row, source, "lat", "lon")
        packages_raw = _number(row.get("demand", ""), "demand", source)
        if not packages_raw.is_integer() or packages_raw <= 0:
            raise ValueError(f"Demanda no válida del cliente {customer_id}: {packages_raw}")
        customers.append(Customer(customer_id, lat, lon, int(packages_raw), dict(row)))
    return customers


def load_facilities(path: str | Path) -> list[Facility]:
    """Un registro por clúster; prioriza categorías de servicio explícitas.

    Los registros del mismo clúster pueden corresponder a distintas empresas.
    No se inventan capacidades a partir del número de registros.
    """
    source = Path(path)
    grouped: dict[str, list[dict[str, str]]] = {}
    for index, row in enumerate(_rows(source), 1):
        cluster_id = (row.get("Location_Cluster_ID") or "").strip() or f"ROW_{index}"
        grouped.setdefault(cluster_id, []).append(row)

    result = []
    types = {"PUDO": "PUDO", "MICROHUB": "MICROHUB", "CC": "CC"}
    for cluster_id, records in grouped.items():
        kinds = {(row.get("Type_Group_Name") or "").strip().upper() for row in records}
        kinds = kinds & types.keys()
        if not kinds:
            continue
        if len(kinds) != 1:
            raise ValueError(f"Clúster {cluster_id} con categorías contradictorias: {sorted(kinds)}")
        kind = next(iter(kinds))
        valid = []
        for row in records:
            try:
                lat, lon = _coordinates(row, source, "Latitude", "Longitude")
            except ValueError:
                continue
            valid.append((row, lat, lon))
        if not valid:
            raise ValueError(f"Clúster {cluster_id} sin coordenadas válidas en {source}")
        # Representante determinista del clúster; no promedia puntos sin autorización.
        row, lat, lon = valid[0]
        name = (row.get("Location") or row.get("Company") or cluster_id).strip()
        result.append(Facility(cluster_id, name, types[kind], lat, lon, dict(row)))
    return result


def load_city_inputs(
    data_root: str | Path,
    city: City,
    demand_filename: str = "demand_high_100_seed_42.csv",
) -> CityInputs:
    root = Path(data_root)
    base = root / "locations" / city
    customers = load_customers(base / "demand" / demand_filename)
    facilities = load_facilities(base / "facilities" / "records_classified.csv")
    geography_dir = base / "geography"
    geography = {
        name: path
        for name in ("neighborhoods_limits.geojson", "districts_limits.geojson", "zones_limits.geojson")
        if (path := geography_dir / name).is_file()
    }
    return CityInputs(city=city, customers=customers, facilities=facilities, geography=geography)
