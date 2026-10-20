from pathlib import Path
from hybrid_lastmile.parameters import load_glims_parameters

def test_real_glims_parameter_mapping():
    root = Path(__file__).resolve().parents[1] / 'data'
    if not (root / 'parameters' / 'model_parameters.csv').exists():
        return
    params = load_glims_parameters(root)
    assert params.capacities['pudo_capacity'] > 0
    assert 'cargo_bike_cost_per_km' in params.cost_catalogue
