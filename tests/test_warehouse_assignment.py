from dataclasses import dataclass
from hybrid_lastmile.workflow.warehouses import assign_warehouses

@dataclass
class Customer:
    customer_id: str
    latitude: float
    longitude: float
    packages: int


def test_nearest_warehouse_and_percentages():
    warehouses = [
        {'id': 'B', 'latitude': 0, 'longitude': 1},
        {'id': 'A', 'latitude': 0, 'longitude': 0},
        {'id': 'C', 'latitude': 0, 'longitude': 2},
    ]
    customers = [Customer('1', 0, 0.01, 2), Customer('2', 0, 0.02, 3),
                 Customer('3', 0, 1.01, 1), Customer('4', 0, 2.01, 4)]
    assignments, loads = assign_warehouses(customers, warehouses)
    assert [assignments[str(i)][0] for i in range(1, 5)] == ['A', 'A', 'B', 'C']
    assert [r['customers'] for r in loads] == [2, 1, 1]
    assert [r['packages'] for r in loads] == [5, 1, 4]
    assert sum(r['customer_percentage'] for r in loads) == 100
    assert sum(r['package_percentage'] for r in loads) == 100


def test_resolve_warehouse_ids_from_cc_only():
    from hybrid_lastmile.workflow.warehouses import resolve_warehouses
    from types import SimpleNamespace
    sites = {'CC': [SimpleNamespace(site_id=x, latitude=float(i), longitude=float(i))
                    for i, x in enumerate(('A', 'B', 'C'))],
             'PUDO': [SimpleNamespace(site_id='D', latitude=4.0, longitude=4.0)]}
    assert [x['id'] for x in resolve_warehouses(['C', 'A', 'B'], sites)] == ['C', 'A', 'B']
    assert resolve_warehouses(['A', 'B', 'C'], sites)[0]['latitude'] == 0.0
    import pytest
    with pytest.raises(ValueError, match='not CC'):
        resolve_warehouses(['A', 'B', 'D'], sites)
    with pytest.raises(ValueError, match='unique'):
        resolve_warehouses(['A', 'A', 'B'], sites)
