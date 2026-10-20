"""Rectangular matrices directly from the GLIMS public routing client."""
from dataclasses import dataclass
import numpy as np
from glims.routing.osrm_client import get_osrm_host, osrm_distance_duration_table_rectangular

@dataclass
class NetworkMatrix:
    distances_m: np.ndarray
    durations_s: np.ndarray

class GlimsNetwork:
    def __init__(self, city: str, block_size: int):
        self.city, self.block_size = city, block_size

    def rectangular(self, origins, destinations, mode: str) -> NetworkMatrix:
        if mode not in ('walking','cycling','driving'):
            raise ValueError(f'Unsupported mode: {mode}')
        shape = (len(origins),len(destinations))
        if not origins or not destinations:
            return NetworkMatrix(np.full(shape,np.nan),np.full(shape,np.nan))
        sources = [(float(p.longitude),float(p.latitude)) for p in origins]
        targets = [(float(p.longitude),float(p.latitude)) for p in destinations]
        distances, durations = osrm_distance_duration_table_rectangular(
            sources, targets, host=get_osrm_host(self.city,mode),
            profile=mode, block_size=self.block_size)
        if distances.shape != shape or durations.shape != shape:
            raise ValueError(f'Unexpected OSRM shape: {distances.shape} instead of {shape}')
        return NetworkMatrix(distances, durations)
