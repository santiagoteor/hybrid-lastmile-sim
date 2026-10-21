"""Spatially grouped, conservative candidate queries using GLIMS OSRM tables.

This is an opt-in experimental acceleration. OSRM snapping can make a
geodesic bound imperfect; compare against the unfiltered baseline before use.
"""
from __future__ import annotations

import csv
from pathlib import Path
import numpy as np


def lower_bound_m(origins, destinations):
    if not origins or not destinations:
        return np.empty((len(origins), len(destinations)))
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
        if not n or not m:
            return NetworkMatrix(distances, durations)

        # Sort by geographic location, not customer ID, so nearby customers
        # share small facility unions. Original row positions are preserved.
        order = sorted(range(n), key=lambda i: (float(origins[i].latitude), float(origins[i].longitude)))
        candidate_pairs = 0
        matrix_calls = 0
        queried_pairs = 0
        # Extra buffer mitigates OSRM endpoint snapping differences. It is
        # NOT a formal guarantee of exact equivalence with the full matrix.
        safety_m = max(1000.0, float(max_distance_m) * 0.10)
        for start in range(0, n, self.block_size):
            indices = order[start:start+self.block_size]
            batch = [origins[i] for i in indices]
            eligible = lower_bound_m(batch, destinations) <= float(max_distance_m) + safety_m
            candidate_pairs += int(eligible.sum())
            columns = np.flatnonzero(eligible.any(axis=0))
            if not len(columns):
                continue
            subset = [destinations[j] for j in columns]
            matrix = self.network.rectangular(batch, subset, mode)
            matrix_calls += 1
            queried_pairs += len(batch)*len(columns)
            for local, original in enumerate(indices):
                selected = columns[eligible[local, columns]]
                # Only copy the OSRM distances of eligible pairs.
                distances[original, selected] = matrix.distances_m[local, eligible[local, columns]]
                durations[original, selected] = matrix.durations_s[local, eligible[local, columns]]
        self.metrics.append({
            'mode': mode, 'origins': n, 'facilities': m,
            'candidate_pairs_total': n*m,
            'candidate_pairs_after_prefilter': candidate_pairs,
            'candidate_pairs_skipped': n*m-candidate_pairs,
            'osrm_matrix_batches': matrix_calls,
            'osrm_pairs_queried': queried_pairs,
            'prefilter_enabled': True,
            'maximum_network_distance_m': max_distance_m,
            'safety_buffer_m': safety_m,
        })
        return NetworkMatrix(distances, durations)

    def export(self, output: Path):
        path = output/'routing'/'candidate_filter_metrics.csv'
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ('mode','origins','facilities','candidate_pairs_total',
                  'candidate_pairs_after_prefilter','candidate_pairs_skipped',
                  'osrm_matrix_batches','osrm_pairs_queried','prefilter_enabled',
                  'maximum_network_distance_m','safety_buffer_m')
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.metrics)
        return {'enabled': self.enabled, 'metrics_file': str(path.relative_to(output)), 'stages': self.metrics}
