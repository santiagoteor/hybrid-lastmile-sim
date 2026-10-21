from dataclasses import dataclass
import numpy as np
from hybrid_lastmile.workflow.spatial_candidates import CandidateFilter, lower_bound_m
from dataclasses import dataclass as _dataclass
import sys, types
@_dataclass
class NetworkMatrix:
    distances_m: np.ndarray
    durations_s: np.ndarray
# Isolate this unit test from GLIMS and the remote OSRM services.
sys.modules.setdefault("hybrid_lastmile.simulation.osrm", types.ModuleType("hybrid_lastmile.simulation.osrm"))
sys.modules["hybrid_lastmile.simulation.osrm"].NetworkMatrix = NetworkMatrix

@dataclass
class Point:
    latitude: float
    longitude: float

class FakeNetwork:
    def __init__(self):
        self.calls = []
    def rectangular(self, origins, destinations, mode):
        self.calls.append((len(origins), len(destinations), mode))
        values = lower_bound_m(origins, destinations) * 1.2
        return NetworkMatrix(values, values / 3)

def test_filtered_equals_full_on_feasible_pairs_and_preserves_order():
    customers = [Point(41.4 + k*0.001, 2.1) for k in [3, 0, 2, 1]]
    sites = [Point(41.4, 2.1), Point(41.401, 2.1), Point(42.5, 2.1)]
    baseline = FakeNetwork().rectangular(customers, sites, 'walking')
    network = FakeNetwork()
    filtered = CandidateFilter(network, enabled=True, block_size=2).rectangular(customers, sites, 'walking', 500)
    np.testing.assert_array_equal(filtered.distances_m[:, :2], baseline.distances_m[:, :2])
    assert np.isinf(filtered.distances_m[:, 2]).all()
    assert all(cols < 3 for _, cols, _ in network.calls)

def test_disabled_uses_original_network():
    network = FakeNetwork()
    customers = [Point(41.4, 2.1)]
    sites = [Point(41.4, 2.1)]
    result = CandidateFilter(network, enabled=False).rectangular(customers, sites, 'cycling', 3000)
    assert result.distances_m.shape == (1, 1)
    assert network.calls == [(1, 1, 'cycling')]
