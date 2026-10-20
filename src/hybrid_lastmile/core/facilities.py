"""Authoritative GLIMS service codes; one physical cluster may offer multiple services."""
from dataclasses import dataclass
import pandas as pd
from glims.common.constants import PUDO_FACILITY_CODES, MICROHUB_FACILITY_CODES, ORIGIN_FACILITY_CODES
from glims.simulation.operational_points import get_facility_candidates

@dataclass(frozen=True)
class Site:
    site_id: str
    latitude: float
    longitude: float
    name: str

def load_sites(csv_path, codes) -> list[Site]:
    raw = pd.read_csv(csv_path, encoding='utf-8-sig', low_memory=False)
    filtered = get_facility_candidates(raw, codes)
    if 'Location_Cluster_ID' not in filtered.columns:
        raise ValueError('Expected Location_Cluster_ID in classified facilities')
    sites = []
    for cluster_id, rows in filtered.groupby('Location_Cluster_ID', sort=True, dropna=True):
        row = rows.iloc[0]
        lat, lon = float(row.Latitude), float(row.Longitude)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        name = str(row.get('Location', cluster_id))
        sites.append(Site(str(cluster_id), lat, lon, name))
    return sites

def load_all_sites(path):
    return {
        'PUDO': load_sites(path, PUDO_FACILITY_CODES),
        'MICROHUB': load_sites(path, MICROHUB_FACILITY_CODES),
        'CC': load_sites(path, ORIGIN_FACILITY_CODES),
    }
