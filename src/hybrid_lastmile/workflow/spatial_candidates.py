"""Conservative geographic prefilter for OSRM rectangular distance matrices.

A great-circle lower bound only excludes physically unreachable candidates;
OSRM remains authoritative for walking/cycling eligibility.
"""
from __future__ import annotations
import csv
from pathlib import Path
import numpy as np


def lower_bound_m(origins, destinations):
    lat1 = np.radians(np.array([p.latitude for p in origins], dtype=float))[:, None]
    lon1 = np.radians(np.array([p.longitude for p in origins], dtype=float))[:, None]
    lat2 = np.radians(np.array([p.latitude for p in destinations], dtype=float))[None, :]
    lon2 = np.radians(np.array([p.longitude for p in destinations], dtype=float))[None, :]
    a = np.sin((lat2-lat1)/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin((lon2-lon1)/2)**2
    return 2*6371008.8*np.arcsin(np.sqrt(np.clip(a, 0, 1)))


class CandidateFilter:
    def __init__(self, network, *, enabled=False, block_size=25):
        self.network = network
        self.enabled = bool(enabled)
        self.block_size = int(block_size)
        if self.block_size < 1:
            raise ValueError('candidate filter block_size must be positive')
        self.metrics = []

    def rectangular(self, origins, destinations, mode, max_distance_m):
        if not self.enabled:
            return self.network.rectangular(origins, destinations, mode)
        from hybrid_lastmile.simulation.osrm import NetworkMatrix
        n, m = len(origins), len(destinations)
        distances = np.full((n, m), np.inf, dtype=float)
        durations = np.full((n, m), np.inf, dtype=float)
        considered = 0
        requests = 0
        for start in range(0, n, self.block_size):
            batch = origins[start:start+self.block_size]
            # 0.999 safety margin for floating point/earth-model differences.
            eligible = lower_bound_m(batch, destinations) <= max_distance_m * 1.001
            columns = np.flatnonzero(eligible.any(axis=0))
            considered += int(eligible.sum())
            if len(columns) == 0:
                continue
            subset = [destinations[j] for j in columns]
            matrix = self.network.rectangular(batch, subset, mode)
            for i in range(len(batch)):
                valid = eligible[i, columns]
                distances[start+i, columns[valid]] = matrix.distances_m[i, valid]
                durations[start+i, columns[valid]] = matrix.durations_s[i, valid]
            requests += 1
        self.metrics.append({'mode': mode, 'origins': n, 'facilities': m,
            'candidate_pairs_total': n*m, 'candidate_pairs_after_prefilter': considered,
            'candidate_pairs_skipped': n*m-considered, 'osrm_matrix_batches': requests,
            'prefilter_enabled': True, 'maximum_network_distance_m': max_distance_m})
        return NetworkMatrix(distances, durations)

    def export(self, output: Path):
        path = output/'routing'/'candidate_filter_metrics.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ('mode','origins','facilities','candidate_pairs_total','candidate_pairs_after_prefilter',
                  'candidate_pairs_skipped','osrm_matrix_batches','prefilter_enabled','maximum_network_distance_m')
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.metrics)
        return {'enabled': self.enabled, 'metrics_file': str(path.relative_to(output)), 'stages': self.metrics}
